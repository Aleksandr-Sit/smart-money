"""Двойное закрытие одной позиции — гонка, найденная аудитом 08.08.

Замер на боевых данных: 77 позиций закрыты дважды с 12 июля, у 99% разрыв между
записями меньше секунды (медиана 42 мс). Пример:
    HRDDDryyz7814C  08:16:13.273  actors_exit  вышло 2/2  −94.27%
    HRDDDryyz7814C  08:16:13.387  actors_exit  вышло 2/2  −94.57%

Последствия: двойной учёт в paper_closed и в risk_state.realized_usd, из-за чего
дневной стоп срабатывал раньше срока. В live это была бы ВТОРАЯ команда на продажу
уже проданной позиции.
"""
import asyncio

import pytest

from src.positions import PositionManager


def test_корневая_причина_on_sell_отдаёт_выход_дважды(tmp_path):
    """PositionManager сам по себе НЕ защищает от повторного сигнала выхода.

    Это не дефект менеджера: он не знает, довёл ли вызывающий выход до конца.
    Защёлка обязана стоять в мониторе, вокруг emit_exit. Тест фиксирует, что
    источник гонки именно здесь — если кто-то решит «чинить» в on_sell, он
    сломает откат при неудачной продаже в live.
    """
    from src import strategy
    pm = PositionManager({**strategy.EXIT, "EXIT_ACTOR_FRAC": 0.25})
    pm.path = tmp_path / "p.json"
    pm.pos = {}
    pm.open("TOK", 0.001, 1e6, ["a1", "a2"], 1000)
    assert pm.on_sell("TOK", "a1") == "actors_exit"      # порог достигнут
    assert pm.on_sell("TOK", "a2") == "actors_exit"      # и ВТОРОЙ раз тоже
    assert len(pm.get("TOK").exited_actors) == 2


def test_защёлка_пропускает_только_первый_выход():
    """Воспроизводит гонку: второй вызов приходит, пока первый ждёт на await."""
    exiting: set[str] = set()
    calls = []

    async def _inner(token):
        await asyncio.sleep(0)        # точка, где управление уходит (замер фрикции)
        calls.append(token)

    async def emit(token):
        if token in exiting:
            return
        exiting.add(token)
        try:
            await _inner(token)
        finally:
            exiting.discard(token)

    async def main():
        await asyncio.gather(emit("TOK"), emit("TOK"), emit("TOK"))

    asyncio.run(main())
    assert calls == ["TOK"], "выход должен выполниться РОВНО один раз"


def test_защёлка_снимается_и_повторный_вход_возможен():
    """После завершения выхода токен должен снова быть доступен: бот входит
    в тот же токен повторно (1346 таких сделок за период, они прибыльны)."""
    exiting: set[str] = set()
    calls = []

    async def emit(token):
        if token in exiting:
            return
        exiting.add(token)
        try:
            await asyncio.sleep(0)
            calls.append(token)
        finally:
            exiting.discard(token)

    async def main():
        await emit("TOK")
        await emit("TOK")             # уже завершился — второй проход разрешён

    asyncio.run(main())
    assert calls == ["TOK", "TOK"]


def test_защёлка_снимается_даже_при_ошибке():
    """Без finally упавший выход навсегда заблокировал бы токен — позиция
    осталась бы открытой и невыходимой."""
    exiting: set[str] = set()

    async def emit(token, boom):
        if token in exiting:
            return
        exiting.add(token)
        try:
            if boom:
                raise RuntimeError("узел недоступен")
        finally:
            exiting.discard(token)

    async def main():
        with pytest.raises(RuntimeError):
            await emit("TOK", True)
        assert "TOK" not in exiting

    asyncio.run(main())


def test_разные_токены_не_блокируют_друг_друга():
    exiting: set[str] = set()
    calls = []

    async def emit(token):
        if token in exiting:
            return
        exiting.add(token)
        try:
            await asyncio.sleep(0)
            calls.append(token)
        finally:
            exiting.discard(token)

    async def main():
        await asyncio.gather(emit("A"), emit("B"), emit("A"))

    asyncio.run(main())
    assert sorted(calls) == ["A", "B"]
