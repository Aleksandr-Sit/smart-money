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
from collections import Counter, deque

from . import (config, delivery, execution, helius, helius_ws, ledger, market, orphans, positions,
               price_track, log_parse, risk, safety, strategy, swap, sweep, tx_parse)
from . import wallet as hot_wallet   # псевдоним: в on_event параметр называется wallet
from .signal_engine import BuyEvent, SignalEngine, load_actor_map

HEARTBEAT_S = 6 * 3600
MAX_POSITIONS = strategy.RISK["MAX_POSITIONS"]   # единый конфиг (капитал-replay: 5 без потерь)
SEEN_MAX = 100_000
MAX_EVENT_AGE_S = 300      # событие старше 5 мин = протухшее (backfill), не торгуем
SLOT_S = 0.4               # длительность слота Solana — переводит отставание слотов в секунды
SWEEP_POLL_S = 600         # как часто проверять, не пора ли выводить прибыль
PRIORITY = set(strategy.ENTRY["PRIORITY_ACTORS"])


def tradable(signal) -> tuple[bool, str]:
    """Правило входа. Сейчас "all" — торгуем каждый сигнал (аудит 07.08).

    Правило "strong_or_priority" действовало до 07.08 и опиралось на вывод аудита-7,
    что когорта «weak без приоритетных» убыточна (mean −4.8%). Замер на 5541 РЕАЛЬНОЙ
    сделке этого не подтвердил: отвергаемые 4761 сделка дали +6075$ и были
    положительны во всех четырёх отрезках периода. Прибыль худшего качества — да
    (win 0.46, медиана −2.0%), но не убыток.

    A/B при отключённых стопе и тейке:
        all                        5541 сделок  win 0.44  мед  −5.4%  +8545$
        приоритетные ИЛИ 500–3000$ 1469 сделок  win 0.64  мед +28.8%  +6839$
        strong_or_priority          890 сделок  win 0.76  мед +51.5%  +5602$

    Цена решения — нагрузка: 213 сделок в сутки против 34. Договорённость с
    владельцем: пересматривать каждые 72 часа, при ухудшении откатываться на
    "приоритетные ИЛИ объём 500–3000$" (пока не вынесено в конфиг как отдельный режим).
    """
    if strategy.ENTRY["RULE"] == "all":
        return True, "all"
    has_priority = bool(PRIORITY & set(signal.actors))
    if signal.level == "strong":
        return True, "strong+приоритет" if has_priority else "strong"
    if has_priority:
        return True, "приоритетный актор"
    return False, "weak без приоритетных (убыточная когорта)"


