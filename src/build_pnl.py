"""Прогнать Модуль A по всему universe: batch-забор трейдов + per-token realized P&L.

CLI:
  .venv\\Scripts\\python.exe -m src.build_pnl [--since 2026-06-26] [--force]
"""
from __future__ import annotations

import argparse

from . import db, dune_fetch, pnl


def main() -> None:
    ap = argparse.ArgumentParser(description="Модуль A по всему universe")
    ap.add_argument("--since", default="2026-06-26")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    dune_fetch.fetch_universe_trades(args.since, force=args.force)

    con = db.connect()
    mints = [r[0] for r in con.execute("SELECT mint FROM universe ORDER BY class, mint").fetchall()]
    con.close()

    print(f"\nСчитаю P&L по {len(mints)} токенам universe...")
    print(f"{'mint':<46} {'wallets':>7} {'buyers':>7} {'winners':>7} {'unbk_sell':>9}")
    for m in mints:
        s = pnl.compute(m)
        print(f"{m:<46} {s['wallets']:>7} {s['buyers']:>7} "
              f"{s['winners_roundtrip']:>7} {s['unbacked_sellers (insider/transfer)']:>9}")
    print("\nГотово. wallet_pnl заполнена по всему universe → дальше Модуль C (агрегация).")


if __name__ == "__main__":
    main()
