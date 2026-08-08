"""Идемпотентность выхода: одна позиция закрывается ровно один раз.

Аудит 08.08 нашёл гонку, жившую с 12 июля. Две продажи разных акторов приходят
почти одновременно: первая запускает emit_exit, тот отдаёт управление на await
(замер фрикции), вторая успевает пройти on_sell — порог снова достигнут — и
запускает ВТОРОЙ emit_exit. Оба видят позицию открытой, потому что pm.close()
стоит в конце.

Замер на боевых данных: 77 позиций закрыты дважды, у 99% разрыв меньше секунды
(медиана 42 мс). Худшее последствие не в статистике: risk_state.realized_usd
считал убыток дважды, и дневной стоп срабатывал раньше срока. В live это две
команды на продажу одной позиции и двойная комиссия.

Здесь воспроизводится сама схема защёлки, потому что emit_exit — замыкание
внутри run() и напрямую не вызывается.
"""
import asyncio


async def _scenario(guarded: bool):
    """Две «продажи» приходят во время await внутри выхода. Считаем закрытия."""
    exiting: set[str] = set()
    closed: list[str] = []
    open_pos = {"TOK"}

    async def body(token):
        # ПОРЯДОК КАК В БОЕВОМ КОДЕ: проверка позиции ДО ожидания, закрытие — ПОСЛЕ.
        # Именно из-за этого зазора и возникала гонка: оба вызова успевали пройти
        # проверку, пока ни один ещё не дошёл до pm.close().
        if token not in open_pos:
            return
        await asyncio.sleep(0)          # точка, где управление уходит (замер фрикции)
        open_pos.discard(token)         # pm.close() в самом конце
        closed.append(token)

    async def emit(token):
        if guarded:
            if token in exiting:
                return
            exiting.add(token)
            try:
                await body(token)
            finally:
                exiting.discard(token)
        else:
            await body(token)

    await asyncio.gather(emit("TOK"), emit("TOK"))
    return closed


def test_без_защёлки_позиция_закрывается_дважды():
    """Фиксируем сам дефект: без защёлки схема даёт двойное закрытие."""
    assert len(asyncio.run(_scenario(guarded=False))) == 2


def test_с_защёлкой_ровно_одно_закрытие():
    assert len(asyncio.run(_scenario(guarded=True))) == 1


async def _release_scenario():
    exiting: set[str] = set()

    async def emit(fail: bool):
        if "TOK" in exiting:
            return "заблокировано"
        exiting.add("TOK")
        try:
            if fail:
                raise RuntimeError("своп упал")
            return "ок"
        finally:
            exiting.discard("TOK")

    try:
        await emit(fail=True)
        raise AssertionError("исключение должно было пройти наружу")
    except RuntimeError:
        pass
    assert "TOK" not in exiting          # защёлка снята несмотря на исключение
    return await emit(fail=False)


def test_защёлка_снимается_после_выхода():
    """finally обязателен: иначе токен навсегда останется «в процессе выхода»
    и позиция по нему никогда не закроется."""
    assert asyncio.run(_release_scenario()) == "ок"
