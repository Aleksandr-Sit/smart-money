"""Несостоявшаяся сделка — не исполнение (найдено 10.08 при пересчёте отказов).

Отказы бывают ДВУХ классов, и первый мой замер видел только один:
  (а) предполёт: sendTransaction вернул -32002, транзакция НЕ ушла в сеть.
      Исключение летит ДО записи исполнения → намерение остаётся без пары. Стоит НОЛЬ.
  (б) транзакция долетела и упала на цепи. Прежде по ней всё равно писался fill —
      с confirmed=false, суммой из котировки и нулевым количеством. В учёте это
      выглядело состоявшейся сделкой на $10.

Из-за (б) доля отказов на первых 46 живых сделках вышла 22% вместо настоящих 35%:
покупки 10 из 26 (38%), продажи 6 из 20 (30%).
"""
import pytest

from src import ledger, swap


def _мок(monkeypatch, записи, ok, причина, settled=(0, 6), баланс=0):
    """баланс — что лежит на токен-аккаунте ДО сделки (для продажи нужен ненулевой)."""
    monkeypatch.setitem(swap.strategy.EXECUTION, "LIVE_ENABLED", True)
    monkeypatch.setattr(swap.wallet, "Wallet",
                        lambda: type("W", (), {"available": True, "address": "a",
                                               "keypair": lambda s: "kp",
                                               "balance_sol": lambda s, url=None: 5.0})())
    monkeypatch.setattr(swap.market, "sol_price", lambda: 77.0)
    monkeypatch.setattr(swap, "_quote", lambda *a, **k: {"outAmount": "1000000"})
    monkeypatch.setattr(swap, "_build_swap_tx", lambda q: {"swapTransaction": "x"})
    monkeypatch.setattr(swap, "_sign_and_send", lambda s: "SIG")
    monkeypatch.setattr(swap, "confirm_detail", lambda s, t=None: (ok, причина))
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (баланс, 6, "ata"))
    monkeypatch.setattr(swap, "_settled_token_balance_raw", lambda m, b, **k: settled)
    # выручка читается из транзакции (правка 10.08) — иначе тест уходит в сеть
    monkeypatch.setattr(swap, "tx_deltas", lambda s, m, **k: (0.13, -1_000_000, 6))
    monkeypatch.setattr(swap.ledger, "record_intent", lambda *a, **k: "iid")
    monkeypatch.setattr(swap.ledger, "record_fill",
                        lambda *a, **k: записи.append(("fill", a, k)))
    monkeypatch.setattr(swap.ledger, "record_reject",
                        lambda *a, **k: записи.append(("reject", a, k)))


def test_упавшая_на_цепи_покупка_пишет_отказ_а_не_исполнение(monkeypatch):
    записи = []
    _мок(monkeypatch, записи, ok=False, причина="превышен допуск проскальзывания")
    with pytest.raises(swap.SwapError):
        swap.buy("MINT", 10.0)
    виды = [z[0] for z in записи]
    assert виды == ["reject"], f"исполнения быть не должно, получено {виды}"
    assert "проскальзыван" in записи[0][1][2]


def test_упавшая_на_цепи_продажа_пишет_отказ_а_не_исполнение(monkeypatch):
    """Прежде сюда писалась ВЫРУЧКА из котировки — то есть утверждение, что
    деньги получены, при том что токены остались в кошельке."""
    записи = []
    _мок(monkeypatch, записи, ok=False, причина="превышен допуск проскальзывания",
         баланс=1_000_000)
    with pytest.raises(swap.SwapError):
        swap.sell("MINT", 1.0)
    виды = [z[0] for z in записи]
    assert виды == ["reject"]
    assert записи[0][2]["extra"]["side"] == "sell"


def test_состоявшаяся_покупка_пишет_исполнение(monkeypatch):
    записи = []
    _мок(monkeypatch, записи, ok=True, причина=None, settled=(1_000_000, 6))
    r = swap.buy("MINT", 10.0)
    assert r["action"] == "bought"
    assert [z[0] for z in записи] == ["fill"]


def test_подтверждения_нет_но_токены_пришли_это_исполнение(monkeypatch):
    """Граница: вердикт не успел, а токены на счету. Сделка состоялась."""
    записи = []
    _мок(monkeypatch, записи, ok=False, причина=None, settled=(1_000_000, 6))
    r = swap.buy("MINT", 10.0)
    assert r["action"] == "bought" and r["confirmed"] is False
    assert [z[0] for z in записи] == ["fill"]


def test_состоявшаяся_продажа_пишет_исполнение(monkeypatch):
    записи = []
    _мок(monkeypatch, записи, ok=True, причина=None, баланс=1_000_000)
    monkeypatch.setattr(swap, "close_token_account", lambda m: {"action": "skip"})
    r = swap.sell("MINT", 1.0)
    assert r["action"] == "sold"
    assert [z[0] for z in записи] == ["fill"]


# ---------- сверка видит отказы ----------
def test_сверка_считает_отказы_и_долю():
    rows = [
        {"type": "intent", "id": "a", "side": "buy", "price": 1.0},
        {"type": "fill", "intent_id": "a", "price": 1.0, "usd": 10.0},
        {"type": "intent", "id": "b", "side": "sell", "price": 2.0},
        {"type": "reject", "intent_id": "b", "reason": "проскальзывание"},
        {"type": "intent", "id": "c", "side": "buy", "price": 1.0},   # предполёт: пусто
    ]
    r = ledger.reconcile(rows)
    assert r["rejects"] == 1
    assert r["unfilled"] == 2, "и отклонённые, и не ушедшие в сеть"
    assert r["reject_rate"] == pytest.approx(2 / 3)
    assert r["gross_usd"] == pytest.approx(10.0), "отказ не должен раздувать оборот"


def test_отказ_не_считается_исполнением_без_намерения():
    """reject ссылается на намерение, но не является fill — тревогу поднимать не о чем."""
    rows = [
        {"type": "intent", "id": "a", "side": "buy", "price": 1.0},
        {"type": "reject", "intent_id": "a", "reason": "x"},
    ]
    r = ledger.reconcile(rows)
    assert r["orphan_fills"] == 0 and r["ok"] is True
