"""Тесты исполнительного ядра. Модуль тратит настоящие деньги — проверяем инварианты."""
import inspect

import pytest

from src import strategy, swap


def test_без_live_флага_ничего_не_отправляется(monkeypatch):
    """ГЛАВНЫЙ ИНВАРИАНТ: пока LIVE_ENABLED=false, отправка недопустима."""
    monkeypatch.setattr(swap.strategy, "EXECUTION", {**strategy.EXECUTION, "LIVE_ENABLED": False})
    monkeypatch.setattr(swap.wallet.Wallet, "available", property(lambda self: True))
    monkeypatch.setattr(swap.wallet.Wallet, "address", property(lambda self: "T" * 43))
    monkeypatch.setattr(swap.market, "sol_price", lambda: 100.0)
    monkeypatch.setattr(swap, "_quote", lambda *a: {"outAmount": "1000", "priceImpactPct": "0.01"})

    def boom(*a, **k):
        raise AssertionError("в dry-run отправка ЗАПРЕЩЕНА")
    monkeypatch.setattr(swap, "_sign_and_send", boom)
    monkeypatch.setattr(swap, "_build_swap_tx", boom)
    r = swap.buy("Mint" + "x" * 40, usd=10)
    assert r["action"] == "dry_run"


def test_потолок_суммы_не_обходится(monkeypatch):
    """Даже при вызове с мусорной суммой больше потолка не купим."""
    monkeypatch.setattr(swap.strategy, "EXECUTION", {**strategy.EXECUTION, "MAX_SWAP_USD": 25})
    with pytest.raises(swap.SwapError, match="выше потолка"):
        swap.buy("Mint" + "x" * 40, usd=10_000)


def test_провал_симуляции_блокирует_отправку(monkeypatch):
    """Jupiter симулирует ДО нас. Ошибка симуляции → отправлять нельзя."""
    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"swapTransaction": "abc", "simulationError": {"error": "InsufficientFunds"}}
    monkeypatch.setattr(swap.wallet.Wallet, "available", property(lambda self: True))
    monkeypatch.setattr(swap.wallet.Wallet, "address", property(lambda self: "T" * 43))
    monkeypatch.setattr(swap.requests, "post", lambda *a, **k: R())
    with pytest.raises(swap.SwapError, match="симуляц"):
        swap._build_swap_tx({"outAmount": "1"})


def test_нет_маршрута_не_торгуем(monkeypatch):
    from src import execution
    monkeypatch.setattr(execution, "quote", lambda *a, **k: None)
    with pytest.raises(swap.SwapError, match="нет маршрута"):
        swap._quote("A" * 43, "B" * 43, 1000)


def test_доля_продажи_валидируется(monkeypatch):
    monkeypatch.setattr(swap.wallet.Wallet, "available", property(lambda self: True))
    for bad in (0, -0.5, 1.5):
        with pytest.raises(swap.SwapError, match="доля"):
            swap.sell("Mint" + "x" * 40, fraction=bad)


def test_нулевой_баланс_не_продаём(monkeypatch):
    monkeypatch.setattr(swap.wallet.Wallet, "available", property(lambda self: True))
    monkeypatch.setattr(swap, "token_balance", lambda m: (0.0, None))
    assert swap.sell("Mint" + "x" * 40)["action"] == "skip"


def test_неподтверждённая_транзакция_это_неудача():
    """FAIL CLOSED: нет подтверждения → исключение, а не «наверное прошло»."""
    src = inspect.getsource(swap.buy) + inspect.getsource(swap.sell)
    assert "НЕ подтвердилась" in src and "raise SwapError" in src


def test_закрытие_аккаунта_только_при_нулевом_балансе(monkeypatch):
    """Закрывать аккаунт с токенами = потерять их."""
    monkeypatch.setattr(swap, "token_balance", lambda m: (5.0, "ATA" + "x" * 40))
    r = swap.close_token_account("Mint" + "x" * 40)
    assert r["action"] == "skip" and "токен" in r["reason"]


def test_live_требует_enforce_режима_риска():
    """Реальные деньги без жёстких лимитов = дневной стоп не остановит слив."""
    import yaml
    cfg = yaml.safe_load(open(strategy.PATH, encoding="utf-8"))
    cfg["execution"]["LIVE_ENABLED"] = True
    cfg["risk"]["RISK_MODE"] = "shadow"
    import tempfile
    import pathlib
    p = pathlib.Path(tempfile.mkdtemp()) / "bad.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="RISK_MODE=enforce"):
        strategy.load(p)


def test_потолок_свопа_не_больше_половины_банка():
    import yaml
    import pathlib
    import tempfile
    cfg = yaml.safe_load(open(strategy.PATH, encoding="utf-8"))
    cfg["execution"]["MAX_SWAP_USD"] = cfg["risk"]["BANKROLL_USD"]
    p = pathlib.Path(tempfile.mkdtemp()) / "bad2.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="половины банка"):
        strategy.load(p)
