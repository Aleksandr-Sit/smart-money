"""Учёт отказов до отправки и замер фрикции на каждой сделке (аудит 12.08).

Аудит нашёл: 223 живых намерения без исполнения при ВСЕГО 43 записях reject —
80% неудач были невидимы. Причина: между записью намерения и подтверждением лежат
сборка транзакции и отправка, и их исключения уходили наверх без следа.
"""
import pytest

from src import swap


@pytest.fixture
def живой(monkeypatch):
    monkeypatch.setattr(swap, "_live", lambda: True)
    monkeypatch.setattr(swap.market, "sol_price", lambda: 100.0)
    monkeypatch.setattr(swap, "_quote", lambda *a, **k: {"outAmount": "1000000",
                                                        "priceImpactPct": "0.01"})
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (0, 6, "ATA"))

    class _W:
        available = True
        address = "КОШЕЛЁК"
    monkeypatch.setattr(swap.wallet, "Wallet", lambda: _W())
    записи = {"intent": [], "reject": [], "fill": [], "measure": []}
    monkeypatch.setattr(swap.ledger, "record_measure",
                        lambda *a, **k: записи["measure"].append((a, k)))
    monkeypatch.setattr(swap.ledger, "record_intent",
                        lambda *a, **k: (записи["intent"].append(k), "ID")[1])
    monkeypatch.setattr(swap.ledger, "record_reject",
                        lambda *a, **k: записи["reject"].append((a, k)))
    monkeypatch.setattr(swap.ledger, "record_fill",
                        lambda *a, **k: записи["fill"].append((a, k)))
    return записи


def test_провал_сборки_пишет_отказ(живой, monkeypatch):
    """Симуляция транзакции упала — намерение уже записано, значит и отказ обязан быть."""
    monkeypatch.setattr(swap, "_build_swap_tx",
                        lambda q: (_ for _ in ()).throw(RuntimeError("симуляция упала")))
    with pytest.raises(RuntimeError):
        swap.buy("TOK", 10.0)
    assert len(живой["reject"]) == 1
    (_, kw) = живой["reject"][0]
    assert "RuntimeError" in живой["reject"][0][0][2]
    assert kw["extra"]["стадия"] == "до отправки"
    assert живой["fill"] == [], "исполнения не было — писать fill нельзя"


def test_провал_отправки_пишет_отказ(живой, monkeypatch):
    monkeypatch.setattr(swap, "_build_swap_tx", lambda q: {"tx": 1})
    monkeypatch.setattr(swap, "_sign_and_send",
                        lambda s: (_ for _ in ()).throw(RuntimeError("узел отверг")))
    with pytest.raises(RuntimeError):
        swap.buy("TOK", 10.0)
    assert len(живой["reject"]) == 1
    assert живой["reject"][0][1]["extra"]["side"] == "buy"


def test_отказ_продажи_до_отправки_помечен_причиной_выхода(monkeypatch):
    """У продажи в отказе должна быть причина выхода: иначе не понять, какой именно
    выход сорвался и надо ли его повторять."""
    monkeypatch.setattr(swap, "_live", lambda: True)
    monkeypatch.setattr(swap.market, "sol_price", lambda: 100.0)
    monkeypatch.setattr(swap, "_quote", lambda *a, **k: {"outAmount": "1000000"})
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (10 ** 6, 6, "ATA"))

    class _W:
        available = True
        address = "КОШЕЛЁК"
    monkeypatch.setattr(swap.wallet, "Wallet", lambda: _W())
    monkeypatch.setattr(swap.ledger, "record_intent", lambda *a, **k: "ID")
    отказы = []
    monkeypatch.setattr(swap.ledger, "record_reject", lambda *a, **k: отказы.append(k))
    monkeypatch.setattr(swap, "_build_swap_tx",
                        lambda q: (_ for _ in ()).throw(RuntimeError("нет маршрута")))
    with pytest.raises(RuntimeError):
        swap.sell("TOK", 1.0, reason="actors_exit")
    assert отказы and отказы[0]["extra"]["reason_exit"] == "actors_exit"


# ---------- фрикция ----------

def test_цена_кривой_достаётся_из_своей_транзакции(monkeypatch):
    """Событие pump.fun нашей же покупки несёт резервы — цена кривой даром."""
    monkeypatch.setattr(swap.log_parse if hasattr(swap, "log_parse") else swap, "_x", None,
                        raising=False)
    цена = swap._цена_кривой_из_логов(["мусор"], "TOK")
    assert цена is None, "нет события — нет цены, а не выдуманное число"


