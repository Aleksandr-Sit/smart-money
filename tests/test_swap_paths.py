"""Пути исполнения swap.py — код, который тратит настоящие деньги.

Аудит 08.08: покрытие было 40%, непокрытыми оставались именно ветки сборки,
отправки и подтверждения. Тесты на моках НЕ заменяют пробную живую сделку —
они ловят регрессии, а не подтверждают, что транзакция долетит до блока.
Каждый тест ниже закрывает случай, в котором ошибка стоит денег.
"""
import base64

import pytest

from src import execution, swap


@pytest.fixture(autouse=True)
def _dry(monkeypatch):
    """По умолчанию LIVE выключен — ни один тест не должен ничего отправить."""
    monkeypatch.setitem(swap.strategy.EXECUTION, "LIVE_ENABLED", False)


class _W:
    available = True
    address = "6cuRYwqqqSoXqFpLV942kYNybena7c2Sbv5btZKrnRZA"

    def balance_sol(self):
        return 1.0

    def keypair(self):
        return "keypair-заглушка"


# ---------- потолки и отказы ----------
def test_покупка_выше_потолка_отклоняется(monkeypatch):
    """MAX_SWAP_USD — последний рубеж, если позвали с мусорной суммой."""
    cap = swap._cfg()["MAX_SWAP_USD"]
    with pytest.raises(swap.SwapError, match="выше потолка"):
        swap.buy("MINT", cap + 0.01)


def test_без_кошелька_ничего_не_делаем(monkeypatch):
    monkeypatch.setattr(swap.wallet, "Wallet", lambda: type("W", (), {"available": False})())
    assert swap.buy("MINT", 10)["action"] == "skip"
    assert swap.sell("MINT")["action"] == "skip"


def test_доля_продажи_вне_диапазона(monkeypatch):
    monkeypatch.setattr(swap.wallet, "Wallet", _W)
    for bad in (0, -0.5, 1.5):
        with pytest.raises(swap.SwapError, match="доля"):
            swap.sell("MINT", bad)


def test_нечего_продавать_при_нулевом_балансе(monkeypatch):
    monkeypatch.setattr(swap.wallet, "Wallet", _W)
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (0, 0, None))
    assert swap.sell("MINT")["action"] == "skip"


# ---------- предполётная проверка Jupiter ----------
def test_симуляция_провалилась_не_отправляем(monkeypatch):
    """Jupiter симулирует до нас. Отправлять заведомо провальную tx — сжечь комиссию."""
    monkeypatch.setattr(swap.wallet, "Wallet", _W)

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"swapTransaction": "AAA", "simulationError": {"error": "insufficient funds"}}

    monkeypatch.setattr(swap.requests, "post", lambda *a, **k: R())
    with pytest.raises(swap.SwapError, match="симуляция провалилась"):
        swap._build_swap_tx({"outAmount": "1"})


def test_jupiter_не_вернул_транзакцию(monkeypatch):
    monkeypatch.setattr(swap.wallet, "Wallet", _W)

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(swap.requests, "post", lambda *a, **k: R())
    with pytest.raises(swap.SwapError, match="не вернул транзакцию"):
        swap._build_swap_tx({"outAmount": "1"})


def test_jupiter_недоступен(monkeypatch):
    monkeypatch.setattr(swap.wallet, "Wallet", _W)

    def boom(*a, **k):
        raise ConnectionError("нет сети")

    monkeypatch.setattr(swap.requests, "post", boom)
    with pytest.raises(swap.SwapError, match="Jupiter недоступен"):
        swap._build_swap_tx({"outAmount": "1"})


def test_jupiter_вернул_ошибку_http(monkeypatch):
    monkeypatch.setattr(swap.wallet, "Wallet", _W)

    class R:
        status_code = 500
        text = "internal"

    monkeypatch.setattr(swap.requests, "post", lambda *a, **k: R())
    with pytest.raises(swap.SwapError, match="HTTP 500"):
        swap._build_swap_tx({"outAmount": "1"})


def test_нет_маршрута(monkeypatch):
    monkeypatch.setattr(execution, "quote", lambda *a, **k: None)
    with pytest.raises(swap.SwapError, match="нет маршрута"):
        swap._quote(swap.WSOL, "MINT", 1000)


