"""Сорванная продажа повторяется на ближайшем тике, а не ждёт другого правила (10.08).

Прежде при отказе продажи позиция просто оставалась открытой. Актор уже помечен
вышедшим, поэтому on_sell больше не срабатывал, а ценовые правила ждали своего часа —
и позиция доживала до таймаута.

ЗАМЕР на 45 цепочках «сорвалась → продали позже», цены из траекторий:
    фактически (ждали медиану 259с)  медиана −15.40%  сумма  +$0.72
    повтор через 15с                 медиана  −4.29%  сумма +$17.17   <- выбрано
    повтор через 30с                 медиана  +0.95%  сумма  +$4.59
    повтор через 60с                 медиана  −2.38%  сумма  −$6.91
15 секунд совпадают с тиком трекера, поэтому повтор ничего не стоит по сложности.
"""
import asyncio


def _контур():
    """Модель: очередь повторов + выход, который может сорваться."""
    повтор: dict[str, str] = {}
    открытые = {"TOK"}
    журнал: list[str] = []
    отказать = {"да": True}

    async def emit_exit(token, price, reason):
        if token not in открытые:
            return
        if отказать["да"]:
            повтор[token] = reason
            журнал.append(f"сорвалась:{reason}")
            return
        повтор.pop(token, None)
        открытые.discard(token)
        журнал.append(f"продана:{reason}")

    async def tick(prices):
        for token, причина in list(повтор.items()):
            if token not in открытые:
                повтор.pop(token, None)
                continue
            журнал.append("повтор")
            await emit_exit(token, prices.get(token), причина)

    return emit_exit, tick, журнал, повтор, открытые, отказать


def test_сорванная_продажа_ставится_в_очередь_повтора():
    emit_exit, tick, журнал, повтор, _, _ = _контур()
    asyncio.run(emit_exit("TOK", 1.0, "actors_exit"))
    assert повтор == {"TOK": "actors_exit"}
    assert журнал == ["сорвалась:actors_exit"]


def test_повтор_срабатывает_на_ближайшем_тике():
    emit_exit, tick, журнал, повтор, открытые, отказать = _контур()

    async def main():
        await emit_exit("TOK", 1.0, "actors_exit")
        отказать["да"] = False              # узел ожил
        await tick({"TOK": 0.9})

    asyncio.run(main())
    assert журнал == ["сорвалась:actors_exit", "повтор", "продана:actors_exit"]
    assert повтор == {} and "TOK" not in открытые


def test_причина_выхода_сохраняется():
    """Повторять надо ТУ ЖЕ причину: решение выйти уже принято, а не пересматривается."""
    emit_exit, tick, журнал, повтор, _, отказать = _контур()

    async def main():
        await emit_exit("TOK", 1.0, "trailing")
        отказать["да"] = False
        await tick({"TOK": 0.9})

    asyncio.run(main())
    assert "продана:trailing" in журнал


def test_повтор_не_виснет_если_позиция_уже_закрыта():
    """Позиция могла закрыться другим путём — очередь обязана чиститься."""
    emit_exit, tick, журнал, повтор, открытые, _ = _контур()

    async def main():
        await emit_exit("TOK", 1.0, "actors_exit")
        открытые.discard("TOK")             # закрылась иначе
        await tick({"TOK": 0.9})

    asyncio.run(main())
    assert повтор == {}
    assert журнал.count("повтор") == 0


def test_повторы_копятся_пока_узел_лежит():
    """Пока продажа не проходит, задача остаётся в очереди и пробуется каждый тик."""
    emit_exit, tick, журнал, повтор, _, _ = _контур()

    async def main():
        await emit_exit("TOK", 1.0, "actors_exit")
        for _ in range(3):
            await tick({"TOK": 0.9})

    asyncio.run(main())
    assert повтор == {"TOK": "actors_exit"}, "задача не должна теряться"
    assert журнал.count("повтор") == 3
