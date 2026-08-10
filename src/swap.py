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


def _settled_token_balance(mint: str, before: float, tries: int = 6) -> float:
    """Дождаться, пока баланс токена изменится после подтверждённой сделки.

    Подтверждение транзакции и чтение баланса могут прийти с разных слотов, а на
    публичном узле — и с разных нод. Прочитав сразу, легко получить состояние ДО
    сделки и записать в леджер мусорное проскальзывание. Ждём фактического сдвига.
    """
    last = before
    for _ in range(tries):
        bal, _ = token_balance(mint)
        if abs(bal - before) > 1e-12:
            return bal
        last = bal
        time.sleep(1.5)
    return last


def _settled_sol_balance(before: float | None, tries: int = 6) -> float | None:
    """То же для SOL: ждём, пока баланс сдвинется после продажи."""
    if before is None:
        return None
    last = before
    for _ in range(tries):
        bal = wallet.Wallet().balance_sol()
        if bal is not None and abs(bal - before) > 1e-9:
            return bal
        last = bal if bal is not None else last
        time.sleep(1.5)
    return last


def token_balance(mint: str) -> tuple[float, str | None]:
    """(количество токенов, адрес ATA). (0, None) если аккаунта нет."""
    raw, _dec, ata = token_balance_raw(mint)
    if ata is None:
        return 0.0, None
    return raw / (10 ** _dec), ata


