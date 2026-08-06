"""Парсер транзакции: покупка ИЛИ продажа токена кошельком (по pre/post балансам).

parse_trade → {side, token_mint, base_amount, sol, usd_proceeds, fee, ts} или None.
  buy:  токен-баланс кошелька ВЫРОС, SOL убыл (sol = потрачено, брутто; fee отдельно).
  sell: токен-баланс УПАЛ, пришёл SOL (sol) ИЛИ стейбл (usd_proceeds) — раньше продажу
        за USDC/USDT молча теряли (sol_delta ≈ −fee), из-за чего actor-exit не срабатывал.

Важно: ветку ПОКУПКИ намеренно оставляем SOL-only (как во всей исторической выборке) —
чтобы не менять поток конфлюенса, на котором валидируется quiet-фильтр. Стейбл-финансируемые
ПОКУПКИ (редкие) по-прежнему не считаем; пересмотреть после окна OOS-валидации.
"""
from __future__ import annotations

from . import helius

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLES = {USDC, USDT}
QUOTE = {WSOL} | STABLES        # не считаем «торгуемым токеном»


def _amt(b: dict) -> float:
    return (b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0


def parse_trade(signature: str, wallet: str) -> dict | None:
    if not helius.budget_ok("getTransaction"):
        return None   # часовой бюджет исчерпан — лучше пропустить сигнал, чем ослепнуть
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
    fee_sol = (meta.get("fee") or 0) / 1e9
    sol_delta = (meta["postBalances"][idx] - meta["preBalances"][idx]) / 1e9  # + получил, − потратил

    # балансы кошелька: торгуемый токен (не quote) отдельно от стейблов
    def _bals(key, filt):
        return {b["mint"]: _amt(b) for b in (meta.get(key) or [])
                if b.get("owner") == wallet and filt(b["mint"])}

    pre_t, post_t = _bals("preTokenBalances", lambda m: m not in QUOTE), _bals("postTokenBalances", lambda m: m not in QUOTE)
    pre_s, post_s = _bals("preTokenBalances", lambda m: m in STABLES), _bals("postTokenBalances", lambda m: m in STABLES)
    stable_delta = sum(post_s.get(m, 0.0) for m in STABLES) - sum(pre_s.get(m, 0.0) for m in STABLES)

    deltas = {m: post_t.get(m, 0.0) - pre_t.get(m, 0.0) for m in set(pre_t) | set(post_t)}
    deltas = {m: d for m, d in deltas.items() if abs(d) > 0}
    if not deltas:
        return None
    mint = max(deltas, key=lambda m: abs(deltas[m]))
    d = deltas[mint]
    ts = res.get("blockTime")

    if d > 0 and sol_delta < 0:                       # покупка за SOL
        return {"side": "buy", "token_mint": mint, "base_amount": d, "sol": -sol_delta,
                "usd_proceeds": 0.0, "fee": fee_sol, "ts": ts}
    if d < 0 and sol_delta > 0:                       # продажа за SOL
        return {"side": "sell", "token_mint": mint, "base_amount": -d, "sol": sol_delta,
                "usd_proceeds": 0.0, "fee": fee_sol, "ts": ts}
    if d < 0 and stable_delta > 0:                    # продажа за USDC/USDT (раньше терялась)
        return {"side": "sell", "token_mint": mint, "base_amount": -d, "sol": 0.0,
                "usd_proceeds": stable_delta, "fee": fee_sol, "ts": ts}
    return None


def parse_buy(signature: str, wallet: str) -> dict | None:
    """Совместимость: только покупки, в старом формате {token_mint, base_amount, sol_spent, ts}."""
    t = parse_trade(signature, wallet)
    if t and t["side"] == "buy":
        return {"token_mint": t["token_mint"], "base_amount": t["base_amount"],
                "sol_spent": t["sol"], "ts": t["ts"]}
    return None
