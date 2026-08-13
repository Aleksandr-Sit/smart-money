"""Фаза D — исполнительное ядро: реальные свопы через Jupiter.

ПРИНЦИПЫ (модуль тратит настоящие деньги, поэтому паранойя обязательна):
  1. LIVE_ENABLED=false по умолчанию. Пока флаг выключен, модуль НЕ отправляет ничего,
     а возвращает план — так же, как sweep.
  2. Jupiter сам симулирует транзакцию и отдаёт simulationError. Есть ошибка → НЕ шлём.
     Это бесплатная предполётная проверка, игнорировать её нельзя.
  3. Потолок суммы: больше MAX_SWAP_USD не покупаем никогда, даже если позвали с мусором.
  4. Подтверждение с таймаутом. Нет подтверждения → считаем НЕУДАЧЕЙ (fail closed):
     лучше повторно проверить кошелёк, чем думать, что позиция открыта, когда её нет.
  5. Всё пишется в леджер: намерение (котировка) → исполнение (факт + подпись).
     Разница котировки и факта = РЕАЛЬНОЕ проскальзывание, ради его замера всё и строилось.
  6. Закрытие токен-аккаунта после продажи возвращает ренту ~$0.15. При ~230 сделках/сутки
     без этого замораживается ~$35/сутки — больше 10% банка.

Self-test (без отправки):  python -m src.swap <mint>
"""
from __future__ import annotations

import base64
import threading
import time

import requests

from . import helius, ledger, market, strategy, wallet

JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"
WSOL = "So11111111111111111111111111111111111111112"
_LAMPORTS = 1_000_000_000


class SwapError(RuntimeError):
    pass


def _cfg() -> dict:
    return strategy.EXECUTION


def _settled_token_balance_raw(mint: str, before_raw: int, tries: int = 12,
                               pause: float = 2.5) -> tuple[int | None, int]:
    """То же, но в МИНИМАЛЬНЫХ единицах. → (баланс | None, decimals).

    Покупке нужны именно целые единицы: Jupiter отдаёт outAmount в них, и сравнивать
    ожидание с фактом надо в одной системе. Пересчёт через uiAmount породил
    «проскальзывание +99999900%» на первых живых покупках (10.08).
    """
    last, dec = None, 0
    for _ in range(tries):
        for url in (None, helius.send_url()):
            try:
                raw, d, _ata = token_balance_raw(mint, url=url)
            except Exception:  # noqa: BLE001
                continue                      # узел не ответил — это НЕ «баланс нулевой»
            if d:
                dec = d
            last = raw
            if raw != before_raw:
                return raw, dec
        time.sleep(pause)
    return last, dec


_КРИВАЯ_ПОСЛЕДНЯЯ: dict[str, float | None] = {}


def _цена_кривой_из_логов(logs: list, mint: str) -> float | None:
    """Цена кривой в SOL за токен из события pump.fun нашей же транзакции."""
    try:
        from . import log_parse
        for e in log_parse._events(logs):
            if e.get("mint") == mint and e.get("curve_price_sol"):
                return e["curve_price_sol"]
    except Exception:  # noqa: BLE001 — наблюдение не имеет права ломать сделку
        pass
    return None


def цена_кривой_сделки(sig: str) -> float | None:
    """Цена кривой, снятая при последнем разборе транзакции `sig`. None — не нашлась."""
    return _КРИВАЯ_ПОСЛЕДНЯЯ.get(sig)


def _замер_фрикции(iid: str, sig: str, mint: str, dec: int) -> None:
    """Фоновый замер качества исполнения покупки. Ничего не решает, только наблюдает.

    Считает отношение НАШЕЙ цены к справедливой цене кривой в тот же миг: событие
    pump.fun в нашей же транзакции несёт виртуальные резервы, поэтому обе величины
    берутся из одного источника и разрыв между ними — чистая цена маршрута.

    Живёт в отдельном потоке, потому что узел отдаёт транзакцию не сразу: замер
    в один заход заполнялся лишь у 21% покупок. Здесь повторы бесплатны — сделка
    уже подтверждена, позиция уже ведётся.
    """
    try:
        факт_sol, факт_ток, факт_dec = tx_deltas(sig, mint, tries=8, pause=2.0)
        кривая = цена_кривой_сделки(sig)
        фрикция = None
        if факт_sol and факт_ток and факт_ток > 0 and кривая:
            цена = (-факт_sol) / (факт_ток / 10 ** (факт_dec or dec or 6))
            фрикция = цена / кривая - 1
        ledger.record_measure(iid, mint, signature=sig, sol_actual=факт_sol,
                              curve_price_sol=кривая, фрикция=фрикция)
    except Exception as e:  # noqa: BLE001
        print(f"[swap] фоновый замер фрикции не вышел ({type(e).__name__}) — "
              f"сделка не затронута")


