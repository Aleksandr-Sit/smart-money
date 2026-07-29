"""Тесты рыночных данных. Главное — РЕГРЕСС на баг аудита-4.

Баг: _best_pair не проверял, что baseToken пары = наш mint → DexScreener отдавал цену
ЧУЖОГО токена. Итог: 9 записей с невозможными ценами, одна +321645% = 81% суммы PnL.
"""
from src import market

MINT = "FeSENori1vjgUYP63oPPeNJtjXeLpm9sht8UN2k4pump"
OTHER = "So11111111111111111111111111111111111111112"


def _pair(base, liq, price):
    return {"baseToken": {"address": base}, "liquidity": {"usd": liq}, "priceUsd": price}


def test_best_pair_отбрасывает_пары_чужого_токена():
    """РЕГРЕСС аудита-4: пара с чужим baseToken не должна выбираться, даже если ликвиднее."""
    pairs = [_pair(OTHER, 5_000_000, "0.0388"),   # чужая, но ликвидность в 1000x больше
             _pair(MINT, 890, "0.0000068")]
    best = market._best_pair(pairs, MINT)
    assert best is not None
    assert best["baseToken"]["address"] == MINT
    assert best["priceUsd"] == "0.0000068"        # НАША цена, не чужая


def test_best_pair_возвращает_none_если_нашего_токена_нет():
    """Лучше нет цены, чем ЧУЖАЯ цена (именно она давала фиктивные +321645%)."""
    assert market._best_pair([_pair(OTHER, 5_000_000, "0.0388")], MINT) is None


def test_best_pair_выбирает_самую_ликвидную_из_наших():
    pairs = [_pair(MINT, 1_000, "0.0000010"), _pair(MINT, 50_000, "0.0000012")]
    assert market._best_pair(pairs, MINT)["liquidity"]["usd"] == 50_000


def test_best_pair_игнорирует_пары_без_ликвидности():
    pairs = [{"baseToken": {"address": MINT}, "priceUsd": "0.1"},   # нет liquidity
             _pair(MINT, 100, "0.0000010")]
    assert market._best_pair(pairs, MINT)["liquidity"]["usd"] == 100


def test_sol_price_имеет_разумный_фолбэк():
    """Курс SOL никогда не должен быть 0/None — на нём считаются все USD-объёмы."""
    p = market.sol_price()
    assert isinstance(p, float) and 1.0 < p < 100_000.0
