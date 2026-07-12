"""Контур 2 — оркестратор live-мониторинга (Module F+G+H+I).

WS(Helius) → парс tx → движок конфлюенса → safety → доставка (Telegram+PAPER).
Долгоживущий процесс (Docker на VPS). Дедуп сигнатур и повторных сигналов.

Run:  .venv\\Scripts\\python.exe -m src.monitor [--seconds N] [--sol-usd 170]
"""
from __future__ import annotations

import argparse
import asyncio
import time

from . import delivery, helius_ws, market, safety, tx_parse
from .signal_engine import BuyEvent, SignalEngine, load_actor_map


def _split(items: list, n: int) -> list[list]:
    return [items[i::n] for i in range(min(n, len(items)))] if items else []


async def run(max_mc: float, seconds: int | None) -> None:
    amap = load_actor_map()
    wallets = list(amap.keys())
    engine = SignalEngine(amap)
    seen_sigs: set[str] = set()
    loop = asyncio.get_event_loop()

    async def on_event(wallet: str, sig: str) -> None:
        if sig in seen_sigs:
            return
        seen_sigs.add(sig)
        if len(seen_sigs) > 100_000:
            seen_sigs.clear()
        buy = await loop.run_in_executor(None, tx_parse.parse_buy, sig, wallet)
        if not buy:
            return
        sol = await loop.run_in_executor(None, market.sol_price)   # живой курс (кэш 5 мин)
        ev = BuyEvent(ts=buy.get("ts") or time.time(), token_mint=buy["token_mint"],
                      wallet=wallet, usd=buy["sol_spent"] * sol)
        signal = engine.process(ev)
        if not signal:
            return
        info = await loop.run_in_executor(None, market.token_info, signal.token_mint)
        # entry price/MC из ОН-ЧЕЙН покупки (DexScreener не знает совсем свежие токены)
        oc_price = (buy["sol_spent"] / buy["base_amount"] * sol) if buy.get("base_amount") else None
        if not info.get("price_usd") and oc_price:
            info["price_usd"] = oc_price
        if not info.get("mc") and oc_price:
            info["mc"] = oc_price * 1_000_000_000        # pump.fun supply 1e9
        mc = info.get("mc")
        if mc and mc > max_mc:                            # early-MC чек: уже не ранний
            print(f"[skip late] {signal.token_mint} MC ${mc:,.0f} > {max_mc:,.0f}")
            return
        saf = await loop.run_in_executor(None, safety.screen, signal.token_mint)
        # Telegram только для сильных и не-danger; всё пишем в signals.log в любом случае
        alert = signal.level == "strong" and saf.get("verdict") in ("ok", "warn")
        await loop.run_in_executor(None, delivery.deliver, signal, saf, info, True, alert)
        print(f"[SIGNAL {signal.level}] {signal.token_mint} n_actors={signal.n_actors} "
              f"MC=${(mc or 0):,.0f} velocity={info.get('buys_h1')} safety={saf.get('verdict')} tg={alert}")

    batches = _split(wallets, 5)
    print(f"[monitor] {len(wallets)} кошельков / {len(amap)} актор-весов, "
          f"{len(batches)} WS-соединений, live SOL=${market.sol_price():.2f}, max_MC=${max_mc:,.0f}. Слушаю...")
    tasks = [asyncio.create_task(helius_ws.subscribe_wallets(b, on_event, label=str(i)))
             for i, b in enumerate(batches)]
    if seconds:
        await asyncio.sleep(seconds)
        for t in tasks:
            t.cancel()
        print(f"[monitor] стоп после {seconds}с (тест).")
    else:
        await asyncio.gather(*tasks)


def main() -> None:
    ap = argparse.ArgumentParser(description="Контур 2 монитор")
    ap.add_argument("--max-mc", type=float, default=2_000_000,
                    help="early-MC чек: не слать сигнал, если MC токена уже выше (не ранний)")
    ap.add_argument("--seconds", type=int, default=None, help="остановиться через N сек (для теста)")
    args = ap.parse_args()
    asyncio.run(run(args.max_mc, args.seconds))


if __name__ == "__main__":
    main()
