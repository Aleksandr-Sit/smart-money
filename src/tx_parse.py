"""Парсер транзакции: купил ли кошелёк токен (по pre/post балансам). Через Helius getTransaction.

Возвращает {token_mint, base_amount, sol_spent, ts} или None (если не покупка).
"""
from __future__ import annotations

from . import helius

WSOL = "So11111111111111111111111111111111111111112"


def parse_buy(signature: str, wallet: str) -> dict | None:
    try:
        res = helius.rpc("getTransaction", [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"},
        ]).get("result")
    except Exception:  # noqa: BLE001
        return None
    if not res or (res.get("meta") or {}).get("err"):
        return None
    meta = res["meta"]
    msg = res["transaction"]["message"]

    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
    if wallet not in keys:
        return None
    idx = keys.index(wallet)
    sol_spent = (meta["preBalances"][idx] - meta["postBalances"][idx]) / 1e9  # включает комиссию
    if sol_spent <= 0:
        return None  # SOL не убыл → не покупка за SOL

    def amt(b):
        return (b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0

    pre = {(b.get("owner"), b.get("mint")): amt(b) for b in (meta.get("preTokenBalances") or [])}
    best_mint, best_delta = None, 0.0
    for b in (meta.get("postTokenBalances") or []):
        if b.get("owner") != wallet or b.get("mint") == WSOL:
            continue
        delta = amt(b) - pre.get((wallet, b.get("mint")), 0.0)
        if delta > best_delta:
            best_delta, best_mint = delta, b.get("mint")

    if not best_mint or best_delta <= 0:
        return None
    return {"token_mint": best_mint, "base_amount": best_delta,
            "sol_spent": sol_spent, "ts": res.get("blockTime")}
