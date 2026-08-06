"""Контур 2 — оркестратор live-мониторинга (вход + ГИБРИДНЫЙ выход).

Вход: WS → parse_trade(buy) → движок конфлюенса → safety → доставка + открытие позиции.
Выход (гибрид): (1) actor-exit — зашедший актор продаёт трекаемый токен; (2) price — частичные
тейки + TP/SL/trailing/timeout на 15с-цикле трекера (на 90с edge исчезал, аудит-3).
Что сработает первым → PARTIAL или EXIT + realized PnL. Параметры — config/strategy.yaml.

Run:  .venv\\Scripts\\python.exe -m src.monitor [--max-mc 2000000] [--seconds N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import deque

from . import (config, delivery, execution, helius, helius_ws, ledger, market, positions, price_track,
               risk, safety, strategy, sweep, tx_parse)
from . import wallet as hot_wallet   # псевдоним: в on_event параметр называется wallet
from .signal_engine import BuyEvent, SignalEngine, load_actor_map

HEARTBEAT_S = 6 * 3600
MAX_POSITIONS = strategy.RISK["MAX_POSITIONS"]   # единый конфиг (капитал-replay: 5 без потерь)
SEEN_MAX = 100_000
MAX_EVENT_AGE_S = 300      # событие старше 5 мин = протухшее (backfill), не торгуем
SWEEP_POLL_S = 600         # как часто проверять, не пора ли выводить прибыль
PRIORITY = set(strategy.ENTRY["PRIORITY_ACTORS"])


def tradable(signal) -> tuple[bool, str]:
    """Правило входа (аудит-7). Три популяции сигналов, одна из них убыточна.

    strong без приоритетных  → mean +25.0%  торгуем
    weak С приоритетным      → mean +68.9%  торгуем (медиана +68%)
    weak без приоритетных    → mean −4.8%   ОТБРАСЫВАЕМ (убыток во всех фолдах)
    Проверено скользящей валидацией: +$2045 против +$1545 при 42% сделок.
    """
    if strategy.ENTRY["RULE"] == "all":
        return True, "all"
    has_priority = bool(PRIORITY & set(signal.actors))
    if signal.level == "strong":
        return True, "strong+приоритет" if has_priority else "strong"
    if has_priority:
        return True, "приоритетный актор"
    return False, "weak без приоритетных (убыточная когорта)"


def _split(items: list, n: int) -> list[list]:
    return [items[i::n] for i in range(min(n, len(items)))] if items else []


async def run(max_mc: float, seconds: int | None) -> None:
    amap = load_actor_map()
    wallets = list(amap.keys())
    engine = SignalEngine(amap)
    pm = positions.PositionManager()
    tracker = price_track.PriceTracker()
    rm = risk.RiskManager()          # Фаза C: дневной стоп / kill-switch / экспозиция
    # после рестарта вернуть открытые позиции под трекинг цен, иначе они «ослепнут»
    # и выйдут по таймауту с ценой 0 = фантомные −100% (аудит-6)
    for _tok in pm.open_tokens():
        try:
            tracker.register(_tok, None, renew=True)
        except Exception as e:  # noqa: BLE001
            print(f"[track] re-register fail {_tok[:8]}: {type(e).__name__}")
    if pm.open_tokens():
        print(f"[monitor] под трекинг возвращено позиций: {len(pm.open_tokens())}")
    try:
        actor_seen = json.loads((config.OUTPUT_DIR / "actor_activity.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        actor_seen = {}
    seen_sigs: set[str] = set()
    seen_order: deque[str] = deque()      # FIFO-эвикция: не сбрасываем дедуп разом
    pos_intent: dict[str, str] = {}       # token -> id намерения в леджере
    stats = {"signals": 0, "strong": 0, "quiet": 0, "alerts": 0, "opens": 0, "exits": 0,
             "skipped_unsellable": 0, "skipped_rule": 0,
             "started": time.time(), "last_signal_ts": time.time()}
    loop = asyncio.get_event_loop()

    def sane_price(token: str, price: float | None) -> float | None:
        """Санитарная проверка цены ИЗ СДЕЛКИ (аудит-5).

        Пылевая продажа (крошечный base_amount в знаменателе) даёт абсурдную цену:
        в проде поймано 2 случая по $76k–152k за токен → одна запись раздула mean PnL
        до +908 000 000%. Фильтр из Фазы A закрывал только траектории, этот путь — нет.
        Сверяем с последней известной хорошей ценой (трекер или вход позиции).
        """
        if not price or price <= 0:
            return None
        pos = pm.get(token)
        ref = tracker.last.get(token) or (pos.entry_price if pos else None)
        if ref and ref > 0:
            ratio = price / ref
            if ratio > tracker.SANITY_JUMP or ratio < 1 / tracker.SANITY_JUMP:
                tracker.anomalies += 1
                print(f"[trade] SKIP аномальная цена {token[:8]}: {price:.3e} "
                      f"(ref {ref:.3e}, {ratio:.1e}x) → берём ref")
                return ref            # опорная цена вместо мусорной
        return price

    def _fallback_price(token: str) -> float | None:
        """Резервный источник цены: котировка Jupiter на клип. None = маршрута нет."""
        try:
            q = execution.quote(execution.WSOL, token,
                                int(rm.clip / market.sol_price() * 1e9),
                                strategy.EXECUTION["SLIPPAGE_BPS"])
            if not q or not q.get("outAmount"):
                return None
            tokens = int(q["outAmount"])
            return (rm.clip / tokens) if tokens > 0 else None
        except Exception:  # noqa: BLE001
            return None

    async def shadow(token: str, phase: str) -> dict:
        """SHADOW-замер фрикции (только котировки). Фаза B + гейт продаваемости (аудит-6)."""
        if not strategy.EXECUTION["SHADOW_ENABLED"]:
            return {}
        try:
            r = await loop.run_in_executor(None, execution.measure_and_log, token, phase, None)
            if r.get("routable"):
                print(f"[SHADOW {phase}] {token[:12]} фрикция {r['roundtrip_friction']:+.2%} "
                      f"итого {r['total_cost']:.2%}")
            else:
                print(f"[SHADOW {phase}] {token[:12]} НЕ РОУТИТСЯ: {r.get('error')}")
            return r
        except Exception as e:  # noqa: BLE001
            print(f"[shadow] fail {token[:8]}: {type(e).__name__}")
            return {}

    async def emit_exit(token: str, exit_price: float | None, reason: str) -> None:
        p = pm.get(token)
        if not p:
            return
        stale = False
        if exit_price is None or exit_price <= 0:
            # цены нет — оцениваем остаток по ПОСЛЕДНЕЙ известной, а не списываем в ноль.
            # Ноль = утверждение «токен мёртв», которого мы не проверяли (аудит-6).
            exit_price = tracker.last.get(token) or p.entry_price
            stale = True
        # замер фрикции В МОМЕНТ ВЫХОДА — ловит тонкую книгу при дампе (стресс-кейс)
        await shadow(token, f"exit_{reason}")
        await loop.run_in_executor(None, delivery.deliver_exit, p, exit_price, reason, True)
        r = positions.total_realized(p, exit_price)   # с учётом частичных тейков
        print(f"[EXIT {reason}] {token} realized={r:+.0%} "
              f"(частичн {p.realized:+.0%}+ост {p.remaining:.2f}){' [ЦЕНА УСТАРЕЛА]' if stale else ''}")
        stats["exits"] += 1
        # леджер: у КАЖДОЙ ноги своё намерение (иначе slippage считался бы как PnL сделки —
        # баг найден при сведении статистики 05.08). position_id связывает вход и выход.
        pid = pos_intent.pop(token, None)
        net = r - strategy.RISK["EXIT_FEE"]

        def _log_sell() -> None:
            sid = ledger.record_intent("sell", token, exit_price, reason=reason, mode="paper",
                                       extra={"position_id": pid})
            ledger.record_fill(sid, token, exit_price, usd=rm.clip * (1 + net), mode="paper",
                               extra={"reason": reason, "realized_net": net, "position_id": pid,
                                      "price_stale": stale})
        await loop.run_in_executor(None, _log_sell)
        tripped = rm.on_close(net)
        if tripped:
            await loop.run_in_executor(None, delivery.send_alert,
                                       f"ТОРГОВЛЯ ОСТАНОВЛЕНА — {tripped['reason']}")
            print(f"[RISK] СТОП: {tripped['reason']}")
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
        # ПРОТУХШЕЕ СОБЫТИЕ: backfill после WS-реконнекта проигрывает старые сигнатуры.
        # Замерено (аудит-6): 11 из 9276 сигналов имели задержку >10 мин, максимум 10.6 ЧАСА.
        # Вход по такой цене в live = гарантированно плохая сделка.
        age = time.time() - (trade.get("ts") or time.time())
        if age > MAX_EVENT_AGE_S:
            print(f"[stale] пропуск события {sig[:10]}: возраст {age/60:.0f} мин")
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
        price = sane_price(token, price)     # защита от пылевых сделок (см. ниже)

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
        # Telegram — лучшие классы: strong + safety ok/warn + (ТИХИЙ | КАЧЕСТВО-моментум).
        # Аудит-3: quiet и quality — 2 непересекающиеся положительные оси (quiet mean +22%,
        # quality MC≥15k+vel≥40 win 58% mean +43%, обе робастны). Оба шлём в Telegram.
        quality = ((info.get("mc") or 0) >= strategy.ALERTS["QUALITY_MIN_MC"]
                   and (info.get("buys_h1") or 0) >= strategy.ALERTS["QUALITY_MIN_VELOCITY"])
        # ВАЖНО: раньше алерт требовал strong → сигналы приоритетного актора (они weak,
        # т.к. это 2 кошельковые группы одного оператора) НЕ доходили вовсе, а это лучшая
        # когорта (медиана +68%). Теперь алертим всё торгуемое с приемлемым safety.
        alert = (saf.get("verdict") in ("ok", "warn")
                 and (signal.quiet or quality or bool(PRIORITY & set(signal.actors))))
        await loop.run_in_executor(None, delivery.deliver, signal, saf, info, True, alert)
        stats["signals"] += 1
        stats["last_signal_ts"] = time.time()
        _touch_actors(signal.actors)      # для алерта «поток иссяк»
        if signal.level == "strong":
            stats["strong"] += 1
        if signal.quiet:
            stats["quiet"] += 1
        if alert:
            stats["alerts"] += 1
        # капитал-лимит: бот держит ≤MAX_POSITIONS слотов (replay: 3-5 = edge без потерь,
        # медиана удержания 5.9 мин → низкая конкуренция). Реализм: не открываем сверх лимита.
        ok_rule, rule_why = tradable(signal)
        if not ok_rule:
            stats["skipped_rule"] += 1
            print(f"[RULE] пропуск {token[:12]}: {rule_why}")
            return
        allowed, deny, blocked = rm.gate(len(pm.open_tokens()))
        at_cap = not allowed
        if blocked and allowed:
            print(f"[RISK shadow] в live было бы заблокировано: {deny}")
        if saf.get("verdict") != "danger" and allowed:
            # ГЕЙТ ПРОДАВАЕМОСТИ (аудит-6): котируем покупку И продажу ДО входа.
            # Замерено: 10 токенов ни разу не роутились на продажу = в live $100 застрявших
            # (40% банка $250). Цена проверки — 0.17с латентности; бумажный PnL этих токенов
            # был −$3, т.е. теряем почти ничего. Замер фрикции тут же и записывается.
            probe = await shadow(token, "entry")
            if probe and not probe.get("routable"):
                stats["skipped_unsellable"] += 1
                print(f"[GATE] пропуск {token[:12]}: {probe.get('error')}")
                return
            entry_price = info.get("price_usd")
            if pm.open(token, entry_price, info.get("mc"), signal.actors, ev.ts):
                stats["opens"] += 1
                def _log_buy() -> str:
                    bid = ledger.record_intent("buy", token, entry_price, reason="signal",
                                               mode="paper")
                    # position_id = id намерения на покупку; связывает обе ноги сделки
                    ledger.record_fill(bid, token, entry_price, usd=rm.clip, mode="paper",
                                       extra={"position_id": bid})
                    return bid
                pos_intent[token] = await loop.run_in_executor(None, _log_buy)
        print(f"[SIGNAL {signal.level}{'/quiet' if signal.quiet else ''}] {token} "
              f"n_actors={signal.n_actors} usd=${signal.window_usd} MC=${(mc or 0):,.0f} "
              f"safety={saf.get('verdict')} tg={alert} open={len(pm.open_tokens())}"
              f"{(' [БЛОК: ' + deny + ']') if at_cap else ''}")

    def _touch_actors(actors) -> None:
        """Отметить активность акторов (для алерта молчания). Персист переживает рестарт."""
        now = time.time()
        for a in actors:
            actor_seen[a] = now
        try:
            (config.OUTPUT_DIR / "actor_activity.json").write_text(
                json.dumps(actor_seen), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _silence_problems() -> list[str]:
        """Проверка свежести watchlist: молчащие приоритетные + возраст списка."""
        probs = []
        now = time.time()
        limit = strategy.ALERTS["PRIORITY_SILENCE_H"] * 3600
        for a in PRIORITY:
            seen = actor_seen.get(a)
            if seen is None:
                probs.append(f"приоритетный актор {a[:10]}… не сигналил НИ РАЗУ за наблюдение")
            elif now - seen > limit:
                probs.append(f"приоритетный актор {a[:10]}… молчит "
                             f"{(now - seen)/3600:.0f}ч — лучшая когорта под угрозой")
        try:
            wl = config.OUTPUT_DIR / "flow_watchlist.json"
            age_d = (now - wl.stat().st_mtime) / 86400
            if age_d > strategy.ALERTS["WATCHLIST_MAX_AGE_D"]:
                probs.append(f"watchlist не обновлялся {age_d:.0f} дней — "
                             f"перезапустить discovery (рынок меняется быстро)")
        except Exception:  # noqa: BLE001
            pass
        return probs

    ws_state = {"failing": set(), "alerted": False}

    async def on_ws_fail(label: str, attempt: int, err: str) -> None:
        """Алерт при отказе WS-подписок. Инцидент 06.08: Helius 429 ослепил бота на час,
        а узнали мы об этом от владельца — пульс раз в 6ч слишком медленный для торговли."""
        if attempt == 0:
            ws_state["failing"].discard(label)
            if not ws_state["failing"] and ws_state["alerted"]:
                ws_state["alerted"] = False
                await loop.run_in_executor(None, delivery.send_alert,
                                           "WS-подписки ВОССТАНОВЛЕНЫ — сигналы снова идут")
            return
        ws_state["failing"].add(label)
        # алертим, когда сыпется БОЛЬШИНСТВО соединений (единичный обрыв — норма, ~20/сутки)
        if len(ws_state["failing"]) >= 3 and attempt >= 2 and not ws_state["alerted"]:
            ws_state["alerted"] = True
            await loop.run_in_executor(
                None, delivery.send_alert,
                f"БОТ ОСЛЕП: {len(ws_state['failing'])} из {len(batches)} WS-подписок не "
                f"подключаются (попытка #{attempt}). Сигналы НЕ поступают.\n{err}\n"
                f"Если это 429 — Helius ограничил доступ, ждём снятия автоматически.")

    async def sweep_loop() -> None:
        """Периодический вывод прибыли на холодный кошелёк (Фаза D).
        Выключен по умолчанию; kill-switch НЕ блокирует — убрать деньги со стола безопасно."""
        while True:
            await asyncio.sleep(SWEEP_POLL_S)
            if not strategy.SWEEP["ENABLED"]:
                continue
            try:
                r = await loop.run_in_executor(None, sweep.execute)
                if r.get("action") in ("sent", "dry_run"):
                    print(f"[SWEEP {r['action']}] ${r.get('amount_usd', 0):.2f} → "
                          f"{str(r.get('destination'))[:8]}…")
            except Exception as e:  # noqa: BLE001
                print(f"[sweep] fail: {type(e).__name__}: {e}")
                await loop.run_in_executor(None, delivery.send_alert,
                                           f"ВЫВОД НЕ УДАЛСЯ: {type(e).__name__}: {str(e)[:100]}")

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
        prev_anom = 0
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            up_h = (time.time() - stats["started"]) / 3600
            msg = (f"жив {up_h:.0f}ч · v{strategy.VERSION} · сигналов={stats['signals']} "
                   f"(strong={stats['strong']} тихих={stats['quiet']} алертов={stats['alerts']}) · "
                   f"входов={stats['opens']} выходов={stats['exits']} · открытых={len(pm.open_tokens())} · "
                   f"трек={len(tracker.active)} · аном={tracker.anomalies} · "
                   f"непродаваемых={stats['skipped_unsellable']} правилом={stats['skipped_rule']} · "
                   f"SOL=${market.sol_price():.2f} · rpc={_rpc_alive()}\n"
                   f"РИСК: {rm.status()}\n{ledger.summary()}\n"
                   f"КОШЕЛЁК: {hot_wallet.status()}\n{sweep.status()}")
            await loop.run_in_executor(None, delivery.send_heartbeat, msg)

            # --- контроль качества данных и потока (аудит-4) ---
            problems = []
            new_anom = tracker.anomalies - prev_anom
            prev_anom = tracker.anomalies
            if new_anom > strategy.ALERTS["MAX_ANOMALY_RATE"]:
                problems.append(f"аномалий цены за период: {new_anom} — источник врёт?")
            silence_h = (time.time() - stats["last_signal_ts"]) / 3600
            if silence_h > strategy.ALERTS["STALE_SIGNAL_H"]:
                problems.append(f"нет сигналов {silence_h:.1f}ч — поток иссяк / WS молчит?")
            if rm.state.halted:
                problems.append(f"ТОРГОВЛЯ ОСТАНОВЛЕНА: {rm.state.halt_reason}")
            lg = ledger.reconcile()
            if not lg["ok"]:
                problems.append(f"леджер: {lg['orphan_fills']} исполнений без намерений!")
            problems += _silence_problems()
            if tracker.rpc_fails > 0:
                problems.append(f"RPC-сбоев трекера: {tracker.rpc_fails}")
                tracker.rpc_fails = 0
            if problems:
                await loop.run_in_executor(None, delivery.send_alert, " · ".join(problems))

    async def exit_tick(prices: dict) -> None:
        """Выходы на 15с-цикле трекера (было 90с — на такой гранулярности edge исчезал, см. аудит).
        prices — свежие цены с бондинг-кривой за этот тик; None = нет данных → dead по возрасту."""
        for token in pm.open_tokens():
            p = pm.get(token)
            if not p:
                continue
            cur = prices.get(token)
            age_h = (time.time() - p.entry_ts) / 3600
            if cur is None:
                # трекер цену не дал — НЕ считаем токен мёртвым вслепую: спрашиваем Jupiter.
                # Списание в 0 = −100% (аудит-6) искажает статистику, а в live означало бы
                # «закрыли позицию», пока токены реально лежат в кошельке.
                cur = await loop.run_in_executor(None, _fallback_price, token)
            res = pm.check_price(token, cur, age_h)
            if not res:
                continue
            if res["action"] == "partial":        # частичный тейк — позиция продолжается
                await loop.run_in_executor(None, delivery.log_partial, p, cur, res["frac"])
                print(f"[PARTIAL {res['reason']}] {token} frac={res['frac']:.2f} rem={p.remaining:.2f}")
            else:
                if cur is None:
                    # цену не дал ни трекер, ни Jupiter → позиция ПОТЕРЯНА, а не обнулена
                    await emit_exit(token, None, "lost_price")
                    await loop.run_in_executor(
                        None, delivery.send_alert,
                        f"позиция {token[:12]} закрыта БЕЗ цены (ни трекер, ни Jupiter) — "
                        f"в live проверить кошелёк вручную")
                else:
                    await emit_exit(token, cur, res["reason"])

    batches = _split(wallets, strategy.TRACKING["WS_CONNECTIONS"])
    print(f"[monitor] {len(wallets)} кошельков, {len(batches)} WS-соединений, "
          f"live SOL=${market.sol_price():.2f}, max_MC=${max_mc:,.0f}, "
          f"открытых позиций={len(pm.open_tokens())}. Слушаю (вход+выход)...")
    tasks = [asyncio.create_task(helius_ws.subscribe_wallets(b, on_event, label=str(i),
                                                             on_fail=on_ws_fail))
             for i, b in enumerate(batches)]
    tasks.append(asyncio.create_task(tracker.run(on_tick=exit_tick)))   # выходы на 15с-цикле трекера
    tasks.append(asyncio.create_task(heartbeat()))
    tasks.append(asyncio.create_task(sweep_loop()))
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
