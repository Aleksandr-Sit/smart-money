"""Фаза 1 winner-discovery: топ Solana мем-токенов по капитализации (CoinGecko free, $0).

Собирает ранжированный по MC список мемов, оставляет только Solana (наш стек), достаёт
mint-контракт + ATH/дату ATH (прокси «когда был верх» для анализа «досидел до пика»).
Точная дата запуска (первый on-chain трейд) — Фаза 2 (Dune), тут не тратим кредиты.

Выход: output/top_memes.json (+ печать таблицы).
Run:  .venv\\Scripts\\python.exe -m src.top_memes [--limit 30]
"""
from __future__ import annotations

import argparse
import json
import time

import requests

from . import config

CG = "https://api.coingecko.com/api/v3"
SOL = "solana"


def _get(path: str, params: dict, tries: int = 4) -> list | dict | None:
    for i in range(tries):
        try:
            r = requests.get(f"{CG}{path}", params=params, timeout=30,
                             headers={"Accept": "application/json"})
        except Exception as e:  # noqa: BLE001
            print(f"[cg] {path} ошибка {type(e).__name__}, повтор {i+1}")
            time.sleep(5 * (i + 1))
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:                 # rate limit — ждём дольше
            print(f"[cg] 429 rate limit, жду {10*(i+1)}с")
            time.sleep(10 * (i + 1))
            continue
        print(f"[cg] {path} HTTP {r.status_code}: {r.text[:120]}")
        return None
    return None


def collect(limit: int = 30) -> list[dict]:
    # 1) карта id -> платформы (один вызов; включает solana-минт)
    print("[cg] тяну coins/list (карта контрактов)...")
    plist = _get("/coins/list", {"include_platform": "true"}) or []
    plat = {c["id"]: (c.get("platforms") or {}) for c in plist if isinstance(c, dict)}
    print(f"[cg] {len(plat)} монет в карте платформ")

    # 2) мем-токены, ранжир по MC (2 страницы = топ-500 мемов всех сетей)
    memes: list[dict] = []
    for page in (1, 2):
        r = _get("/coins/markets", {"vs_currency": "usd", "category": "meme-token",
                                    "order": "market_cap_desc", "per_page": 250, "page": page})
        if not r:
            break
        memes += r
        time.sleep(3)
    print(f"[cg] {len(memes)} мем-токенов (все сети)")

    # 3) фильтр Solana + сборка строк
    rows: list[dict] = []
    for m in memes:
        mint = (plat.get(m["id"]) or {}).get(SOL)
        if not mint:
            continue
        rows.append({
            "id": m["id"], "symbol": (m.get("symbol") or "").upper(), "name": m.get("name"),
            "mint": mint, "mc": m.get("market_cap"), "mc_rank": m.get("market_cap_rank"),
            "price": m.get("current_price"),
            "ath": m.get("ath"), "ath_date": m.get("ath_date"),
            "atl": m.get("atl"), "atl_date": m.get("atl_date"),
            "ath_change_pct": m.get("ath_change_percentage"),
        })
    rows.sort(key=lambda x: x["mc"] or 0, reverse=True)
    return rows[:limit]


def main() -> None:
    ap = argparse.ArgumentParser(description="Фаза 1: топ Solana мемов по капитализации")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    rows = collect(args.limit)
    out = config.OUTPUT_DIR / "top_memes.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n=== ТОП-{len(rows)} SOLANA МЕМОВ ПО КАПИТАЛИЗАЦИИ ===")
    print(f"{'#':<3}{'SYM':<10}{'MC':>14}{'ATH дата':>13}  mint")
    for i, r in enumerate(rows, 1):
        mc = f"${r['mc']/1e6:,.1f}M" if r.get("mc") else "—"
        athd = (r.get("ath_date") or "")[:10] or "—"
        print(f"{i:<3}{r['symbol']:<10}{mc:>14}{athd:>13}  {r['mint']}")
    print(f"\nсохранено: {out}")
    print("Дальше (Фаза 2, Dune, после сброса кредитов 10.08): ранние покупатели каждого mint.")


if __name__ == "__main__":
    main()
