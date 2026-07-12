"""Парсер транзакции: покупка ИЛИ продажа токена кошельком (по pre/post балансам).

parse_trade → {side:'buy'|'sell', token_mint, base_amount, sol, ts} или None.
  buy:  токен-баланс кошелька ВЫРОС, SOL убыл (sol = потрачено).
  sell: токен-баланс УПАЛ, SOL пришёл (sol = получено).
"""
from __future__ import annotations

from . import helius

WSOL = "So11111111111111111111111111111111111111112"


def _amt(b: dict) -> float:
    return (b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0


def parse_trade(signature: str, wallet: str) -> dict | None:
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
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in res["transaction"]["message"]["accountKeys"]]
    if wallet not in keys:
        return None
    idx = keys.index(wallet)
    sol_delta = (meta["postBalances"][idx] - meta["preBalances"][idx]) / 1e9  # + получил, − потратил

    pre = {b["mint"]: _amt(b) for b in (meta.get("preTokenBalances") or [])
           if b.get("owner") == wallet and b.get("mint") != WSOL}
    post = {b["mint"]: _amt(b) for b in (meta.get("postTokenBalances") or [])
            if b.get("owner") == wallet and b.get("mint") != WSOL}
    deltas = {m: post.get(m, 0.0) - pre.get(m, 0.0) for m in set(pre) | set(post)}
    deltas = {m: d for m, d in deltas.items() if abs(d) > 0}
    if not deltas:
        return None
    mint = max(deltas, key=lambda m: abs(deltas[m]))
    d = deltas[mint]
    ts = res.get("blockTime")
    if d > 0 and sol_delta < 0:      # покупка за SOL
        return {"side": "buy", "token_mint": mint, "base_amount": d, "sol": -sol_delta, "ts": ts}
    if d < 0 and sol_delta > 0:      # продажа за SOL
        return {"side": "sell", "token_mint": mint, "base_amount": -d, "sol": sol_delta, "ts": ts}
    return None


def parse_buy(signature: str, wallet: str) -> dict | None:
    """Совместимость: только покупки, в старом формате {token_mint, base_amount, sol_spent, ts}."""
    t = parse_trade(signature, wallet)
    if t and t["side"] == "buy":
        return {"token_mint": t["token_mint"], "base_amount": t["base_amount"],
                "sol_spent": t["sol"], "ts": t["ts"]}
    return None
