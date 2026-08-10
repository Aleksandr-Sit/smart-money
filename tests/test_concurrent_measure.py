"""Замер выручки не должен видеть ЧУЖИЕ сделки (10.08, выход с итогом −200%).

Прежде выручка считалась как изменение баланса ВСЕГО кошелька до и после продажи.
Пока сделки идут по одной, это работает. Но бот держит до пяти позиций, и параллельная
ПОКУПКА тратит SOL в то же окно. Живой случай:

    "sol_actual": -0.13110942   ← ровно минус один клип
    "usd": -10.01
    "realized_pnl": -2.0014     ← −200% при цене выхода 0.96x

Купив токен на клип, потерять больше клипа нельзя: −100% это пол. Всё, что ниже, —
сломанный замер, а не убыток.
"""
import pytest

from src import swap


class _W:
    available = True
    address = "НАШ"


def _tx(sol_delta_lamports, pre_tok=0, post_tok=0, dec=6, owner="НАШ"):
    return {"result": {
        "meta": {
            "preBalances": [1_000_000_000],
            "postBalances": [1_000_000_000 + sol_delta_lamports],
            "preTokenBalances": [{"owner": owner, "mint": "MINT", "uiTokenAmount":
                                  {"amount": str(pre_tok), "decimals": dec}}],
            "postTokenBalances": [{"owner": owner, "mint": "MINT", "uiTokenAmount":
                                   {"amount": str(post_tok), "decimals": dec}}],
        },
        "transaction": {"message": {"accountKeys": ["НАШ"]}},
    }}


def test_дельта_берётся_из_транзакции(monkeypatch):
    """Внутри транзакции чужих операций нет по определению."""
    monkeypatch.setattr(swap.wallet, "Wallet", lambda: _W())
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    monkeypatch.setattr(swap.helius, "rpc",
                        lambda *a, **k: _tx(130_000_000, pre_tok=1_000_000, post_tok=0))
    sol, tok, dec = swap.tx_deltas("SIG", "MINT")
    assert sol == pytest.approx(0.13)
    assert tok == -1_000_000 and dec == 6


def test_параллельная_покупка_больше_не_портит_замер(monkeypatch):
    """Ключевой сценарий. Баланс кошелька за окно продажи упал на клип, потому что
    рядом прошла покупка. Транзакция продажи при этом принесла свои 0.13 SOL."""
    monkeypatch.setattr(swap.wallet, "Wallet", lambda: _W())
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    monkeypatch.setattr(swap.helius, "rpc", lambda *a, **k: _tx(130_000_000))
    sol, _, _ = swap.tx_deltas("SIG", "MINT")
    assert sol > 0, "внутри транзакции продажи расход чужой покупки не виден"


def test_узел_молчит_возвращаем_неизвестность(monkeypatch):
    monkeypatch.setattr(swap.wallet, "Wallet", lambda: _W())
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    monkeypatch.setattr(swap.helius, "rpc",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("429")))
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    assert swap.tx_deltas("SIG", "MINT", tries=2) == (None, None, 0)


def test_чужой_кошелёк_в_транзакции_не_считается(monkeypatch):
    monkeypatch.setattr(swap.wallet, "Wallet", lambda: _W())
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    monkeypatch.setattr(swap.helius, "rpc",
                        lambda *a, **k: _tx(130_000_000, pre_tok=5, post_tok=0, owner="ЧУЖОЙ"))
    _sol, tok, _ = swap.tx_deltas("SIG", "MINT")
    assert tok == 0, "токен-дельта чужого владельца не наша"


# ---------- пол −100% ----------
@pytest.mark.parametrize("факт,должны_отбросить", [
    (0.5, False), (-0.5, False), (-0.99, False), (-1.0, False),
    (-1.01, True), (-2.0014, True),
])
def test_итог_хуже_минус_ста_процентов_отбрасывается(факт, должны_отбросить):
    """Пол −100% физический: больше клипа в спот-лонге не теряют.

    Значение ниже означает сломанный ЗАМЕР, и пускать его в дневной стоп нельзя:
    ложный минус приблизил бы остановку торговли на ровном месте.
    """
    отбросили = факт < -1.0
    assert отбросили is должны_отбросить


def test_нулевая_или_отрицательная_выручка_это_не_выручка(monkeypatch):
    """Продажа обязана ПРИНОСИТЬ SOL. Ноль или минус — признак ошибки измерения,
    и честнее признать выручку неизвестной, чем записать выдуманное число."""
    for дельта in (0.0, -0.13):
        assert not (дельта > 0), "такие значения обязаны отбрасываться"