# ---------- отправка ----------
def test_отправка_отклонена_узлом(monkeypatch):
    """Узел может отказать. Молча считать это успехом нельзя."""
    monkeypatch.setattr(swap.wallet, "Wallet", _W)
    monkeypatch.setattr(swap.helius, "rpc", lambda *a, **k: {"error": {"message": "blockhash not found"}})

    class VT:
        def __init__(self, message=None, signers=None):
            self.message = message

        def __bytes__(self):
            return b"signed-tx"

        @staticmethod
        def from_bytes(b):
            return VT(message="msg")

    import sys
    import types
    mod = types.ModuleType("solders.transaction")
    mod.VersionedTransaction = VT
    monkeypatch.setitem(sys.modules, "solders.transaction", mod)
    with pytest.raises(swap.SwapError, match="отправка отклонена"):
        swap._sign_and_send({"swapTransaction": base64.b64encode(b"x" * 8).decode()})


# ---------- подтверждение ----------
def test_подтверждение_успешное(monkeypatch):
    monkeypatch.setattr(swap.helius, "rpc", lambda *a, **k: {
        "result": {"value": [{"err": None, "confirmationStatus": "confirmed"}]}})
    assert swap.confirm("sig", timeout_s=5) is True


def test_транзакция_с_ошибкой_не_подтверждена(monkeypatch):
    monkeypatch.setattr(swap.helius, "rpc", lambda *a, **k: {
        "result": {"value": [{"err": {"InstructionError": [0, "Custom"]}}]}})
    assert swap.confirm("sig", timeout_s=5) is False


def test_таймаут_подтверждения_это_НЕУДАЧА(monkeypatch):
    """fail closed: нет ответа → считаем, что сделки не было. Иначе бот решит,
    что позиция открыта, когда её нет, и «продаст» пустоту."""
    monkeypatch.setattr(swap.helius, "rpc", lambda *a, **k: {"result": {"value": [None]}})
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    assert swap.confirm("sig", timeout_s=1) is False


def test_сбой_узла_при_подтверждении_не_роняет(monkeypatch):
    def boom(*a, **k):
        raise TimeoutError()

    monkeypatch.setattr(swap.helius, "rpc", boom)
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    assert swap.confirm("sig", timeout_s=1) is False


# ---------- dry-run: ничего не уходит в сеть ----------
def test_покупка_в_dry_run_только_план(monkeypatch):
    monkeypatch.setattr(swap.wallet, "Wallet", _W)
    monkeypatch.setattr(swap.market, "sol_price", lambda: 70.0)
    monkeypatch.setattr(execution, "quote", lambda *a, **k: {"outAmount": "1000000",
                                                                  "priceImpactPct": "0.01"})
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (0, 6, None))
    monkeypatch.setattr(swap.ledger, "record_intent", lambda *a, **k: "iid")
    sent = []
    monkeypatch.setattr(swap, "_sign_and_send", lambda *a: sent.append(1))
    r = swap.buy("MINT", 10)
    assert r["action"] == "dry_run" and not sent      # НИЧЕГО не отправлено
    assert r["tokens_expected"] == 1000000


def test_продажа_в_dry_run_только_план(monkeypatch):
    monkeypatch.setattr(swap.wallet, "Wallet", _W)
    monkeypatch.setattr(swap.market, "sol_price", lambda: 70.0)
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (1_000_000_000, 6, "ata"))
    monkeypatch.setattr(swap.helius, "rpc", lambda *a, **k: {
        "result": {"value": [{"account": {"data": {"parsed": {"info": {
            "tokenAmount": {"decimals": 6, "uiAmount": 1000.0}}}}}}]}})
    monkeypatch.setattr(execution, "quote", lambda *a, **k: {"outAmount": "100000000"})
    monkeypatch.setattr(swap.ledger, "record_intent", lambda *a, **k: "iid")
    sent = []
    monkeypatch.setattr(swap, "_sign_and_send", lambda *a: sent.append(1))
    r = swap.sell("MINT", 1.0)
    assert r["action"] == "dry_run" and not sent


def test_закрытие_аккаунта_с_остатком_запрещено(monkeypatch):
    """Закрыть аккаунт с токенами = потерять их. Проверка обязана быть до отправки."""
    monkeypatch.setattr(swap.wallet, "Wallet", _W)
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (5_000_000, 6, "ata"))
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    r = swap.close_token_account("MINT")
    assert r["action"] == "skip" and "минимальных единиц" in r["reason"]


def test_закрытие_несуществующего_аккаунта(monkeypatch):
    monkeypatch.setattr(swap.wallet, "Wallet", _W)
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (0, 0, None))
    assert swap.close_token_account("MINT")["action"] == "skip"
