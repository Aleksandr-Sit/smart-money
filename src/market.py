"""Рыночные данные (DexScreener, free, без ключа): курс SOL + инфо по токену.

Закрывает v2-доводки: живой курс SOL (вместо статичного $170), velocity (число покупок),
строгий early-MC чек. Кэш курса SOL, чтобы не дёргать на каждой сделке.
"""
from __future__ import annotations

import json
import time

import requests

from . import config

WSOL = "So11111111111111111111111111111111111111112"
FALLBACK_SOL = 170.0
DS = "https://api.dexscreener.com/latest/dex/tokens"
_SOL_STATE = config.OUTPUT_DIR / "sol_price.json"


def _load_last_sol() -> float:
    """Последний ЖИВОЙ курс с диска (переживает рестарт) — лучше статичного FALLBACK."""
    try:
        return float(json.loads(_SOL_STATE.read_text(encoding="utf-8"))["price"])
    except Exception:  # noqa: BLE001
        return FALLBACK_SOL


_sol_cache = {"price": _load_last_sol(), "ts": 0.0}


def _best_pair(pairs: list[dict]) -> dict | None:
    pairs = [p for p in pairs if (p.get("liquidity") or {}).get("usd")]
    return max(pairs, key=lambda x: x["liquidity"]["usd"]) if pairs else None


def sol_price(max_age_s: int = 300) -> float:
    """Курс SOL/USD с кэшем (обновление раз в max_age_s). Fallback $170."""
    if time.time() - _sol_cache["ts"] < max_age_s:
        return _sol_cache["price"]
    try:
        r = requests.get(f"{DS}/{WSOL}", timeout=10).json()
        best, best_liq = None, 0.0
        for p in r.get("pairs") or []:
            if (p.get("quoteToken") or {}).get("symbol") in ("USDC", "USDT") and p.get("priceUsd"):
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if liq > best_liq:
                    best, best_liq = float(p["priceUsd"]), liq
        if best:
            _sol_cache.update(price=best, ts=time.time())
            try:                              # персист живого курса (fallback после рестарта)
                _SOL_STATE.write_text(json.dumps({"price": best, "ts": _sol_cache["ts"]}),
                                      encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return _sol_cache["price"]


def token_info(mint: str) -> dict:
    """DexScreener по токену: mc, price_usd, liquidity, buys/sells h1 (velocity), возраст."""
    try:
        r = requests.get(f"{DS}/{mint}", timeout=10).json()
        p = _best_pair(r.get("pairs") or [])
        if not p:
            return {}
        h1 = (p.get("txns") or {}).get("h1") or {}
        created = p.get("pairCreatedAt")
        age_s = (time.time() - created / 1000.0) if created else None
        return {
            "mc": p.get("marketCap") or p.get("fdv"),
            "price_usd": float(p["priceUsd"]) if p.get("priceUsd") else None,
            "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
            "buys_h1": h1.get("buys"), "sells_h1": h1.get("sells"),
            "vol_h1": (p.get("volume") or {}).get("h1"),
            "age_s": age_s,
        }
    except Exception:  # noqa: BLE001
        return {}


if __name__ == "__main__":
    import sys
    print("SOL price:", sol_price())
    for m in sys.argv[1:]:
        print(m, "->", token_info(m))
