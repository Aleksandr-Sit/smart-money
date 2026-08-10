"""Гонка на ВХОДЕ — найдена первым же часом живой торговли (10.08).

pm.open() резервирует слот ДО отправки покупки, а покупка идёт несколько секунд:
котировка, сборка, отправка, подтверждение, расчёт баланса. Если в это окно приходит
продажа актора, выход видит открытую позицию и пытается продать ещё не купленный токен:

    [LIVE BUY FAIL]  6AiM14f1vDuS: Transaction simulation failed
    [LIVE SELL FAIL] 6AiM14f1vDuS: баланс не прочитан — токен остаётся у нас

Опаснее обратный случай: покупка УСПЕШНА, выход отработал в её окне, позиция закрылась
раньше, чем бот о ней узнал, — и токены остались сиротой без трекинга и без выхода.

Тот же класс, что гонка двойного выхода (аудит 08.08), но на входе.
"""
import asyncio


def _контур():
    """Модель обеих защёлок монитора: вход откладывает выход, а не теряет его."""
    entering: dict[str, str] = {}
    exiting: set[str] = set()
    журнал: list[str] = []

    async def emit_exit(token, reason):
        if token in entering:
            entering[token] = reason
            журнал.append(f"отложен:{reason}")
            return
        if token in exiting:
            журнал.append("дубль")
            return
        exiting.add(token)
        try:
            await asyncio.sleep(0)
            журнал.append(f"выход:{reason}")
        finally:
            exiting.discard(token)

    async def enter(token, покупка):
        entering[token] = ""
        try:
            ok = await покупка()
        finally:
            отложено = entering.pop(token, "")
        if not ok:
            журнал.append("покупка сорвалась, слот откачен")
            return
        журнал.append("позиция открыта")
        if отложено:
            await emit_exit(token, отложено)

    return emit_exit, enter, журнал


def test_выход_во_время_покупки_не_продаёт_некупленное():
    """Ровно тот случай, что дал LIVE SELL FAIL на 6AiM14f1vDuS."""
    emit_exit, enter, журнал = _контур()

    async def покупка():
        await asyncio.sleep(0.01)          # окно, в котором прилетает продажа актора
        return False                        # симуляция транзакции провалилась

    async def main():
        await asyncio.gather(enter("TOK", покупка), emit_exit("TOK", "actors_exit"))

    asyncio.run(main())
    assert "отложен:actors_exit" in журнал
    assert not any(x.startswith("выход:") for x in журнал), \
        "продавать некупленный токен нельзя"
    assert "покупка сорвалась, слот откачен" in журнал


def test_выход_во_время_УСПЕШНОЙ_покупки_догоняет():
    """Сигнал выхода терять нельзя: продажа актора закрывает 86% позиций."""
    emit_exit, enter, журнал = _контур()

    async def покупка():
        await asyncio.sleep(0.01)
        return True

    async def main():
        await asyncio.gather(enter("TOK", покупка), emit_exit("TOK", "actors_exit"))

    asyncio.run(main())
    assert журнал == ["отложен:actors_exit", "позиция открыта", "выход:actors_exit"], \
        f"порядок обязан быть именно таким, получено {журнал}"


def test_обычный_выход_после_покупки_не_откладывается():
    emit_exit, enter, журнал = _контур()

    async def покупка():
        return True

    async def main():
        await enter("TOK", покупка)
        await emit_exit("TOK", "timeout")

    asyncio.run(main())
    assert журнал == ["позиция открыта", "выход:timeout"]


def test_две_продажи_во_время_покупки_дают_один_выход():
    """Отложенная причина перезаписывается, но выход всё равно один."""
    emit_exit, enter, журнал = _контур()

    async def покупка():
        await asyncio.sleep(0.02)
        return True

    async def main():
        await asyncio.gather(enter("TOK", покупка),
                             emit_exit("TOK", "actors_exit"),
                             emit_exit("TOK", "actors_exit"))

    asyncio.run(main())
    assert журнал.count("выход:actors_exit") == 1


def test_разные_токены_не_блокируют_друг_друга():
    emit_exit, enter, журнал = _контур()

    async def покупка():
        await asyncio.sleep(0.01)
        return True

    async def main():
        await asyncio.gather(enter("A", покупка), emit_exit("B", "timeout"))

    asyncio.run(main())
    assert "выход:timeout" in журнал and "позиция открыта" in журнал


def test_пометка_снимается_даже_при_ошибке_покупки():
    """Без finally упавшая покупка навсегда заблокировала бы выходы по токену."""
    entering: dict[str, str] = {}

    async def enter(token):
        entering[token] = ""
        try:
            raise RuntimeError("Jupiter недоступен")
        finally:
            entering.pop(token, "")

    async def main():
        try:
            await enter("TOK")
        except RuntimeError:
            pass
        assert "TOK" not in entering

    asyncio.run(main())
