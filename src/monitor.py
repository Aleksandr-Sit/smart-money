"""Контур 2 — оркестратор live-мониторинга (вход + ГИБРИДНЫЙ выход).

Вход: WS → parse_trade(buy) → движок конфлюенса → safety → доставка + открытие позиции.
Выход (гибрид): (1) actor-exit — зашедший актор продаёт трекаемый токен; (2) price — TP/SL/
trailing/dead (фоновый цикл каждые ~90с). Что сработает первым → EXIT + realized PnL.

Run:  .venv\\Scripts\\python.exe -m src.monitor [--max-mc 2000000] [--seconds N]
"""
from __future__ import annotations

import argparse
import asyncio
import time

from . import delivery, helius_ws, market, positions, safety, tx_parse
from .signal_engine import BuyEvent, SignalEngine, load_actor_map

PRICE_POLL_S = 90


def _split(items: list, n: int) -> list[list]:
    return [items[i::n] for i in range(min(n, len(items)))] if items else []


async def run(max_mc: float, seconds: int | None) -> None:
    amap = load_actor_map()
    wallets = list(amap.keys())
    engine = SignalEngine(amap)
    pm = positions.PositionManager()
    seen_sigs: set[str] = set()
    loop = asyncio.get_event_loop()

    async def emit_exit(token: str, exit_price: float, reason: str) -> None:
        p = pm.get(token)
        if not p:
            return
        await loop.run_in_executor(None, delivery.deliver_exit, p, exit_price, reason, True)
        r = (exit_price / p.entry_price - 1) if (p.entry_price and exit_price) else None
        print(f"[EXIT {reason}] {token} realized={r:+.0%}" if r is not None else f"[EXIT {reason}] {token}")
        pm.close(token)

    async def on_event(wallet: str, sig: str) -> None:
        if sig in seen_sigs:
            return
        seen_sigs.add(sig)
        if len(seen_sigs) > 100_000:
            seen_sigs.clear()
        trade = await loop.run_in_executor(None, tx_parse.parse_trade, sig, wallet)
        if not trade:
            return
        sol = await loop.run_in_executor(None, market.sol_price)
        token = trade["token_mint"]
        price = (trade["sol"] / trade["base_amount"] * sol) if trade.get("base_amount") else None

        # --- ПРОДАЖА: actor-exit ---
        if trade["side"] == "sell":
            actor = amap.get(wallet)
            if actor and pm.get(token):
                reason = pm.on_sell(token, actor[0])
                if reason:
                    await emit_exit(token, price or 0.0, reason)
            return

        # --- ПОКУПКА: конфлюенс → вход ---
        ev = BuyEvent(ts=trade.get("ts") or time.time(), token_mint=token,
                      wallet=wallet, usd=trade["sol"] * sol)
        signal = engine.process(ev)
        if not signal:
            return
        info = await loop.run_in_executor(None, market.token_info, token)
        if not info.get("price_usd") and price:                 # entry из он-чейн покупки
            info["price_usd"] = price
        if not info.get("mc") and price:
            info["mc"] = price * 1_000_000_000
        mc = info.get("mc")
        if mc and mc > max_mc:
            print(f"[skip late] {token} MC ${mc:,.0f} > {max_mc:,.0f}")
            return
        saf = await loop.run_in_executor(None, safety.screen, token)
        alert = signal.level == "strong" and saf.get("verdict") in ("ok", "warn")
        await loop.run_in_executor(None, delivery.deliver, signal, saf, info, True, alert)
        if saf.get("verdict") != "danger":
            pm.open(token, info.get("price_usd"), info.get("mc"), signal.actors, ev.ts)
        print(f"[SIGNAL {signal.level}] {token} n_actors={signal.n_actors} "
              f"MC=${(mc or 0):,.0f} safety={saf.get('verdict')} tg={alert} open={len(pm.open_tokens())}")

    async def price_watch() -> None:
        while True:
            await asyncio.sleep(PRICE_POLL_S)
            for token in pm.open_tokens():
                p = pm.get(token)
                if not p:
                    continue
                info = await loop.run_in_executor(None, market.token_info, token)
                cur = info.get("price_usd")
                age_h = (time.time() - p.entry_ts) / 3600
                reason = pm.check_price(token, cur, age_h)
                if reason:
                    await emit_exit(token, cur or 0.0, reason)

    batches = _split(wallets, 5)
    print(f"[monitor] {len(wallets)} кошельков, {len(batches)} WS-соединений, "
          f"live SOL=${market.sol_price():.2f}, max_MC=${max_mc:,.0f}, "
          f"открытых позиций={len(pm.open_tokens())}. Слушаю (вход+выход)...")
    tasks = [asyncio.create_task(helius_ws.subscribe_wallets(b, on_event, label=str(i)))
             for i, b in enumerate(batches)]
    tasks.append(asyncio.create_task(price_watch()))
    if seconds:
        await asyncio.sleep(seconds)
        for t in tasks:
            t.cancel()
        print(f"[monitor] стоп после {seconds}с (тест).")
    else:
        await asyncio.gather(*tasks)


def main() -> None:
    ap = argparse.ArgumentParser(description="Контур 2 монитор (вход+выход)")
    ap.add_argument("--max-mc", type=float, default=2_000_000,
                    help="early-MC чек: не слать сигнал, если MC токена уже выше")
    ap.add_argument("--seconds", type=int, default=None, help="остановиться через N сек (тест)")
    args = ap.parse_args()
    asyncio.run(run(args.max_mc, args.seconds))


if __name__ == "__main__":
    main()