def tx_deltas(sig: str, mint: str, tries: int = 10,
              pause: float = 2.0) -> tuple[float | None, int | None, int]:
    """Движение денег ИЗ САМОЙ ТРАНЗАКЦИИ. → (дельта SOL, дельта токена в ед., decimals).

    ЗАЧЕМ (найдено 10.08 по выходу с невозможным итогом −200%). Прежде выручка
    считалась как изменение баланса ВСЕГО кошелька до и после продажи. Пока сделки
    идут по одной, это работает. Но бот держит до пяти позиций, и параллельная
    ПОКУПКА тратит SOL в то же окно: замер приписал чужой расход нашей продаже и
    выдал sol_actual = −0.131 SOL, то есть ровно минус один клип.

        realized_pnl −200.1% при цене выхода 0.96x — невозможное число для спот-лонга.

    Дельта внутри транзакции чужих операций не видит по определению. Комиссия в неё
    входит, и это правильно: нас интересуют чистые деньги, а не валовая выручка.
    """
    w = wallet.Wallet()
    if not w.available:
        return None, None, 0
    for _ in range(tries):
        for url in (helius.send_url(), None):
            try:
                r = helius.rpc("getTransaction",
                               [sig, {"encoding": "jsonParsed",
                                      "maxSupportedTransactionVersion": 0,
                                      "commitment": "confirmed"}], url=url)
            except Exception:  # noqa: BLE001
                continue
            res = r.get("result")
            if not res:
                continue
            m = res["meta"]
            keys = [k["pubkey"] if isinstance(k, dict) else k
                    for k in res["transaction"]["message"]["accountKeys"]]
            if w.address not in keys:
                return None, None, 0
            i = keys.index(w.address)
            sol_d = (m["postBalances"][i] - m["preBalances"][i]) / _LAMPORTS

            def _amt(key):
                for b in (m.get(key) or []):
                    if b.get("owner") == w.address and b.get("mint") == mint:
                        return int(b["uiTokenAmount"]["amount"]), int(b["uiTokenAmount"]["decimals"])
                return 0, 0
            pre, d1 = _amt("preTokenBalances")
            post, d2 = _amt("postTokenBalances")
            # ЦЕНА КРИВОЙ ИЗ НАШЕЙ ЖЕ ТРАНЗАКЦИИ — даром (12.08). Событие pump.fun
            # несёт виртуальные резервы ПОСЛЕ сделки, а транзакция у нас уже в руках.
            # Это даёт фрикцию на КАЖДОЙ сделке: фактическая цена из дельт против
            # справедливой цены кривой в тот же миг. Прежде фрикция восстанавливалась
            # раз в неделю разбором и стоила часов.
            _КРИВАЯ_ПОСЛЕДНЯЯ[sig] = _цена_кривой_из_логов(m.get("logMessages") or [], mint)
            return sol_d, post - pre, (d2 or d1)
        time.sleep(pause)
    return None, None, 0


def _settled_sol_balance(before: float | None, tries: int = 12,
                         pause: float = 2.5) -> float | None:
    """То же для SOL: ждём, пока баланс сдвинется после продажи.

    Первая живая продажа (10.08) вернула sol_actual = 0.0 именно здесь: узел чтения
    девять секунд отдавал баланс ДО сделки, код сдался и записал нулевую выручку,
    из-за чего проскальзывание опять не измерилось. Спрашиваем оба узла и ждём 30с.
    """
    if before is None:
        return None
    w = wallet.Wallet()
    last = None
    for _ in range(tries):
        for url in (None, helius.send_url()):
            bal = w.balance_sol(url=url)
            if bal is None:
                continue
            last = bal
            if abs(bal - before) > 1e-9:
                return bal
        time.sleep(pause)
    return last