def sanitize_price(price: float | None, ref: float | None, jump: float) -> tuple[float | None, bool]:
    """Отсеять невозможную цену. → (цена_к_использованию, была_ли_аномалия).

    Вынесена из замыкания намеренно: это единственная защита от мусора источника
    на путях «сделка» и «запасная котировка», и она обязана быть под тестом.

    При аномалии возвращаем ОПОРНУЮ цену, а не None: позиция должна продолжать
    оцениваться по здравому значению. Вернуть None означало бы «цены нет», и выход
    сработал бы вслепую — ровно та ошибка, что стоила −100% на выпуске токена (08.08).
    """
    if not price or price <= 0:
        return None, False
    if ref and ref > 0:
        ratio = price / ref
        if ratio > jump or ratio < 1 / jump:
            return ref, True
    return price, False


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
             "skipped_unsellable": 0, "skipped_rule": 0, "from_logs": 0, "from_rpc": 0,
             "stale_slots": 0,
             "started": time.time(), "last_signal_ts": time.time()}
    loop = asyncio.get_event_loop()
    # Аномалии цены по источнику. Одно число на все пути было бесполезно: у каждого своя
    # нормальная частота, и общий порог либо молчит на поломке, либо кричит на норме.
    anom_src: Counter = Counter()

    def sane_price(token: str, price: float | None, src: str = "trade") -> float | None:
        """Санитарная проверка цены. Применяется на ВСЕХ путях, кроме трекера,
        у которого свой такой же фильтр.

        Путь "trade" (аудит-5): пылевая продажа с крошечным base_amount в знаменателе
        даёт абсурдную цену — в проде поймано 2 случая по $76k–152k за токен, одна
        запись раздула mean PnL до +908 000 000%.

        Путь "fallback" (08.08): при выпуске токена на DEX фильтр трекера отбрасывает
        тик, монитор идёт за котировкой Jupiter, а та в момент миграции пула отдаёт
        мусор. Проверки здесь не было — мусор заходил через эту дверь и закрывал
        позицию с −100% ровно на лучших токенах.

        Сверяем с последней известной хорошей ценой (трекер или вход позиции).
        При аномалии возвращаем опорную цену, а не None: позиция должна продолжать
        оцениваться по здравому значению, а не закрываться вслепую.
        """
        pos = pm.get(token)
        ref = tracker.last.get(token) or (pos.entry_price if pos else None)
        out, anomaly = sanitize_price(price, ref, tracker.SANITY_JUMP)
        if anomaly:
            tracker.anomalies += 1
            anom_src[src] += 1        # разбивка по источнику: у путей разная нормальная частота
            print(f"[{src}] SKIP аномальная цена {token[:8]}: {price:.3e} "
                  f"(ref {ref:.3e}, {price/ref:.1e}x) → берём ref")
        return out

    def _fallback_price(token: str) -> float | None:
        """Резервный источник цены: котировка Jupiter на клип. None = маршрута нет.

        Результат ОБЯЗАТЕЛЬНО через sane_price. Санитарная проверка стояла в трекере
        и в разборе сделок, но не здесь — и мусор заходил через эту дверь (08.08).

        Разбор случая CgWJ5NBN…: токен выпустился с бондинг-кривой на DEX, чтение
        кривой стало бессмысленным, фильтр трекера этот тик корректно отбросил — и
        тем самым отправил монитор сюда. Jupiter в момент миграции пула отдал
        котировку, дающую цену 4.7e-11 при последней честной 2.8e-05. Трейлинг был
        взведён (пик 3.03x) и закрыл позицию с −100%. Через 30 секунд реальная цена
        была 5.07x от входа.

        Выпуск на DEX проходят 16.2% токенов — по определению лучшие из наших.
        """
        try:
            q = execution.quote(execution.WSOL, token,
                                int(rm.clip / market.sol_price() * 1e9),
                                strategy.EXECUTION["SLIPPAGE_BPS"])
            if not q or not q.get("outAmount"):
                return None
            tokens = int(q["outAmount"])
            return sane_price(token, (rm.clip / tokens) if tokens > 0 else None, "fallback")
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

    # ---- слой исполнения: paper и live идут ОДНИМ путём решений ----------------
    # Решение о входе/выходе принимается одинаково; различается только то, уходит ли
    # сделка в сеть. Асимметрия здесь опаснее всего: если live-ветка думает иначе,
    # чем paper, то весь накопленный бумажный результат перестаёт что-либо значить.
    LIVE = bool(strategy.EXECUTION["LIVE_ENABLED"])

    async def do_buy(token: str, price: float | None) -> str | None:
        """Открыть позицию физически. None = не купили (в live позиции быть не должно)."""
        if not LIVE:
            def _paper() -> str:
                bid = ledger.record_intent("buy", token, price, reason="signal", mode="paper")
                # position_id = id намерения на покупку; связывает обе ноги сделки
                ledger.record_fill(bid, token, price, usd=rm.clip, mode="paper",
                                   extra={"position_id": bid})
                return bid
            return await loop.run_in_executor(None, _paper)
        try:
            r = await loop.run_in_executor(None, swap.buy, token, rm.clip)
        except Exception as e:  # noqa: BLE001
            # fail closed: не купили — значит позиции нет, и делать вид, что есть, нельзя
            print(f"[LIVE BUY FAIL] {token[:12]}: {type(e).__name__}: {str(e)[:120]}")
            await loop.run_in_executor(None, delivery.send_alert,
                                       f"ПОКУПКА НЕ ПРОШЛА {token[:12]}: {str(e)[:150]}")
            return None
        return r.get("intent")

    async def do_sell(token: str, fraction: float, price: float | None,
                      reason: str, pid: str | None,
                      net: float | None = None) -> tuple[bool, float | None]:
        """Продать долю позиции. → (получилось, фактическая выручка в долларах).

        Выручка нужна для ЧЕСТНОГО учёта: результат сделки считается по деньгам,
        а не по цене трекера (см. _emit_exit). None — выручку прочитать не удалось.
        """
        if not LIVE:
            def _paper() -> None:
                sid = ledger.record_intent("sell", token, price, reason=reason, mode="paper",
                                           extra={"position_id": pid, "fraction": fraction})
                ledger.record_fill(sid, token, price,
                                   usd=rm.clip * fraction * (1 + (net if net is not None else 0.0)),
                                   mode="paper",
                                   extra={"reason": reason, "realized_net": net,
                                          "position_id": pid, "fraction": fraction})
            await loop.run_in_executor(None, _paper)
            return True, None
        try:
            r = await loop.run_in_executor(None, swap.sell, token, fraction, reason)
        except Exception as e:  # noqa: BLE001
            # токен остался у нас — это НЕ тихая ошибка, нужен человек
            print(f"[LIVE SELL FAIL] {token[:12]}: {type(e).__name__}: {str(e)[:120]}")
            await loop.run_in_executor(None, delivery.send_alert,
                                       f"ПРОДАЖА НЕ ПРОШЛА {token[:12]} ({reason}): "
                                       f"{str(e)[:150]} — токен ещё в кошельке, проверить вручную")
            return False, None
        return True, (r or {}).get("usd_actual")

    exiting: set[str] = set()          # токены, по которым выход уже выполняется
    entering: dict[str, str] = {}      # токен → отложенная причина выхода, пока идёт покупка

    async def emit_exit(token: str, exit_price: float | None, reason: str) -> None:
        """Защёлка от ДВОЙНОГО закрытия одной позиции (найдено аудитом 08.08).

        ВТОРАЯ ЗАЩЁЛКА — ВХОД (найдена первым же часом живой торговли 10.08). Слот
        резервируется вызовом pm.open() ДО отправки покупки, а покупка идёт несколько
        секунд: котировка, сборка, отправка, подтверждение, расчёт баланса. Если в это
        окно приходит продажа актора, выход видит открытую позицию и пытается продать
        то, чего ещё нет:
            [LIVE BUY FAIL]  6AiM14f1vDuS: simulation failed
            [LIVE SELL FAIL] 6AiM14f1vDuS: токен остаётся у нас
        Опаснее обратный случай: покупка УСПЕШНА, выход отработал в её окне, позиция
        закрылась раньше, чем бот о ней узнал, — и токены остались сиротой.

        Выход при этом НЕ теряем: запоминаем причину и исполняем сразу после покупки.
        Терять нельзя — продажа актора закрывает 86% позиций.
        """
        if token in entering:
            entering[token] = reason
            print(f"[EXIT отложен] {token[:12]} покупка ещё идёт ({reason}) — "
                  f"выйдем сразу после неё")
            return
        await _guarded_exit(token, exit_price, reason)

    async def _guarded_exit(token: str, exit_price: float | None, reason: str) -> None:
        """Защёлка от ДВОЙНОГО закрытия одной позиции (найдено аудитом 08.08).

        Гонка: две продажи разных акторов приходят почти одновременно. Первая даёт
        actors_exit, начинается выход — и на первом же await (замер фрикции) отдаёт
        управление. Вторая успевает обработаться, порог снова достигнут, запускается
        второй выход. Оба видят позицию открытой, потому что pm.close() в самом конце.

        Замер: 77 позиций закрыты дважды с 12 июля, у 99% разрыв меньше секунды
        (медиана 42 мс). Последствия: двойной учёт в paper_closed и, что важнее,
        двойной убыток в risk_state.realized_usd — дневной стоп срабатывал раньше
        срока. В live это была бы ВТОРАЯ команда на продажу той же позиции.

        Закрывать позицию в начале вместо конца нельзя: это сломает откат при
        неудачной продаже в live (do_sell вернул False → позиция остаётся нашей).
        """
        if token in exiting:
            print(f"[EXIT dup] {token[:12]} уже закрывается ({reason}) — событие пропущено")
            return
        exiting.add(token)
        try:
            await _emit_exit(token, exit_price, reason)
        finally:
            exiting.discard(token)

    async def _emit_exit(token: str, exit_price: float | None, reason: str) -> None:
        p = pm.get(token)
        if not p:
            return
        stale = False
        if exit_price is None or exit_price <= 0:
            # цены нет — оцениваем остаток по ПОСЛЕДНЕЙ известной, а не списываем в ноль.
            # Ноль = утверждение «токен мёртв», которого мы не проверяли (аудит-6).
            exit_price = tracker.last.get(token) or p.entry_price
            stale = True
        r = positions.total_realized(p, exit_price)   # с учётом частичных тейков
        # леджер: у КАЖДОЙ ноги своё намерение (иначе slippage считался бы как PnL сделки —
        # баг найден при сведении статистики 05.08). position_id связывает вход и выход.
        pid = pos_intent.pop(token, None)
        net = r - strategy.RISK["EXIT_FEE"]
        # ПРОДАЁМ ПЕРВЫМ ДЕЛОМ: сообщать о выходе, которого не случилось, нельзя.
        # fraction=1.0 — это ВЕСЬ остаток на кошельке; p.remaining измеряется от
        # исходной позиции и после частичного тейка означает другое число.
        # SHADOW-ЗАМЕР УБРАН ОТСЮДА (10.08): две котировки Jupiter ради статистики
        # стояли ПЕРЕД продажей и задерживали её. В живом режиме они и не нужны —
        # настоящее проскальзывание теперь измеряется по деньгам.
        sold, факт_usd = await do_sell(token, 1.0, exit_price, reason, pid, net)
        if not sold:
            pos_intent[token] = pid       # позиция ещё наша — вернуть связку для повтора
            return
        if not LIVE:
            await shadow(token, f"exit_{reason}")     # в бумажном режиме замер бесплатен
        # ЧЕСТНЫЙ ИТОГ: в живом режиме считаем по ДЕНЬГАМ, а не по цене трекера.
        # Цена трекера в момент actor-exit — это цена АКТОРА, за которым мы идём; мы
        # продаём после него, в им же продавленную книгу. Замер на 52 живых сделках:
        # модель +$99.10 против фактических +$37.63, разрыв −9.8% медиана на сделку.
        факт = (факт_usd / rm.clip - 1) if факт_usd is not None else None
        итог = факт if факт is not None else r
        net = итог - (0.0 if факт is not None else strategy.RISK["EXIT_FEE"])
        разрыв = (факт - r) if факт is not None else None
        await loop.run_in_executor(None, delivery.deliver_exit, p, exit_price, reason, True,
                                   итог, разрыв)
        print(f"[EXIT {reason}] {token} realized={итог:+.0%}"
              + (f" (модель {r:+.0%}, разрыв {разрыв:+.1%})" if разрыв is not None else "")
              + (' [ЦЕНА УСТАРЕЛА]' if stale else ''))
        stats["exits"] += 1
        tripped = rm.on_close(net)
        if tripped:
            await loop.run_in_executor(None, delivery.send_alert,
                                       f"ТОРГОВЛЯ ОСТАНОВЛЕНА — {tripped['reason']}")
            print(f"[RISK] СТОП: {tripped['reason']}")
        pm.close(token)

    async def on_event(wallet: str, sig: str, logs: list | None = None,
                       slot_lag: int = 0) -> None:
        # ПРОТУХШЕЕ СОБЫТИЕ, путь «из логов». Разбор из логов не даёт blockTime, монитор
        # проставляет время ПОЛУЧЕНИЯ — и проверка возраста ниже на этом пути всегда
        # видела ноль (аудит 10.08). Отставание по слотам — единственный доступный
        # признак: слот идёт с уведомлением бесплатно, RPC не нужен.
        if slot_lag * SLOT_S > MAX_EVENT_AGE_S:
            stats["stale_slots"] += 1
            print(f"[stale slot] пропуск {sig[:10]}: отставание {slot_lag} слотов "
                  f"(~{slot_lag * SLOT_S / 60:.0f} мин)")
            return
        if sig in seen_sigs:
            return
        seen_sigs.add(sig)
        seen_order.append(sig)
        if len(seen_order) > SEEN_MAX:        # выкидываем только самую старую
            seen_sigs.discard(seen_order.popleft())
        # СНАЧАЛА разбираем логи из самого уведомления — это бесплатно.
        # getTransaction остаётся лишь запасным путём: он стоил 97% всех кредитов
        # (242k/сутки, ~646 запросов на один полезный сигнал) и сжёг лимит за 4 дня.
        trade = log_parse.parse_logs(logs or [], sig, wallet)
        if trade is not None:
            trade["ts"] = time.time()   # время получения; блок отстаёт на 1 слот (0.4с)
            stats["from_logs"] += 1
        elif logs and not log_parse.is_trade(logs):
            return                      # заведомо не сделка — за транзакцией не ходим
        else:
            # В логах сделки НАШЕГО кошелька нет (44% случаев — упоминание в чужой
            # транзакции). Идём за getTransaction: он разберёт по балансам авторитетно
            # и сам отсеет чужое. Часовой потолок не даст повторить инцидент 06.08.
            trade = await loop.run_in_executor(None, tx_parse.parse_trade, sig, wallet)
            if trade:
                stats["from_rpc"] += 1
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
            # Продажа по токену, который трекер ведёт, но позиции у нас нет. Нужна, чтобы
            # у НЕВЗЯТЫХ сигналов можно было восстановить исход: actor-exit закрывает 86%
            # позиций, а без этой записи о нём не осталось бы следа (10.08).
            if actor and not pos and token in tracker.active:
                await loop.run_in_executor(None, delivery.log_actor_sell_any,
                                           token, actor[0], price, trade.get("ts"))
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
        # ЖУРНАЛ ПОКУПОК — до проверки сигнала (аудит 10.08). Пишем синхронно, а не через
        # executor: это добавление строки в файл, а лишний переход между потоками стоил бы
        # больше, чем сама запись, и удлинил бы путь до входа.
        _actor = amap.get(wallet)
        if _actor:
            try:
                delivery.log_actor_buy(_actor[0], wallet, token, ev.usd, ev.ts,
                                       bool(signal), signal.n_actors if signal else 0)
            except Exception:  # noqa: BLE001
                pass          # журнал наблюдений не должен ронять торговлю
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
                # пометка «идёт покупка»: выход по продаже актора в это окно будет
                # отложен, а не выполнен вслепую по ещё не купленному токену (10.08)
                entering[token] = ""
                try:
                    bid = await do_buy(token, entry_price)
                finally:
                    отложенный_выход = entering.pop(token, "")
                if bid is None:
                    # в live покупка не прошла — откатываем слот, иначе бот будет
                    # вести позицию, которой в кошельке нет, и «продаст» пустоту
                    pm.close(token)
                    return
                stats["opens"] += 1
                pos_intent[token] = bid
                if отложенный_выход:
                    # выход, пришедший во время покупки, исполняем сразу: продажа
                    # актора закрывает 86% позиций, терять этот сигнал нельзя
                    print(f"[EXIT догоняет] {token[:12]} ({отложенный_выход})")
                    await emit_exit(token, tracker.last.get(token) or entry_price,
                                    отложенный_выход)
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

    async def orphan_loop() -> None:
        """Подбор токенов, оставшихся в кошельке без позиции (аудит 10.08).

        Работает ТОЛЬКО в живом режиме: в бумажном кошелька не касаемся вовсе.
        Путь появления сироты: покупка не подтвердилась за таймаут → монитор откатил
        слот → транзакция дошла позже → токены есть, позиции нет, выйти некому.
        Мем-коин без присмотра дешевеет за часы, поэтому подбираем, а не копим.
        """
        if not LIVE:
            return
        while True:
            await asyncio.sleep(SWEEP_POLL_S)
            try:
                r = await loop.run_in_executor(None, orphans.recover, False)
            except Exception as e:  # noqa: BLE001
                print(f"[orphans] осмотр не удался: {type(e).__name__}: {e}")
                continue
            if r["orphans"] or r["failed"]:
                msg = (f"подобрано сирот: {r['orphans']} на ${r['orphan_usd']:.2f}, "
                       f"закрыто пустых аккаунтов {len(r['closed'])}")
                if r["failed"]:
                    msg += f"\nНЕ УДАЛОСЬ продать {len(r['failed'])}: " \
                           + ", ".join(x["mint"][:10] for x in r["failed"][:5])
                print(f"[orphans] {msg}", flush=True)
                await loop.run_in_executor(None, delivery.send_alert, msg)

    async def heartbeat() -> None:
        def _rpc_alive() -> str:
            try:                              # живость Helius RPC (баланс кредитов через RPC недоступен)
                r = helius.rpc("getHealth", [])
                return "ok" if r.get("result") == "ok" else "?"
            except Exception:  # noqa: BLE001
                return "нет связи"
        prev_anom = 0

        async def check_quality() -> None:
            """Контроль качества данных и потока. Вызывается ПРИ СТАРТЕ и в каждом пульсе.

            Аудит 08.08: раньше эти проверки жили только внутри шестичасового цикла,
            который начинался со sleep. За всё время работы они не выполнились НИ РАЗУ —
            деплои случались чаще, чем раз в шесть часов, и каждый обнулял таймер.
            Из-за этого watchlist протух до 28.8 дней при пороге 7, и никто не узнал.
            Проверки молчат, когда всё в порядке, поэтому частый вызов не спамит.
            """
            nonlocal prev_anom
            problems = []
            new_anom = tracker.anomalies - prev_anom
            prev_anom = tracker.anomalies
            if new_anom > strategy.ALERTS["MAX_ANOMALY_RATE"]:
                # разбивка обязательна: «аномалий 400» ни о чём не говорит, а
                # «из них 380 на разборе сделки» указывает прямо на парсер логов
                разбивка = ", ".join(f"{k}={v}" for k, v in anom_src.most_common())
                problems.append(f"аномалий цены за период: {new_anom} ({разбивка}) — источник врёт?")
            anom_src.clear()
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
                print("[качество] " + " · ".join(problems), flush=True)
                await loop.run_in_executor(None, delivery.send_alert, " · ".join(problems))

        # пульс при старте — подтверждает, что новый деплой поднялся
        await loop.run_in_executor(None, delivery.send_heartbeat,
                                   f"монитор запущен · {len(wallets)} кош./{len(batches)} WS · "
                                   f"SOL=${market.sol_price():.2f} · открытых={len(pm.open_tokens())}")
        await check_quality()          # ← проверки СРАЗУ, не через шесть часов
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            up_h = (time.time() - stats["started"]) / 3600
            msg = (f"жив {up_h:.0f}ч · v{strategy.VERSION} · сигналов={stats['signals']} "
                   f"(strong={stats['strong']} тихих={stats['quiet']} алертов={stats['alerts']}) · "
                   f"входов={stats['opens']} выходов={stats['exits']} · открытых={len(pm.open_tokens())} · "
                   f"трек={len(tracker.active)} · аном={tracker.anomalies} · "
                   f"непродаваемых={stats['skipped_unsellable']} правилом={stats['skipped_rule']} "
                   f"протухших={stats['stale_slots']} · "
                   f"сделок из логов={stats['from_logs']} через RPC={stats['from_rpc']} "
                   f"(бюджет {helius.budget_report()}) · "
                   f"SOL=${market.sol_price():.2f} · rpc={_rpc_alive()}\n"
                   f"РИСК: {rm.status()}\n{ledger.summary()}\n"
                   f"КОШЕЛЁК: {hot_wallet.status()}\n{sweep.status()}"
                   + (f"\n{orphans.status()}" if LIVE else ""))
            # дублируем в stdout: 07.08 диагностика упёрлась в то, что пульс уходил
            # только в Telegram и счётчики расхода RPC не было видно в docker logs
            print(f"[пульс] {msg.splitlines()[0]}", flush=True)
            await loop.run_in_executor(None, delivery.send_heartbeat, msg)
            await check_quality()

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
                # res["frac"] — доля ИСХОДНОЙ позиции, а своп продаёт долю ТЕКУЩЕГО
                # баланса кошелька. До тейка на руках было (remaining + frac).
                before = p.remaining + res["frac"]
                frac_of_wallet = res["frac"] / before if before > 1e-9 else 1.0
                mult = (cur / p.entry_price) if (cur and p.entry_price) else 1.0
                ok, _факт = await do_sell(token, frac_of_wallet, cur, res["reason"],
                                          pos_intent.get(token), mult - 1)
                if not ok:
                    # токены остались у нас — вернуть учёт, иначе на выходе продадим меньше
                    pm.rollback_partial(token, res["frac"], mult)
                    continue
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

    conns = strategy.TRACKING["WS_CONNECTIONS"]
    cap = strategy.TRACKING["MAX_SUBS_PER_CONN"]
    if len(wallets) > conns * cap:
        # Публичный узел рвёт соединение, если подписок больше ~50: молча потерять
        # половину кошельков хуже, чем добавить канал.
        conns = -(-len(wallets) // cap)
        print(f"[monitor] подписок на канал > {cap} — увеличиваю соединения до {conns}")
    # Шумные адреса — на свой канал: 99.9% трафика идёт через них, и если публичный
    # узел оборвёт это соединение, остальные 146 (99.8% первых сигналов) не пострадают.
    hot = [x for x in wallets if x in set(strategy.TRACKING["HIGH_RATE_WALLETS"])]
    batches = _split([x for x in wallets if x not in set(hot)], conns)
    if hot:
        batches.append(hot)
        print(f"[monitor] {len(hot)} высокочастотных кошельков вынесены на отдельный канал")
    print(f"[monitor] {len(wallets)} кошельков, {len(batches)} WS-соединений, "
          f"live SOL=${market.sol_price():.2f}, max_MC=${max_mc:,.0f}, "
          f"открытых позиций={len(pm.open_tokens())}. Слушаю (вход+выход)...")
    tasks = [asyncio.create_task(helius_ws.subscribe_wallets(b, on_event, label=str(i),
                                                             on_fail=on_ws_fail))
             for i, b in enumerate(batches)]
    tasks.append(asyncio.create_task(tracker.run(on_tick=exit_tick)))   # выходы на 15с-цикле трекера
    tasks.append(asyncio.create_task(heartbeat()))
    tasks.append(asyncio.create_task(sweep_loop()))
    tasks.append(asyncio.create_task(orphan_loop()))
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
