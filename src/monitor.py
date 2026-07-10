"""Контур 2 — оркестратор live-мониторинга (Module F+G+H+I).

WS(Helius) → парс tx → движок конфлюенса → safety → доставка (Telegram+PAPER).
Долгоживущий процесс (Docker на VPS). Дедуп сигнатур и повторных сигналов.

Run:  .venv\\Scripts\\python.exe -m src.monitor [--seconds N] [--sol-usd 170]
"""
from __future__ import annotations

import argparse
import asyncio
import time

from . import delivery, helius_ws, safety, tx_parse
from .signal_engine import BuyEvent, SignalEngine, load_actor_map


def _split(items: list, n: int) -> list[list]:
    return [items[i::n] for i in range(min(n, len(items)))] if items else []


async def run(sol_usd: float, seconds: int | None) -> None:
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
        ev = BuyEvent(ts=buy.get("ts") or time.time(), token_mint=buy["token_mint"],
                      wallet=wallet, usd=buy["sol_spent"] * sol_usd)
        signal = engine.process(ev)
        if not signal:
            return
        saf = await loop.run_in_executor(None, safety.screen, signal.token_mint)
        await loop.run_in_executor(None, delivery.deliver, signal, saf, None, True)
        print(f"[SIGNAL {signal.level}] {signal.token_mint} n_actors={signal.n_actors} "
              f"usd=${signal.window_usd} safety={saf.get('verdict')}")

    batches = _split(wallets, 5)
    print(f"[monitor] {len(wallets)} кошельков / {len(amap)} актор-весов, "
          f"{len(batches)} WS-соединений, SOL=${sol_usd}. Слушаю...")
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
    ap.add_argument("--sol-usd", type=float, default=170.0, help="прибл. цена SOL для оценки размера")
    ap.add_argument("--seconds", type=int, default=None, help="остановиться через N сек (для теста)")
    args = ap.parse_args()
    asyncio.run(run(args.sol_usd, args.seconds))


if __name__ == "__main__":
    main()
