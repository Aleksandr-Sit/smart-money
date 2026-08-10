"""Разбор СИРОТ — токенов, которые лежат в кошельке без открытой позиции.

ЗАЧЕМ (аудит 10.08). В живом режиме есть путь, на котором бот теряет токены из виду:

  1. `swap.buy` отправляет транзакцию и ждёт подтверждения CONFIRM_TIMEOUT_S секунд.
  2. Подтверждение не пришло, токенов на балансе ещё нет → покупка объявлена неудачной,
     монитор откатывает слот позиции.
  3. Транзакция подтверждается ПОЗЖЕ. Токены приходят в кошелёк, но позиции у бота нет:
     цену никто не ведёт, правила выхода не применяются, продать некому.

Второй источник сирот — неудачная продажа: `do_sell` вернул False, позиция осталась
нашей, но при следующем рестарте её могло не быть в open_positions.json.

Токен без позиции не станет лучше от ожидания: мем-коин теряет ликвидность за часы.
Поэтому разбор идёт по правилу «нашёл — продал», а не «нашёл — сообщил и жду».

БЕЗОПАСНОСТЬ. Никогда не трогаем токен, по которому позиция ОТКРЫТА, — его ведёт
монитор. Пустые токен-аккаунты закрываем: каждый держит ~0.002 SOL ренты.

Run:  python -m src.orphans [--recover]      (без флага — только показать)
"""
from __future__ import annotations

import argparse

from . import config, helius, market, positions, strategy, swap, wallet

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
WSOL = "So11111111111111111111111111111111111111112"
# Пыль не считаем сиротой: продать её дороже, чем она стоит, а закрыть аккаунт мешает
# ненулевой баланс. Порог в долларах, а не в токенах: у мем-коинов разные масштабы.
DUST_USD = 0.50


def wallet_tokens() -> list[dict]:
    """Все токен-аккаунты кошелька → [{mint, amount, ata}]. Пустой список, если ключа нет."""
    w = wallet.Wallet()
    if not w.available:
        return []
    r = helius.rpc("getTokenAccountsByOwner",
                   [w.address, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}])
    out = []
    for acc in (r.get("result") or {}).get("value") or []:
        info = acc["account"]["data"]["parsed"]["info"]
        out.append({"mint": info["mint"],
                    "amount": float(info["tokenAmount"]["uiAmount"] or 0),
                    "ata": acc["pubkey"]})
    return out


def classify(accounts: list[dict], open_tokens: set[str], prices: dict[str, float] | None = None,
             dust_usd: float = DUST_USD) -> dict[str, list[dict]]:
    """Разложить аккаунты на четыре кучи. Чистая функция — вся логика решения здесь.

    → orphan — продать: токен есть, позиции нет
      empty  — закрыть ради ренты (~0.002 SOL): аккаунт есть, токенов нет
      held   — не трогать: позицию ведёт монитор
      dust   — не трогать: стоит меньше порога, продажа обойдётся дороже

    WSOL пропускаем всегда: это обёртка для расчётов, а не позиция.

    НЕИЗВЕСТНАЯ ЦЕНА — НЕ ПОВОД СЧИТАТЬ ПЫЛЬЮ. Токен, по которому нет котировки,
    попадает в orphan: продажу попробуем, а вот молча оставить его в кошельке нельзя.
    """
    prices = prices or {}
    res: dict[str, list[dict]] = {"orphan": [], "empty": [], "held": [], "dust": []}
    for a in accounts:
        mint, amt = a["mint"], a["amount"]
        if mint == WSOL:
            continue
        px = prices.get(mint)
        if amt <= 0:
            res["empty"].append(a)
        elif mint in open_tokens:
            res["held"].append(a)
        elif px is not None and px > 0 and px * amt < dust_usd:
            res["dust"].append({**a, "usd": px * amt})
        else:
            res["orphan"].append({**a, "usd": (px or 0) * amt})
    return res


def _price(mint: str) -> float | None:
    try:
        return market.token_info(mint).get("price_usd")
    except Exception:  # noqa: BLE001
        return None


def scan() -> dict:
    """Осмотреть кошелёк и разложить содержимое. Ничего не меняет."""
    pm = positions.PositionManager()
    accounts = wallet_tokens()
    # цену спрашиваем ТОЛЬКО у токенов без позиции: по остальным решение уже принято,
    # а каждый запрос — сетевой вызов
    open_tokens = set(pm.open_tokens())
    prices = {a["mint"]: _price(a["mint"]) for a in accounts
              if a["amount"] > 0 and a["mint"] not in open_tokens and a["mint"] != WSOL}
    prices = {k: v for k, v in prices.items() if v is not None}
    groups = classify(accounts, open_tokens, prices)
    usd = sum(a.get("usd") or 0 for a in groups["orphan"])
    return {**groups, "orphan_usd": usd, "accounts": len(accounts)}


def recover(dry_run: bool = True) -> dict:
    """Продать сирот и закрыть пустые аккаунты. dry_run=True — только план.

    Возвращает отчёт; ошибка по одному токену не останавливает разбор остальных —
    застрявший мем-коин не должен мешать вернуть остальные деньги.
    """
    r = scan()
    sold, failed, closed = [], [], []
    for a in r["orphan"]:
        if dry_run:
            sold.append({"mint": a["mint"], "action": "dry_run"})
            continue
        try:
            res = swap.sell(a["mint"], 1.0, reason="orphan")
            sold.append({"mint": a["mint"], **{k: res.get(k) for k in ("action", "signature")}})
        except Exception as e:  # noqa: BLE001
            failed.append({"mint": a["mint"], "error": f"{type(e).__name__}: {str(e)[:120]}"})
    for a in r["empty"]:
        if dry_run:
            closed.append({"mint": a["mint"], "action": "dry_run"})
            continue
        try:
            closed.append({"mint": a["mint"], **swap.close_token_account(a["mint"])})
        except Exception as e:  # noqa: BLE001
            failed.append({"mint": a["mint"], "error": f"{type(e).__name__}: {str(e)[:120]}"})
    return {"orphans": len(r["orphan"]), "orphan_usd": r["orphan_usd"],
            "held": len(r["held"]), "empty": len(r["empty"]), "dust": len(r["dust"]),
            "sold": sold, "closed": closed, "failed": failed, "dry_run": dry_run}


def status() -> str:
    if not wallet.Wallet().available:
        return "разбор сирот: кошелёк не подключён"
    try:
        r = scan()
    except Exception as e:  # noqa: BLE001
        return f"разбор сирот: осмотр не удался ({type(e).__name__})"
    return (f"кошелёк: аккаунтов {r['accounts']} · под позициями {len(r['held'])} · "
            f"СИРОТ {len(r['orphan'])} (${r['orphan_usd']:.2f}) · пустых {len(r['empty'])} · "
            f"пыли {len(r['dust'])}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Разбор токенов без открытой позиции")
    ap.add_argument("--recover", action="store_true", help="продать сирот и закрыть пустые")
    a = ap.parse_args()
    print(wallet.status())
    print(f"живой режим: {strategy.EXECUTION['LIVE_ENABLED']}")
    r = recover(dry_run=not a.recover)
    for k, v in r.items():
        if k not in ("sold", "closed", "failed"):
            print(f"  {k}: {v}")
    for name in ("sold", "closed", "failed"):
        for x in r[name]:
            print(f"  {name}: {x}")
    print(f"[orphans] {config.OUTPUT_DIR}")


if __name__ == "__main__":
    main()