def token_balance_raw(mint: str, url: str | None = None) -> tuple[int, int, str | None]:
    """(целое количество в минимальных единицах, decimals, ATA). (0, 0, None) — аккаунта нет.

    ЦЕЛОЕ, А НЕ uiAmount (аудит 10.08). Прежде продажа считалась как
    int(uiAmount * fraction * 10**decimals), то есть число проходило через float.
    Замер перебором на диапазоне 1e12..1e15 минимальных единиц: в 1.24% случаев
    результат меньше остатка ровно на единицу. Одна оставшаяся единица — уже ненулевой
    баланс, а он не даёт закрыть токен-аккаунт и вернуть ренту ~0.002 SOL.
    Масштаб честно: ~3 незакрытых аккаунта из 230 сделок в сутки, около $0.45/сут.
    Правка дешёвая, поэтому сделана, но крупной экономией она не является.

    СБОЙ УЗЛА БРОСАЕТ ИСКЛЮЧЕНИЕ, а не возвращает ноль (найдено пробной сделкой 10.08):
    прежде любая ошибка RPC превращалась в «аккаунта нет», и покупка, реально прошедшая
    на цепи, выглядела как неудачная. Тихий ноль на денежном пути недопустим.
    """
    w = wallet.Wallet()
    if not w.available:
        return 0, 0, None
    r = helius.rpc("getTokenAccountsByOwner",
                   [w.address, {"mint": mint}, {"encoding": "jsonParsed"}], url=url)
    accs = (r.get("result") or {}).get("value") or []
    if not accs:
        return 0, 0, None
    amount = accs[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
    return int(amount["amount"]), int(amount["decimals"]), accs[0]["pubkey"]


def sell_amount_raw(raw_balance: int, fraction: float) -> int:
    """Сколько минимальных единиц продать. → ВЕСЬ остаток при fraction >= 1.

    Вынесено отдельно и покрыто тестом: именно здесь рождалась пыль, мешавшая
    закрыть аккаунт. Полная продажа обязана быть точной, без обратного пересчёта
    через float.
    """
    if fraction >= 1.0:
        return raw_balance
    return int(raw_balance * fraction)


def _quote(input_mint: str, output_mint: str, amount_raw: int,
           slippage_bps: int | None = None) -> dict:
    from . import execution
    bps = slippage_bps if slippage_bps is not None else _cfg()["SLIPPAGE_BPS"]
    q = execution.quote(input_mint, output_mint, amount_raw, bps)
    if not q:
        raise SwapError(f"нет маршрута {input_mint[:6]}→{output_mint[:6]}")
    return q


def _sell_slippage_bps() -> int:
    """Допуск на ВЫХОДЕ. Отдельный от входа — см. обоснование в config/strategy.yaml."""
    return int(_cfg().get("SLIPPAGE_BPS_SELL", _cfg()["SLIPPAGE_BPS"]))


def _build_swap_tx(quote_resp: dict) -> dict:
    """Запросить у Jupiter готовую транзакцию. Возвращает ответ целиком (в нём симуляция)."""
    w = wallet.Wallet()
    body = {
        "quoteResponse": quote_resp,
        "userPublicKey": w.address,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "maxLamports": int(_cfg()["MAX_PRIORITY_LAMPORTS"]),
                "priorityLevel": _cfg()["PRIORITY_LEVEL"],
            }
        },
    }
    try:
        r = requests.post(JUP_SWAP, json=body, timeout=30)
    except Exception as e:  # noqa: BLE001
        raise SwapError(f"Jupiter недоступен: {type(e).__name__}") from None
    if r.status_code != 200:
        raise SwapError(f"Jupiter swap HTTP {r.status_code}: {r.text[:150]}")
    d = r.json()
    if not d.get("swapTransaction"):
        raise SwapError("Jupiter не вернул транзакцию")
    if d.get("simulationError"):
        # Jupiter симулирует до нас — отправлять заведомо провальную tx бессмысленно
        raise SwapError(f"симуляция провалилась: {str(d['simulationError'])[:150]}")
    return d


