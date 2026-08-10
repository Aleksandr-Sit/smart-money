"""Токены в кошельке без открытой позиции — путь, которым живой режим теряет деньги.

Аудит 10.08: `swap.buy` ждал подтверждения CONFIRM_TIMEOUT_S секунд и при таймауте
объявлял покупку неудачной. Монитор откатывал слот. Если транзакция подтверждалась
позже, токены приходили в кошелёк БЕЗ позиции: цену никто не вёл, правила выхода не
применялись, продать было некому. Мем-коин теряет ликвидность за часы, поэтому
разбор идёт по правилу «нашёл — продал», а не «нашёл — сообщил».
"""
import pytest

from src import orphans, swap


def _acc(mint, amount, ata="ata"):
    return {"mint": mint, "amount": amount, "ata": ata}


# ---------- раскладка: чистая логика решения ----------
def test_токен_без_позиции_это_сирота():
    g = orphans.classify([_acc("A", 100.0)], open_tokens=set(), prices={"A": 1.0})
    assert [x["mint"] for x in g["orphan"]] == ["A"]
    assert g["orphan"][0]["usd"] == pytest.approx(100.0)


def test_открытую_позицию_не_трогаем():
    """Её ведёт монитор: свои правила выхода, свой трекер цены."""
    g = orphans.classify([_acc("A", 100.0)], open_tokens={"A"}, prices={"A": 1.0})
    assert g["orphan"] == [] and [x["mint"] for x in g["held"]] == ["A"]


def test_пустой_аккаунт_закрываем_ради_ренты():
    """Каждый держит ~0.002 SOL. При 230 сделках в сутки это ~35$ замороженных."""
    g = orphans.classify([_acc("A", 0.0)], open_tokens=set(), prices={})
    assert [x["mint"] for x in g["empty"]] == ["A"]


def test_пыль_не_продаём():
    """Комиссия свопа больше остатка — продажа обошлась бы дороже находки."""
    g = orphans.classify([_acc("A", 1.0)], open_tokens=set(), prices={"A": 0.01},
                         dust_usd=0.50)
    assert g["orphan"] == [] and [x["mint"] for x in g["dust"]] == ["A"]


def test_неизвестная_цена_НЕ_делает_токен_пылью():
    """Нет котировки — не повод молча оставить токен в кошельке.

    Ловушка: если считать отсутствующую цену нулём, ЛЮБОЙ токен без котировки
    попадал бы в пыль и оставался лежать — ровно те токены, ради которых модуль
    и написан (на миграции пула Jupiter временно не отдаёт котировку).
    """
    g = orphans.classify([_acc("A", 100.0)], open_tokens=set(), prices={})
    assert [x["mint"] for x in g["orphan"]] == ["A"]


def test_wsol_не_трогаем_никогда():
    """Обёртка для расчётов, а не позиция."""
    g = orphans.classify([_acc(orphans.WSOL, 5.0)], open_tokens=set(), prices={})
    assert all(not v for v in g.values())


def test_несколько_куч_разом():
    g = orphans.classify(
        [_acc("сирота", 100.0), _acc("своя", 50.0), _acc("пустой", 0.0),
         _acc("пыль", 1.0), _acc(orphans.WSOL, 2.0)],
        open_tokens={"своя"},
        prices={"сирота": 1.0, "пыль": 0.001})
    assert [x["mint"] for x in g["orphan"]] == ["сирота"]
    assert [x["mint"] for x in g["held"]] == ["своя"]
    assert [x["mint"] for x in g["empty"]] == ["пустой"]
    assert [x["mint"] for x in g["dust"]] == ["пыль"]


# ---------- разбор ----------
def test_ошибка_по_одному_токену_не_останавливает_остальные(monkeypatch):
    """Застрявший мем-коин не должен мешать вернуть деньги из остальных."""
    monkeypatch.setattr(orphans, "scan", lambda: {
        "orphan": [_acc("плохой", 1.0), _acc("хороший", 1.0)],
        "empty": [], "held": [], "dust": [], "orphan_usd": 20.0, "accounts": 2})

    def _sell(mint, frac, reason):
        if mint == "плохой":
            raise swap.SwapError("нет маршрута")
        return {"action": "sold", "signature": "sig1"}

    monkeypatch.setattr(orphans.swap, "sell", _sell)
    monkeypatch.setattr(orphans.swap, "close_token_account", lambda m: {"action": "closed"})
    r = orphans.recover(dry_run=False)
    assert [x["mint"] for x in r["sold"]] == ["хороший"]
    assert [x["mint"] for x in r["failed"]] == ["плохой"]