def token_balance_raw(mint: str) -> tuple[int, int, str | None]:
    """(целое количество в минимальных единицах, decimals, ATA). (0, 0, None) — аккаунта нет.

    ЦЕЛОЕ, А НЕ uiAmount (аудит 10.08). Прежде продажа считалась как
    int(uiAmount * fraction * 10**decimals), то есть число проходило через float.
    Замер перебором на диапазоне 1e12..1e15 минимальных единиц: в 1.24% случаев
    результат меньше остатка ровно на единицу. Одна оставшаяся единица — уже ненулевой
    баланс, а он не даёт закрыть токен-аккаунт и вернуть ренту ~0.002 SOL.
    Масштаб честно: ~3 незакрытых аккаунта из 230 сделок в сутки, около $0.45/сут.
    Правка дешёвая, поэтому сделана, но крупной экономией она не является.
    """
    w = wallet.Wallet()
    if not w.available:
        return 0, 0, None
    try:
        r = helius.rpc("getTokenAccountsByOwner",
                       [w.address, {"mint": mint}, {"encoding": "jsonParsed"}])
        accs = (r.get("result") or {}).get("value") or []
        if not accs:
            return 0, 0, None
        amount = accs[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
        return int(amount["amount"]), int(amount["decimals"]), accs[0]["pubkey"]
    except Exception:  # noqa: BLE001
        return 0, 0, None


def sell_amount_raw(raw_balance: int, fraction: float) -> int:
    """Сколько минимальных единиц продать. → ВЕСЬ остаток при fraction >= 1.

    Вынесено отдельно и покрыто тестом: именно здесь рождалась пыль, мешавшая
    закрыть аккаунт. Полная продажа обязана быть точной, без обратного пересчёта
    через float.
    """
    if fraction >= 1.0:
        return raw_balance
    return int(raw_balance * fraction)


def _quote(input_mint: str, output_mint: str, amount_raw: int) -> dict:
    from . import execution
    q = execution.quote(input_mint, output_mint, amount_raw, _cfg()["SLIPPAGE_BPS"])
    if not q:
        raise SwapError(f"нет маршрута {input_mint[:6]}→{output_mint[:6]}")
    return q


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
    timeout_s = timeout_s or _cfg()["CONFIRM_TIMEOUT_S"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = helius.rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
            st = ((r.get("result") or {}).get("value") or [None])[0]
            if st:
                if st.get("err"):
                    return False
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    return False


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
    tokens_expected = int(q["outAmount"])
    quoted_price = usd / tokens_expected if tokens_expected else None
    # баланс ДО сделки: считать цену по итоговому балансу нельзя — при повторном входе
    # или после частичной продажи там лежат старые токены, и цена выйдет заниженной
    bal_before, _ = token_balance(mint)

    iid = ledger.record_intent("buy", mint, quoted_price, clip_usd=usd,
                               reason="signal", mode="live" if _live() else "dry",
                               extra={"tokens_expected": tokens_expected,
                                      "impact_pct": q.get("priceImpactPct")})
    if not _live():
        return {"action": "dry_run", "intent": iid, "tokens_expected": tokens_expected,
                "quoted_price": quoted_price, "usd": usd}

    swap = _build_swap_tx(q)               # содержит симуляцию: провал → исключение
    sig = _sign_and_send(swap)
    ok = confirm(sig)
    bal_after = _settled_token_balance(mint, bal_before)
    got = bal_after - bal_before           # КУПЛЕНО, а не всего на аккаунте
    actual_price = (usd / got) if got > 0 else None
    slip = (actual_price / quoted_price - 1) if (actual_price and quoted_price) else None
    ledger.record_fill(iid, mint, actual_price, usd=usd, tokens=got, signature=sig,
                       mode="live", extra={"confirmed": ok, "slippage_vs_quote": slip,
                                           "balance_before": bal_before, "balance_after": bal_after})
    if got > 0:
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

    # ОДИН запрос вместо двух: раньше баланс и decimals читались отдельными вызовами
    raw_bal, dec, ata = token_balance_raw(mint)
    if raw_bal <= 0:
        return {"action": "skip", "reason": "нечего продавать (нулевой баланс)"}
    raw = sell_amount_raw(raw_bal, fraction)
    if raw <= 0:
        return {"action": "skip", "reason": "сумма после округления = 0"}

    q = _quote(mint, WSOL, raw)
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

    sol_before = w.balance_sol()
    swap = _build_swap_tx(q)
    sig = _sign_and_send(swap)
    ok = confirm(sig)
    # ФАКТ, а не котировка: раньше сюда писался quoted_price, поэтому измеренное
    # проскальзывание по продажам было тождественно нулю — ради его замера всё и строилось
    sol_after = _settled_sol_balance(sol_before)
    sol_got = (sol_after - sol_before) if (sol_after is not None and sol_before is not None) else None
    actual_price = (sol_got * sol_usd / tokens_sold) if (sol_got and tokens_sold) else None
    slip = (actual_price / quoted_price - 1) if (actual_price and quoted_price) else None
    ledger.record_fill(iid, mint, actual_price or quoted_price,
                       usd=(sol_got * sol_usd) if sol_got else sol_out * sol_usd,
                       tokens=tokens_sold, signature=sig, mode="live",
                       extra={"confirmed": ok, "reason": reason, "quoted_price": quoted_price,
                              "sol_quoted": sol_out, "sol_actual": sol_got,
                              "slippage_vs_quote": slip})
    closed = None
    if ok and fraction >= 1.0:
        closed = close_token_account(mint)     # вернуть ренту ~$0.15
    if not ok:
        raise SwapError(f"продажа НЕ подтвердилась за таймаут, tx {sig} — проверить кошелёк")
    return {"action": "sold", "signature": sig, "sol_out": sol_out,
            "quoted_price": quoted_price, "ata_closed": closed, "intent": iid}


def close_token_account(mint: str) -> dict:
    """Закрыть пустой токен-аккаунт и вернуть ренту (~0.002 SOL ≈ $0.15)."""
    w = wallet.Wallet()
    # ЦЕЛОЕ количество: остаток в несколько минимальных единиц во float читается как 0.0,
    # мы бы решили, что аккаунт пуст, и отправили заведомо провальную транзакцию закрытия
    raw_bal, _dec, ata = token_balance_raw(mint)
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
