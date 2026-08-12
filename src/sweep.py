"""Фаза D — односторонний вывод прибыли на холодный кошелёк (Ledger).

ГЛАВНОЕ СВОЙСТВО: адрес получателя — КОНСТАНТА из config/strategy.yaml, а не параметр.
Функция вывода физически не принимает адрес → ни баг, ни случайный вызов не могут
отправить средства куда-либо ещё. Конфиг под git: подмена адреса видна в диффе как
изменение кода (в отличие от .env, который в истории не виден).

ЧЕСТНОЕ ОГРАНИЧЕНИЕ: на Solana нет биржи, принудительно проверяющей whitelist. Укравший
ключ отправит средства куда угодно в обход нашего кода. Поэтому код защищает от НАШИХ
ошибок, а от кражи защищает только малый остаток на горячем кошельке — ради этого и
выводим часто (порог $25: комиссия $0.0004 несущественна при любой частоте).

Предохранители: интервал между выводами, потолок суммы, неснижаемый буфер комиссий,
kill-switch НЕ блокирует вывод (убрать деньги со стола безопасно всегда).
"""
from __future__ import annotations

import json
import time

from . import config, delivery, helius, ledger, market, strategy, wallet

_LAMPORTS = 1_000_000_000
_ФАЙЛ_ПОСЛЕДНЕГО = "last_sweep.json"


def _прочитать_последний() -> float:
    """Время последнего вывода — ИЗ ФАЙЛА, а не из памяти процесса.

    Переменная в памяти обнулялась при каждом перезапуске контейнера, а деплой
    случается чаще, чем MIN_INTERVAL_S. Защита от частых выводов существовала
    только на бумаге: после `up --build` интервал считался с нуля и следующий
    вывод мог уйти сразу за предыдущим.
    """
    try:
        with open(config.OUTPUT_DIR / _ФАЙЛ_ПОСЛЕДНЕГО, encoding="utf-8") as f:
            return float(json.load(f).get("ts") or 0.0)
    except Exception:  # noqa: BLE001 — нет файла = выводов ещё не было
        return 0.0