def test_замер_фрикции_не_роняет_покупку(живой, monkeypatch):
    """Наблюдение не имеет права ломать сделку: если разбор упал, покупка проходит."""
    monkeypatch.setattr(swap, "_build_swap_tx", lambda q: {"tx": 1})
    monkeypatch.setattr(swap, "_sign_and_send", lambda s: "SIG")
    monkeypatch.setattr(swap, "confirm_detail", lambda sig, timeout_s=None: (True, None))
    monkeypatch.setattr(swap, "_settled_token_balance_raw", lambda m, b, **k: (10 ** 6, 6))
    monkeypatch.setattr(swap, "tx_deltas",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("узел лёг")))
    r = swap.buy("TOK", 10.0)
    assert r["action"] == "bought"
    assert живой["fill"], "сделка обязана записаться несмотря на сбой замера"


def test_фрикция_считается_и_попадает_в_свою_запись(живой, monkeypatch):
    """Замер приходит ПОЗЖЕ сделки и своей записью: журнал append-only.

    До 13.08 фрикция считалась прямо в покупке одной попыткой, чтобы не задерживать
    позицию, и заполнялась лишь у 21% сделок — в группе крупных выигрышей осталось
    девять замеров, то есть показатель был непригоден там, где нужен.
    """
    # потратили 0.11 SOL, получили 1.0 токен → цена 0.11; кривая 0.10 → фрикция +10%
    monkeypatch.setattr(swap, "tx_deltas", lambda *a, **k: (-0.11, 10 ** 6, 6))
    monkeypatch.setattr(swap, "цена_кривой_сделки", lambda sig: 0.10)
    swap._замер_фрикции("ID", "SIG", "TOK", 6)
    (_, kw) = живой["measure"][0]
    assert kw["фрикция"] == pytest.approx(0.10, abs=1e-6)
    assert kw["curve_price_sol"] == pytest.approx(0.10)


def test_покупка_не_ждёт_замера(живой, monkeypatch):
    """Замер ушёл в фон — покупка обязана вернуться, не дожидаясь узла."""
    monkeypatch.setattr(swap, "_build_swap_tx", lambda q: {"tx": 1})
    monkeypatch.setattr(swap, "_sign_and_send", lambda s: "SIG")
    monkeypatch.setattr(swap, "confirm_detail", lambda sig, timeout_s=None: (True, None))
    monkeypatch.setattr(swap, "_settled_token_balance_raw", lambda m, b, **k: (10 ** 6, 6))
    запущено = []
    monkeypatch.setattr(swap.threading, "Thread",
                        lambda **k: type("_T", (), {"start": lambda s: запущено.append(k)})())
    r = swap.buy("TOK", 10.0)
    assert r["action"] == "bought"
    assert запущено and запущено[0]["target"] is swap._замер_фрикции
    assert запущено[0]["daemon"] is True, "поток не должен держать процесс при остановке"


# ---------- видимость доли исполнения ----------

def _строки(живых_нам, живых_фил, отказов):
    из = []
    for i in range(живых_нам):
        из.append({"type": "intent", "id": f"L{i}", "mode": "live", "side": "buy"})
    for i in range(живых_фил):
        из.append({"type": "fill", "intent_id": f"L{i}", "mode": "live", "usd": 10})
    for i in range(отказов):
        из.append({"type": "reject", "intent_id": f"L{живых_фил + i}", "mode": "live"})
    из.append({"type": "intent", "id": "P0", "mode": "paper"})
    из.append({"type": "fill", "intent_id": "P0", "mode": "paper", "usd": 10})
    return из


def test_живая_доля_исполнения_считается_отдельно():
    """Общая доля смешивает бумагу (всегда 100%) с живым режимом (оказалось 63%)."""
    from src import ledger
    r = ledger.reconcile(_строки(живых_нам=10, живых_фил=6, отказов=1))
    assert r["live_intents"] == 10 and r["live_fills"] == 6
    assert r["live_fill_rate"] == pytest.approx(0.6)


def test_необъяснённые_неудачи_видны_отдельно():
    """Аудит нашёл 180 таких из 223: намерение не исполнилось, причина не записана."""
    from src import ledger
    r = ledger.reconcile(_строки(живых_нам=10, живых_фил=6, отказов=1))
    assert r["live_unexplained"] == 3, "4 неудачи, объяснена 1 → 3 без объяснения"


def test_сводка_показывает_живую_долю(monkeypatch):
    from src import ledger
    monkeypatch.setattr(ledger, "load", lambda: _строки(10, 6, 1))
    т = ledger.summary()
    assert "ЖИВЫХ: исполнено 6/10 (60%)" in т
    assert "БЕЗ ОБЪЯСНЕНИЯ 3" in т


def test_без_живых_сделок_строка_не_появляется(monkeypatch):
    """В бумажном режиме показывать «живую долю» нечего, и молчать честнее нуля."""
    from src import ledger
    monkeypatch.setattr(ledger, "load", lambda: [{"type": "intent", "id": "P0", "mode": "paper"},
                                                 {"type": "fill", "intent_id": "P0",
                                                  "mode": "paper", "usd": 1}])
    assert "ЖИВЫХ" not in ledger.summary()
