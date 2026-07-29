"""Тесты SHADOW-исполнения. Главное — модуль умеет ТОЛЬКО читать котировки."""
import inspect

import pytest

from src import execution

MINT = "FeSENori1vjgUYP63oPPeNJtjXeLpm9sht8UN2k4pump"


def test_модуль_не_умеет_отправлять_транзакции():
    """Инвариант безопасности Фазы B: никакой подписи/отправки в коде исполнения."""
    src = inspect.getsource(execution)
    for forbidden in ("sendTransaction", "signTransaction", "Keypair", "sign(",
                      "private_key", "secret_key"):
        assert forbidden not in src, f"в execution.py есть {forbidden} — это уже НЕ shadow"


def test_расчёт_фрикции_по_котировкам(monkeypatch):
    """Клип $20 при SOL=$100 → 2e8 лампортов; обратно приходит 96% → фрикция ровно 4%."""
    def fake_quote(inp, out, amount, slip, timeout=15):
        if inp == execution.WSOL:                      # покупка: SOL → токены
            return {"outAmount": "1000", "priceImpactPct": "0.01", "routePlan": []}
        return {"outAmount": str(int(0.96 * 2e8)),     # продажа: вернулось 96% вложенных лампортов
                "priceImpactPct": "0.02", "routePlan": []}
    monkeypatch.setattr(execution, "quote", fake_quote)
    monkeypatch.setattr(execution, "priority_fee_microlamports", lambda: 10000.0)
    r = execution.measure(MINT, clip_usd=20, sol_usd=100.0)   # 20$/100 = 0.2 SOL = 2e8 лампортов
    assert r["routable"] is True
    assert r["roundtrip_friction"] == pytest.approx(0.04, abs=1e-6)
    assert r["priority_fee_frac"] < 0.001          # приоритетка ничтожна на клипе $20
    assert r["total_cost"] == pytest.approx(r["roundtrip_friction"] + r["priority_fee_frac"])


def test_нет_маршрута_на_покупку(monkeypatch):
    monkeypatch.setattr(execution, "quote", lambda *a, **k: None)
    r = execution.measure(MINT, clip_usd=20, sol_usd=100.0)
    assert r["routable"] is False and "покупку" in r["error"]


def test_ловушка_ликвидности_купить_можно_продать_нельзя(monkeypatch):
    """Критичный риск-кейс: вход есть, выхода нет — должен фиксироваться явно."""
    def fake_quote(inp, out, amount, slip, timeout=15):
        return {"outAmount": "1000", "priceImpactPct": "0.01", "routePlan": []} \
            if inp == execution.WSOL else None
    monkeypatch.setattr(execution, "quote", fake_quote)
    r = execution.measure(MINT, clip_usd=20, sol_usd=100.0)
    assert r["routable"] is False and "ловушка" in r["error"]


def test_сетевой_сбой_не_роняет_замер(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("сеть")
    monkeypatch.setattr(execution.requests, "get", boom)
    assert execution.quote(execution.WSOL, MINT, 1000, 300) is None
