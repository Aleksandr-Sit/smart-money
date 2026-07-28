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
from collections import deque

from . import delivery, helius, helius_ws, market, positions, price_track, safety, tx_parse
from .signal_engine import BuyEvent, SignalEngine, load_actor_map

HEARTBEAT_S = 6 * 3600
MAX_POSITIONS = 5      # лимит одновр. позиций (капитал-replay: 5 без потерь, 3 ≈ 18/19 хвостов)
SEEN_MAX = 100_000


def _split(items: list, n: int) -> list[list]:
    return [items[i::n] for i in range(min(n, len(items)))] if items else []


async def run(max_mc: float, seconds: int | None) -> None:
    amap = load_actor_map()
    wallets = list(amap.keys())
    engine = SignalEngine(amap)
    pm = positions.PositionManager()
    tracker = price_track.PriceTracker()
    seen_sigs: set[str] = set()
    seen_order: deque[str] = deque()      # FIFO-эвикция: не сбрасываем дедуп разом
    stats = {"signals": 0, "strong": 0, "quiet": 0, "alerts": 0, "opens": 0, "exits": 0,
             "started": time.time()}
    loop = asyncio.get_event_loop()

    async def emit_exit(token: str, exit_price: float, reason: str) -> None:
        p = pm.get(token)
        if not p:
            return
        await loop.run_in_executor(None, delivery.deliver_exit, p, exit_price, reason, True)
        r = (exit_price / p.entry_price - 1) if (p.entry_price and exit_price) else None
        print(f"[EXIT {reason}] {token} realized={r:+.0%}" if r is not None else f"[EXIT {reason}] {token}")
        stats["exits"] += 1
        pm.close(token)

    async def on_event(wallet: str, sig: str) -> None:
        if sig in seen_sigs:
            return
        seen_sigs.add(sig)
        seen_order.append(sig)
        if len(seen_order) > SEEN_MAX:        # выкидываем только самую старую
            seen_sigs.discard(seen_order.popleft())
        trade = await loop.run_in_executor(None, tx_parse.parse_trade, sig, wallet)
        if not trade:
            return
        sol = await loop.run_in_executor(None, market.sol_price)
        token = trade["token_mint"]
        # цена: продажа за стейбл → из usd_proceeds; иначе SOL-нога за вычетом fee (точность)
        if trade.get("base_amount"):
            if trade.get("usd_proceeds"):
                price = trade["usd_proceeds"] / trade["base_amount"]
            else:
                net_sol = max(0.0, trade["sol"] - trade.get("fee", 0.0))
                price = net_sol / trade["base_amount"] * sol
        else:
            price = None

        # --- ПРОДАЖА: actor-exit ---
        if trade["side"] == "sell":
            actor = amap.get(wallet)
            pos = pm.get(token)
            if actor and pos and actor[0] in pos.entry_actors and actor[0] not in pos.exited_actors:
                # лог КАЖДОЙ новой продажи зашедшего актора (до on_sell) — для оптимизации exit-правила
                await loop.run_in_executor(None, delivery.log_actor_sell,
                                           token, actor[0], price, trade.get("ts"), pos)
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
        # трекинг траектории для consumer-mode оценки (не зависит от жизни paper-позиции)
        try:
            tracker.register(token, info.get("price_usd"), ev.ts)
        except Exception as e:  # noqa: BLE001
            print(f"[track] register fail {token[:8]}: {type(e).__name__}")
        saf = await loop.run_in_executor(None, safety.screen, token)
        # Telegram — ТОЛЬКО лучший класс: strong + ТИХИЙ + safety ok/warn (ресерч: quiet +38.7% OOS).
        # Лог и paper пишут ВСЕ сигналы (нужен знаменатель + продолжение OOS по обеим группам).
        alert = signal.level == "strong" and signal.quiet and saf.get("verdict") in ("ok", "warn")
        await loop.run_in_executor(None, delivery.deliver, signal, saf, info, True, alert)
        stats["signals"] += 1
        if signal.level == "strong":
            stats["strong"] += 1
        if signal.quiet:
            stats["quiet"] += 1
        if alert:
            stats["alerts"] += 1
        # капитал-лимит: бот держит ≤MAX_POSITIONS слотов (replay: 3-5 = edge без потерь,
        # медиана удержания 5.9 мин → низкая конкуренция). Реализм: не открываем сверх лимита.
        at_cap = len(pm.open_tokens()) >= MAX_POSITIONS
        if saf.get("verdict") != "danger" and not at_cap:
            if pm.open(token, info.get("price_usd"), info.get("mc"), signal.actors, ev.ts):
                stats["opens"] += 1
        print(f"[SIGNAL {signal.level}{'/quiet' if signal.quiet else ''}] {token} "
              f"n_actors={signal.n_actors} usd=${signal.window_usd} MC=${(mc or 0):,.0f} "
              f"safety={saf.get('verdict')} tg={alert} open={len(pm.open_tokens())}"
              f"{' [CAP]' if at_cap else ''}")

    async def heartbeat() -> None:
        def _rpc_alive() -> str:
            try:                              # живость Helius RPC (баланс кредитов через RPC недоступен)
                r = helius.rpc("getHealth", [])
                return "ok" if r.get("result") == "ok" else "?"
            except Exception:  # noqa: BLE001
                return "нет связи"
        # пульс при старте — подтверждает, что новый деплой поднялся
        await loop.run_in_executor(None, delivery.send_heartbeat,
                                   f"монитор запущен · {len(wallets)} кош./{len(batches)} WS · "
                                   f"SOL=${market.sol_price():.2f} · открытых={len(pm.open_tokens())}")
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            up_h = (time.time() - stats["started"]) / 3600
            msg = (f"жив {up_h:.0f}ч · сигналов={stats['signals']} "
                   f"(strong={stats['strong']} тихих={stats['quiet']} алертов={stats['alerts']}) · "
                   f"входов={stats['opens']} выходов={stats['exits']} · открытых={len(pm.open_tokens())} · "
                   f"трек={len(tracker.active)} · SOL=${market.sol_price():.2f} · rpc={_rpc_alive()}")
            await loop.run_in_executor(None, delivery.send_heartbeat, msg)

    async def exit_tick(prices: dict) -> None:
        """Выходы на 15с-цикле трекера (было 90с — на такой гранулярности edge исчезал, см. аудит).
        prices — свежие цены с бондинг-кривой за этот тик; None = нет данных → dead по возрасту."""
        for token in pm.open_tokens():
            p = pm.get(token)
            if not p:
                continue
            cur = prices.get(token)
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
    tasks.append(asyncio.create_task(tracker.run(on_tick=exit_tick)))   # выходы на 15с-цикле трекера
    tasks.append(asyncio.create_task(heartbeat()))
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