def _записать_последний(ts: float) -> None:
    try:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = config.OUTPUT_DIR / (_ФАЙЛ_ПОСЛЕДНЕГО + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": ts}, f)
        tmp.replace(config.OUTPUT_DIR / _ФАЙЛ_ПОСЛЕДНЕГО)
    except Exception as e:  # noqa: BLE001
        print(f"[вывод] не удалось записать отметку времени: {type(e).__name__}")


class SweepError(RuntimeError):
    pass


def destination():
    """Адрес получателя — ТОЛЬКО из конфига. Валидация строгая, fail closed."""
    from solders.pubkey import Pubkey
    addr = str(strategy.SWEEP.get("SWEEP_ADDRESS") or "").strip()
    if not addr:
        raise SweepError("SWEEP_ADDRESS не задан в config/strategy.yaml — вывод отключён")
    try:
        pk = Pubkey.from_string(addr)
    except Exception:  # noqa: BLE001
        raise SweepError(f"SWEEP_ADDRESS не является адресом Solana: {addr[:12]}…") from None
    w = wallet.Wallet()
    if w.available and str(pk) == w.address:
        raise SweepError("SWEEP_ADDRESS совпадает с горячим кошельком — вывод в никуда")
    return pk


def plan(balance_sol: float | None = None) -> dict:
    """Сколько надо вывести. Ничего не отправляет — только расчёт."""
    cfg = strategy.SWEEP
    w = wallet.Wallet()
    bal = balance_sol if balance_sol is not None else w.balance_sol()
    if bal is None:
        return {"action": "skip", "reason": "баланс недоступен (RPC) — fail closed"}
    sol_usd = market.sol_price()
    bal_usd = bal * sol_usd
    keep_usd = strategy.RISK["BANKROLL_USD"]
    buffer_usd = cfg["FEE_BUFFER_SOL"] * sol_usd
    excess = bal_usd - keep_usd - buffer_usd
    if bal_usd < cfg["SWEEP_TRIGGER_USD"]:
        return {"action": "skip", "reason": f"баланс ${bal_usd:.2f} < порога ${cfg['SWEEP_TRIGGER_USD']}",
                "balance_usd": bal_usd}
    if excess < cfg["SWEEP_MIN_USD"]:
        return {"action": "skip", "reason": f"излишек ${excess:.2f} < минимума ${cfg['SWEEP_MIN_USD']}",
                "balance_usd": bal_usd}
    amount_usd = min(excess, cfg["SWEEP_MAX_USD"])          # потолок на одну транзакцию
    return {"action": "sweep", "amount_usd": amount_usd, "amount_sol": amount_usd / sol_usd,
            "balance_usd": bal_usd, "keep_usd": keep_usd, "sol_usd": sol_usd}


def execute(dry_run: bool | None = None) -> dict:
    """Выполнить вывод по плану. dry_run=None → берётся из конфига (по умолчанию БЕЗОПАСНО)."""
    cfg = strategy.SWEEP
    dry = cfg["DRY_RUN"] if dry_run is None else dry_run

    if not cfg["ENABLED"]:
        return {"action": "skip", "reason": "вывод выключен (SWEEP.ENABLED=false)"}
    w = wallet.Wallet()
    if not w.available:
        return {"action": "skip", "reason": "кошелёк не подключён"}
    since = time.time() - _прочитать_последний()
    if since < cfg["MIN_INTERVAL_S"]:
        return {"action": "skip", "reason": f"интервал {since:.0f}с < {cfg['MIN_INTERVAL_S']}с"}

    dest = destination()                     # бросит исключение, если адрес не валиден
    p = plan()
    if p["action"] != "sweep":
        return p

    lamports = int(p["amount_sol"] * _LAMPORTS)
    if lamports <= 0:
        return {"action": "skip", "reason": "сумма после округления = 0"}

    iid = ledger.record_intent("sweep", "SOL", p["sol_usd"], clip_usd=p["amount_usd"],
                               reason="вывод прибыли", mode="dry" if dry else "live",
                               extra={"destination": str(dest)})
    if dry:
        return {**p, "action": "dry_run", "destination": str(dest), "intent": iid}

    sig = _send_transfer(w, dest, lamports)
    _записать_последний(time.time())
    ledger.record_fill(iid, "SOL", p["sol_usd"], usd=p["amount_usd"], signature=sig,
                       mode="live", extra={"destination": str(dest)})
    delivery.send_alert(f"ВЫВОД ${p['amount_usd']:.2f} на холодный кошелёк · tx {sig[:16]}…")
    return {**p, "action": "sent", "signature": sig, "destination": str(dest), "intent": iid}


def _send_transfer(w, dest, lamports: int) -> str:
    """Сборка, подпись и отправка перевода SOL. Получатель приходит ТОЛЬКО из destination()."""
    # защита в глубину: даже прямой вызов этой функции не отправит на чужой адрес
    if str(dest) != str(destination()):
        raise SweepError("получатель не совпадает с SWEEP_ADDRESS — отправка заблокирована")
    from solders.message import MessageV0
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction

    bh = helius.rpc("getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash = (bh.get("result") or {}).get("value", {}).get("blockhash")
    if not blockhash:
        raise SweepError("не получен blockhash")
    from solders.hash import Hash
    ix = transfer(TransferParams(from_pubkey=w.pubkey, to_pubkey=dest, lamports=lamports))
    msg = MessageV0.try_compile(w.pubkey, [ix], [], Hash.from_string(blockhash))
    tx = VersionedTransaction(msg, [w.keypair()])
    import base64
    raw = base64.b64encode(bytes(tx)).decode()
    res = helius.rpc("sendTransaction", [raw, {"encoding": "base64", "maxRetries": 3}])
    sig = res.get("result")
    if not sig:
        raise SweepError(f"отправка не удалась: {str(res.get('error'))[:120]}")
    return sig


def status() -> str:
    try:
        dest = str(destination())
        dest_s = f"{dest[:6]}…{dest[-4:]}"
    except SweepError as e:
        dest_s = f"НЕ НАСТРОЕН ({e})"
    p = plan()
    cfg = strategy.SWEEP
    mode = "DRY-RUN" if cfg["DRY_RUN"] else "LIVE"
    # ПОКАЗЫВАЕМ И SOL, И ДОЛЛАРЫ. Кошелёк номинирован в SOL, а пороги заданы в
    # долларах: при движении курса баланс «пересекает» порог без единой сделки,
    # и по одной строке в долларах это неотличимо от заработка.
    bal_usd = p.get("balance_usd")
    sol_usd = market.sol_price()
    баланс = ("н/д" if bal_usd is None or not sol_usd
              else f"{bal_usd / sol_usd:.4f} SOL (${bal_usd:.2f} по курсу ${sol_usd:.0f})")
    посл = _прочитать_последний()
    когда = ("выводов не было" if not посл
             else f"последний вывод {(time.time() - посл) / 3600:.1f}ч назад")
    return (f"вывод [{mode}, {'вкл' if cfg['ENABLED'] else 'выкл'}] → {dest_s} · "
            f"баланс {баланс} · порог ${cfg['SWEEP_TRIGGER_USD']}, шаг ${cfg['SWEEP_MIN_USD']} · "
            f"{когда} · {p.get('reason', p['action'])}")


if __name__ == "__main__":
    print(wallet.status())
    print(status())
    print("план:", plan())