def _sign_and_send(swap_resp: dict) -> str:
    from solders.transaction import VersionedTransaction
    w = wallet.Wallet()
    raw = base64.b64decode(swap_resp["swapTransaction"])
    unsigned = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(unsigned.message, [w.keypair()])
    payload = base64.b64encode(bytes(signed)).decode()
    res = helius.rpc("sendTransaction",
                     [payload, {"encoding": "base64", "maxRetries": 3,
                                "skipPreflight": False}])
    sig = res.get("result")
    if not sig:
        raise SwapError(f"отправка отклонена: {str(res.get('error'))[:200]}")
    return sig


def confirm(sig: str, timeout_s: int | None = None) -> bool:
    """Дождаться подтверждения. False = НЕ подтвердилось (обрабатывать как неудачу)."""
    return confirm_detail(sig, timeout_s)[0]


def confirm_detail(sig: str, timeout_s: int | None = None) -> tuple[bool, str | None]:
    """→ (подтверждено, причина отказа). Причина None = не дождались, ошибки нет.

    ЗАЧЕМ ОТДЕЛЬНО ОТ confirm (10.08). Алерты владельцу говорили «не подтвердилась за
    таймаут», хотя на цепи транзакция УЖЕ упала с определённой ошибкой Custom 6001
    (превышен допуск проскальзывания). Разница принципиальна: «не дождались» означает
    неизвестность и возможного сироту, а «упала с ошибкой» — что денег не потрачено и
    токенов нет. Диагноз в сообщении должен различать эти два случая.

    Спрашиваем ОБА узла: узел чтения отстаёт на слоты, а узел отправки транзакцию
    заведомо видел — он её и принял.
    """
    timeout_s = timeout_s or _cfg()["CONFIRM_TIMEOUT_S"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for url in (None, helius.send_url()):
            try:
                r = helius.rpc("getSignatureStatuses",
                               [[sig], {"searchTransactionHistory": True}], url=url)
            except Exception:  # noqa: BLE001
                continue
            st = ((r.get("result") or {}).get("value") or [None])[0]
            if not st:
                continue
            if st.get("err"):
                return False, _объяснить(st["err"])
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return True, None
        time.sleep(2)
    return False, None


def _объяснить(err) -> str:
    """Код ошибки Solana → человеческая причина. Иначе в алерте одни цифры."""
    s = str(err)
    if "6001" in s or "0x1771" in s:
        return ("превышен допуск проскальзывания (Jupiter 6001): цена ушла между "
                "котировкой и исполнением; транзакция отклонена, деньги целы")
    if "6000" in s or "0x1770" in s:
        return "маршрут устарел (Jupiter 6000); транзакция отклонена, деньги целы"
    if "insufficient" in s.lower():
        return "не хватило средств на счёте"
    return s[:160]


def _live() -> bool:
    return bool(_cfg().get("LIVE_ENABLED"))


def buy(mint: str, usd: float | None = None) -> dict:
    """Купить токен на usd долларов. Без LIVE_ENABLED возвращает план, ничего не шлёт."""
    w = wallet.Wallet()
    usd = usd if usd is not None else strategy.RISK["CLIP_USD"]
    cap = _cfg()["MAX_SWAP_USD"]
    if usd > cap:
        raise SwapError(f"сумма ${usd:.2f} выше потолка ${cap} — отказ")
    if not w.available:
        return {"action": "skip", "reason": "кошелёк не подключён"}

    sol_usd = market.sol_price()
    lamports = int(usd / sol_usd * _LAMPORTS)
    q = _quote(WSOL, mint, lamports)
    tokens_expected = int(q["outAmount"])          # МИНИМАЛЬНЫЕ единицы, не целые токены
    # ЦЕНУ ЗА ЦЕЛЫЙ ТОКЕН здесь посчитать нечем: decimals известны только из токен-аккаунта,
    # а его при первой покупке ещё не существует. Прежде сюда клали usd/outAmount — цену за
    # минимальную единицу — и сравнивали её с фактической ценой за целый токен. Разница
    # ровно в 10**decimals, отсюда «проскальзывание 999999» на первых живых покупках (10.08).
    # Поэтому в намерение кладём КОЛИЧЕСТВО, а цену считаем на исполнении, когда decimals есть.
    # баланс ДО сделки: считать цену по итоговому балансу нельзя — при повторном входе
    # или после частичной продажи там лежат старые токены, и цена выйдет заниженной.
    # Не прочитали — не повод отказываться от сделки: потеряем только замер проскальзывания.
    try:
        raw_before, dec_before, _ = token_balance_raw(mint)
    except Exception as e:  # noqa: BLE001
        print(f"[swap] баланс до покупки не прочитан ({type(e).__name__}) — "
              f"сделка идёт, проскальзывание не измерим")
        raw_before, dec_before = None, 0

    iid = ledger.record_intent("buy", mint, None, clip_usd=usd,
                               reason="signal", mode="live" if _live() else "dry",
                               extra={"tokens_expected_raw": tokens_expected,
                                      "impact_pct": q.get("priceImpactPct")})
    if not _live():
        return {"action": "dry_run", "intent": iid, "tokens_expected": tokens_expected,
                "quoted_price": None, "usd": usd}

    # ОТКАЗ ДО ОТПРАВКИ ТОЖЕ ОТКАЗ (найдено аудитом 12.08). Между записью намерения и
    # подтверждением лежат два пути — сборка с симуляцией и отправка, — и их исключения
    # уходили наверх, не оставляя следа. Замер: 223 живых намерения без исполнения при
    # ВСЕГО 43 записях reject, то есть 80% неудач были невидимы. Нельзя чинить долю
    # отказов, не зная, из чего она состоит.
    try:
        swap = _build_swap_tx(q)           # содержит симуляцию: провал → исключение
        sig = _sign_and_send(swap)
    except Exception as e:
        ledger.record_reject(iid, mint, f"{type(e).__name__}: {str(e)[:160]}",
                             extra={"side": "buy", "стадия": "до отправки"})
        raise
    ok, причина = confirm_detail(sig)
    raw_after, dec = _settled_token_balance_raw(mint, raw_before if raw_before is not None else -1)
    dec = dec or dec_before
    # None = баланс прочитать не удалось. Это НЕ ноль: подтверждённая транзакция уже
    # в блоке, токены наши, и отказываться от позиции из-за молчания узла нельзя —
    # именно так родилась первая живая сирота (10.08).
    known = raw_after is not None and raw_before is not None
    got_raw = (raw_after - raw_before) if known else None
    got = (got_raw / 10 ** dec) if (got_raw is not None and dec) else None
    quoted_price = (usd / (tokens_expected / 10 ** dec)) if (tokens_expected and dec) else None
    actual_price = (usd / got) if (got and got > 0) else None
    # ЗНАК КАК У ПРОДАЖИ: меньше получили — отрицательное. Считаем по КОЛИЧЕСТВУ,
    # чтобы не зависеть от decimals и не смешивать единицы (урок 10.08).
    slip = (got_raw / tokens_expected - 1) if (got_raw and tokens_expected) else None
    # ЗАМЕР ФРИКЦИИ НА КАЖДОЙ ПОКУПКЕ (12.08). Только наблюдение: решение «состоялась
    # ли сделка» по-прежнему принимается по балансу, ничего не меняется. Дельты дают
    # фактически потраченный SOL, событие в той же транзакции — справедливую цену
    # кривой. Их отношение и есть фрикция, и теперь она известна сразу, а не после
    # недельного разбора.
    факт_sol = кривая_sol = фрикция = None
    if ok:
        # ЗАМЕР УШЁЛ В ФОН (13.08). Прежде он делался здесь одной попыткой без ожидания,
        # чтобы не задерживать открытие позиции. Цена такой осторожности оказалась выше
        # выгоды: узел почти никогда не отдаёт транзакцию в тот же миг, и заполнилось
        # ТОЛЬКО 52 замера из 244 покупок (21%), причём в группе крупных выигрышей —
        # где решается судьба стратегии — их осталось девять. Единственный прямой
        # показатель качества исполнения оказался непригоден ровно там, где нужен.
        # В отдельном потоке повторы ничего не задерживают: покупка уже подтверждена,
        # позиция уже ведётся, а результат дописывается своей записью в леджер.
        threading.Thread(target=_замер_фрикции, args=(iid, sig, mint, dec),
                         daemon=True).start()
    состоялась = bool(got_raw and got_raw > 0) or (ok and got is None)
    if not состоялась:
        # ИСПОЛНЕНИЯ НЕ БЫЛО — и записывать его нельзя. Прежде сюда писался fill с
        # суммой из котировки и нулевым количеством: в учёте это выглядело сделкой
        # на $10 и маскировало долю отказов (35% вместо показанных 22%).
        ledger.record_reject(iid, mint, причина or "нет подтверждения за таймаут",
                             signature=sig, extra={"confirmed": ok, "side": "buy"})
    else:
        ledger.record_fill(iid, mint, actual_price, usd=usd, tokens=got, signature=sig,
                           mode="live", extra={"confirmed": ok, "slippage_vs_quote": slip,
                                               "quoted_price": quoted_price,
                                               "tokens_expected_raw": tokens_expected,
                                               "tokens_got_raw": got_raw,
                                               "balance_before_raw": raw_before,
                                               "balance_after_raw": raw_after,
                                               "sol_actual": факт_sol,
                                               "curve_price_sol": кривая_sol,
                                               "фрикция": фрикция})
    if ok and got is None:
        # подтверждено, но количество неизвестно: позицию открываем (выход продаёт
        # ВЕСЬ остаток, точное число для управления сделкой не нужно), замер теряем
        print(f"[swap] покупка {mint[:12]} подтверждена, баланс не прочитан — "
              f"позиция открывается, проскальзывание не измерено")
        return {"action": "bought", "signature": sig, "tokens": None, "confirmed": True,
                "quoted_price": quoted_price, "actual_price": None,
                "slippage_vs_quote": None, "balance_unknown": True, "intent": iid}
    if got and got > 0:
        # ФАКТ БАЛАНСА ВАЖНЕЕ ВЕРДИКТА ПОДТВЕРЖДЕНИЯ (аудит 10.08). Токены на счету —
        # значит покупка состоялась, даже если confirm() не дождался статуса за таймаут.
        # Прежний код в этом случае бросал исключение, монитор откатывал слот, и токены
        # оставались в кошельке БЕЗ позиции: ни трекинга цены, ни выхода. Это был
        # единственный путь, которым живой режим мог тихо потерять деньги.
        return {"action": "bought", "signature": sig, "tokens": got, "confirmed": ok,
                "quoted_price": quoted_price, "actual_price": actual_price,
                "slippage_vs_quote": slip, "intent": iid}
    if not ok:
        # токенов нет И подтверждения нет — транзакция может подтвердиться ПОЗЖЕ.
        # Отказываем (позиции нет), но пометка нужна: подбирать такие токены будет orphans.
        if причина:
            # транзакция ДОЛЕТЕЛА и упала с определённой ошибкой — денег не потрачено,
            # токенов нет, сироты не будет. Это принципиально не то же самое, что таймаут.
            raise SwapError(f"покупка ОТКЛОНЕНА сетью, tx {sig}: {причина}")
        raise SwapError(f"покупка НЕ подтвердилась за таймаут, tx {sig} — токенов не прибавилось; "
                        f"если транзакция дойдёт позже, токен подберёт разбор сирот")
    # подтверждение есть, а токенов не прибавилось — расхождение, торговать вслепую нельзя
    raise SwapError(f"покупка подтверждена, но баланс не вырос (tx {sig}) — проверить кошелёк")


def sell(mint: str, fraction: float = 1.0, reason: str = "exit") -> dict:
    """Продать долю позиции (1.0 = всю). Закрывает токен-аккаунт при полной продаже."""
    w = wallet.Wallet()
    if not w.available:
        return {"action": "skip", "reason": "кошелёк не подключён"}
    if not (0 < fraction <= 1.0):
        raise SwapError(f"доля {fraction} вне (0, 1]")

    # ОДИН запрос вместо двух: раньше баланс и decimals читались отдельными вызовами.
    # Узел не ответил → НЕ продаём: продажа вслепую хуже пропущенного выхода, а
    # «ноль» от сбоя связи неотличим от пустого аккаунта (пробная сделка 10.08).
    try:
        raw_bal, dec, ata = token_balance_raw(mint)
    except Exception as e:  # noqa: BLE001
        raise SwapError(f"баланс {mint[:12]} не прочитан ({type(e).__name__}) — "
                        f"продажу не начинаем, токен остаётся у нас") from None
    if raw_bal <= 0:
        return {"action": "skip", "reason": "нечего продавать (нулевой баланс)"}
    raw = sell_amount_raw(raw_bal, fraction)
    if raw <= 0:
        return {"action": "skip", "reason": "сумма после округления = 0"}

    q = _quote(mint, WSOL, raw, _sell_slippage_bps())
    sol_out = int(q["outAmount"]) / _LAMPORTS
    sol_usd = market.sol_price()
    # цену считаем по ТОМУ ЖЕ количеству, что уходит в котировку, а не по bal*fraction:
    # при полной продаже это ровно весь остаток, и расхождения между «сколько продаём»
    # и «по чему считаем цену» быть не должно
    tokens_sold = raw / (10 ** dec)
    quoted_price = (sol_out * sol_usd) / tokens_sold

    iid = ledger.record_intent("sell", mint, quoted_price, clip_usd=sol_out * sol_usd,
                               reason=reason, mode="live" if _live() else "dry",
                               extra={"fraction": fraction, "impact_pct": q.get("priceImpactPct")})
    if not _live():
        return {"action": "dry_run", "intent": iid, "quoted_price": quoted_price,
                "sol_out": sol_out, "fraction": fraction}

    try:
        swap = _build_swap_tx(q)
        sig = _sign_and_send(swap)
    except Exception as e:                 # см. пояснение в buy(): отказ до отправки
        ledger.record_reject(iid, mint, f"{type(e).__name__}: {str(e)[:160]}",
                             extra={"side": "sell", "стадия": "до отправки",
                                    "reason_exit": reason})
        raise
    ok, причина = confirm_detail(sig)
    # ФАКТ ИЗ САМОЙ ТРАНЗАКЦИИ, а не из баланса кошелька: параллельная покупка другой
    # позиции тратит SOL в то же окно, и разница балансов приписывала её нашей продаже
    # (найдено 10.08 по выходу с невозможным −200%).
    sol_got, _tok_d, _dec = tx_deltas(sig, mint) if ok else (None, None, 0)
    if sol_got is not None and sol_got <= 0:
        # продажа обязана ПРИНОСИТЬ SOL. Ноль или минус = мы что-то измерили не так,
        # и лучше признать это, чем записать выдуманное число.
        print(f"[swap] {mint[:12]} продажа дала {sol_got:+.6f} SOL — это не выручка, "
              f"выручку считаем неизвестной")
        sol_got = None
    actual_price = (sol_got * sol_usd / tokens_sold) if (sol_got and tokens_sold) else None
    slip = (actual_price / quoted_price - 1) if (actual_price and quoted_price) else None
    if not ok:
        # ПРОДАЖИ НЕ БЫЛО — токены остались у нас. Писать сюда fill с суммой из
        # котировки означало бы утверждать, что выручка получена (правка 10.08).
        ledger.record_reject(iid, mint, причина or "нет подтверждения за таймаут",
                             signature=sig, extra={"confirmed": ok, "side": "sell",
                                                   "reason_exit": reason,
                                                   "sol_quoted": sol_out})
    else:
        ledger.record_fill(iid, mint, actual_price or quoted_price,
                           usd=(sol_got * sol_usd) if sol_got else sol_out * sol_usd,
                           tokens=tokens_sold, signature=sig, mode="live",
                           extra={"confirmed": ok, "reason": reason,
                                  "quoted_price": quoted_price,
                                  "sol_quoted": sol_out, "sol_actual": sol_got,
                                  "slippage_vs_quote": slip,
                                  "curve_price_sol": цена_кривой_сделки(sig),
                                  "фрикция": (
                                      ((sol_got * sol_usd / tokens_sold)
                                       / (цена_кривой_сделки(sig) * sol_usd) - 1)
                                      if (sol_got and tokens_sold
                                          and цена_кривой_сделки(sig)) else None)})
    closed = None
    if ok and fraction >= 1.0:
        # рента ~$0.15 приятна, но продажа уже состоялась — сбой на закрытии аккаунта
        # НЕ должен выглядеть как сорванная продажа. Аккаунт подберёт разбор сирот.
        try:
            closed = close_token_account(mint)
        except Exception as e:  # noqa: BLE001
            closed = {"action": "fail", "reason": f"{type(e).__name__}: {str(e)[:100]}"}
    if not ok:
        if причина:
            raise SwapError(f"продажа ОТКЛОНЕНА сетью, tx {sig}: {причина} — "
                            f"токен остался у нас, выход будет повторён")
        raise SwapError(f"продажа НЕ подтвердилась за таймаут, tx {sig} — проверить кошелёк")
    # ФАКТИЧЕСКАЯ ВЫРУЧКА наверх (10.08). Без неё монитор считал результат сделки по цене
    # ТРЕКЕРА в момент срабатывания правила, а это цена актора, за которым мы идём. Мы
    # продаём ПОСЛЕ него, в им же продавленную книгу. Замер на 52 живых сделках: модель
    # +$99.10, деньги +$37.63, разрыв −9.8% медиана на сделку. Пока PnL считается по
    # модели, каждый будущий замер стратегии завышен примерно на эту величину.
    return {"action": "sold", "signature": sig, "sol_out": sol_out,
            "quoted_price": quoted_price, "ata_closed": closed, "intent": iid,
            "sol_actual": sol_got,
            "usd_actual": (sol_got * sol_usd) if sol_got is not None else None}


def close_token_account(mint: str) -> dict:
    """Закрыть пустой токен-аккаунт и вернуть ренту (~0.002 SOL ≈ $0.15)."""
    w = wallet.Wallet()
    # ЦЕЛОЕ количество: остаток в несколько минимальных единиц во float читается как 0.0,
    # мы бы решили, что аккаунт пуст, и отправили заведомо провальную транзакцию закрытия.
    # ЖДЁМ ОБНУЛЕНИЯ, а не читаем один раз (10.08): закрытие идёт сразу после продажи, и
    # узел чтения ещё отдаёт баланс ДО неё. Первая живая продажа из-за этого не вернула
    # ренту 0.002039 SOL — аккаунт остался открытым с нулём внутри.
    raw_bal, ata, прочитано = None, None, False
    for _ in range(8):
        for url in (helius.send_url(), None):      # сначала узел, принявший продажу
            try:
                raw_bal, _dec, ata = token_balance_raw(mint, url=url)
            except Exception:  # noqa: BLE001
                continue                            # молчание узла — НЕ «аккаунта нет»
            прочитано = True
            if ata is None or raw_bal == 0:
                break
        if прочитано and (ata is None or raw_bal == 0):
            break
        time.sleep(2.0)
    if not прочитано:
        return {"action": "skip", "reason": "баланс не прочитан — закрывать вслепую нельзя"}
    if ata is None:
        return {"action": "skip", "reason": "аккаунта нет"}
    if raw_bal > 0:
        return {"action": "skip", "reason": f"на аккаунте ещё {raw_bal} минимальных единиц"}
    if not _live():
        return {"action": "dry_run", "ata": ata}
    try:
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.transaction import VersionedTransaction
        TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
        acc = Pubkey.from_string(ata)
        ix = Instruction(program_id=TOKEN, data=bytes([9]),      # CloseAccount
                         accounts=[AccountMeta(acc, False, True),
                                   AccountMeta(w.pubkey, False, True),
                                   AccountMeta(w.pubkey, True, False)])
        bh = helius.rpc("getLatestBlockhash", [{"commitment": "finalized"}])
        blockhash = (bh.get("result") or {}).get("value", {}).get("blockhash")
        msg = MessageV0.try_compile(w.pubkey, [ix], [], Hash.from_string(blockhash))
        tx = VersionedTransaction(msg, [w.keypair()])
        res = helius.rpc("sendTransaction",
                         [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64"}])
        sig = res.get("result")
        return {"action": "closed", "signature": sig} if sig else {
            "action": "fail", "reason": str(res.get("error"))[:120]}
    except Exception as e:  # noqa: BLE001
        return {"action": "fail", "reason": f"{type(e).__name__}: {e}"}


def status() -> str:
    c = _cfg()
    mode = "🔴 LIVE" if c.get("LIVE_ENABLED") else "DRY-RUN"
    return (f"исполнение [{mode}] · клип ${strategy.RISK['CLIP_USD']} · "
            f"потолок ${c['MAX_SWAP_USD']} · slippage {c['SLIPPAGE_BPS']}bps · "
            f"подтверждение {c['CONFIRM_TIMEOUT_S']}с")


if __name__ == "__main__":
    import sys
    print(wallet.status())
    print(status())
    for m in sys.argv[1:]:
        try:
            print(f"\n{m[:14]}… покупка:", buy(m))
        except SwapError as e:
            print(f"\n{m[:14]}… ОШИБКА: {e}")