def test_осмотр_без_флага_ничего_не_продаёт(monkeypatch):
    """dry_run по умолчанию: модуль тратит настоящие деньги, тишина обязана быть безопасной."""
    monkeypatch.setattr(orphans, "scan", lambda: {
        "orphan": [_acc("A", 1.0)], "empty": [_acc("B", 0.0)],
        "held": [], "dust": [], "orphan_usd": 5.0, "accounts": 2})

    def _boom(*a, **k):
        raise AssertionError("dry_run не должен ничего отправлять")

    monkeypatch.setattr(orphans.swap, "sell", _boom)
    monkeypatch.setattr(orphans.swap, "close_token_account", _boom)
    r = orphans.recover()
    assert r["dry_run"] and r["orphans"] == 1
    assert r["sold"][0]["action"] == "dry_run"


# ---------- покупка: факт баланса важнее вердикта подтверждения ----------
def test_покупка_без_подтверждения_но_с_токенами_считается_состоявшейся(monkeypatch):
    """Прежний код бросал исключение, монитор откатывал слот — и рождалась сирота.

    Токены на балансе — это факт. Вердикт confirm() всего лишь не успел прийти
    за CONFIRM_TIMEOUT_S; транзакция при этом уже в блоке.
    """
    monkeypatch.setitem(swap.strategy.EXECUTION, "LIVE_ENABLED", True)
    monkeypatch.setattr(swap.wallet, "Wallet",
                        lambda: type("W", (), {"available": True, "address": "addr",
                                               "keypair": lambda s: "kp"})())
    monkeypatch.setattr(swap.market, "sol_price", lambda: 100.0)
    monkeypatch.setattr(swap, "_quote", lambda a, b, c: {"outAmount": "1000"})
    monkeypatch.setattr(swap, "_build_swap_tx", lambda q: {"swapTransaction": "x"})
    monkeypatch.setattr(swap, "_sign_and_send", lambda s: "SIG")
    monkeypatch.setattr(swap, "confirm", lambda s: False)          # не дождались вердикта
    monkeypatch.setattr(swap, "token_balance", lambda m: (0.0, None))
    monkeypatch.setattr(swap, "_settled_token_balance", lambda m, b, tries=6: 1000.0)
    monkeypatch.setattr(swap.ledger, "record_intent", lambda *a, **k: "iid")
    monkeypatch.setattr(swap.ledger, "record_fill", lambda *a, **k: None)

    r = swap.buy("MINT", 10.0)
    assert r["action"] == "bought" and r["tokens"] == 1000.0
    assert r["confirmed"] is False, "флаг обязан ехать наверх — это не обычная покупка"


def test_ни_подтверждения_ни_токенов_отказ(monkeypatch):
    """Позиции нет — и делать вид, что есть, нельзя. Токен подберёт разбор сирот."""
    monkeypatch.setitem(swap.strategy.EXECUTION, "LIVE_ENABLED", True)
    monkeypatch.setattr(swap.wallet, "Wallet",
                        lambda: type("W", (), {"available": True, "address": "addr",
                                               "keypair": lambda s: "kp"})())
    monkeypatch.setattr(swap.market, "sol_price", lambda: 100.0)
    monkeypatch.setattr(swap, "_quote", lambda a, b, c: {"outAmount": "1000"})
    monkeypatch.setattr(swap, "_build_swap_tx", lambda q: {"swapTransaction": "x"})
    monkeypatch.setattr(swap, "_sign_and_send", lambda s: "SIG")
    monkeypatch.setattr(swap, "confirm", lambda s: False)
    monkeypatch.setattr(swap, "token_balance", lambda m: (0.0, None))
    monkeypatch.setattr(swap, "_settled_token_balance", lambda m, b, tries=6: 0.0)
    monkeypatch.setattr(swap.ledger, "record_intent", lambda *a, **k: "iid")
    monkeypatch.setattr(swap.ledger, "record_fill", lambda *a, **k: None)

    with pytest.raises(swap.SwapError, match="разбор сирот"):
        swap.buy("MINT", 10.0)
