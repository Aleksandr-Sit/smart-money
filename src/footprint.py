"""Широта он-чейн-следа кандидатов — детекция spray-ботов (покупают тысячи токенов).

Наш bot_like по одному токену не ловит широких снайперов (на токене не переторговывают,
но берут ТЫСЯЧИ токенов). Тянем полное число уник. токенов на кошелёк одним запросом.

Run:  .venv\\Scripts\\python.exe -m src.footprint [--days 80] [--force]
"""
from __future__ import annotations

import argparse

from . import db, dune_fetch

WSOL = "So11111111111111111111111111111111111111112"

FOOTPRINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_footprint (
    wallet VARCHAR, total_trades BIGINT, total_tokens BIGINT,
    first_trade TIMESTAMP, last_trade TIMESTAMP, vol_usd DOUBLE
);
"""


def fetch(days: int, min_early: int, min_wins: int, win_mult: float, force: bool) -> None:
    con = db.connect()
    con.execute(FOOTPRINT_SCHEMA)
    cands = [w for (w,) in con.execute(
        "SELECT wallet FROM early_pnl GROUP BY wallet "
        "HAVING count(*) >= ? AND count(*) FILTER (WHERE multiple >= ?) >= ?",
        [min_early, win_mult, min_wins]).fetchall()]
    if not force:
        have = {w for (w,) in con.execute("SELECT wallet FROM wallet_footprint").fetchall()}
        cands = [w for w in cands if w not in have]
    if not cands:
        print("[cache] footprint всех кандидатов уже есть")
        con.close()
        return

    print(f"[dune] footprint по {len(cands)} кандидатам, {days} дней ...")
    wl = ",".join("'" + w + "'" for w in cands)
    sql = f"""
    SELECT trader_id,
        count(*)                                                          AS total_trades,
        count(DISTINCT CASE WHEN token_bought_mint_address <> '{WSOL}'
             THEN token_bought_mint_address END)                         AS total_tokens,
        min(block_time)                                                   AS first_trade,
        max(block_time)                                                   AS last_trade,
        round(sum(amount_usd))                                            AS vol_usd
    FROM dex_solana.trades
    WHERE trader_id IN ({wl}) AND block_time > now() - interval '{days}' day
    GROUP BY 1
    """
    rows = dune_fetch.run_sql(sql)
    con.execute("DELETE FROM wallet_footprint")
    con.executemany(
        "INSERT INTO wallet_footprint VALUES (?,?,?,?,?,?)",
        [(r["trader_id"], int(r["total_trades"]), int(r["total_tokens"]),
          dune_fetch._parse_ts(r["first_trade"]), dune_fetch._parse_ts(r["last_trade"]),
          float(r["vol_usd"] or 0)) for r in rows],
    )
    con.close()
    print(f"[dune] footprint получен для {len(rows)} кошельков")


def main() -> None:
    ap = argparse.ArgumentParser(description="Широта следа кандидатов (spray-bot detection)")
    ap.add_argument("--days", type=int, default=80)
    ap.add_argument("--min-early", type=int, default=2)
    ap.add_argument("--min-wins", type=int, default=1)
    ap.add_argument("--win-mult", type=float, default=5.0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    fetch(a.days, a.min_early, a.min_wins, a.win_mult, a.force)


if __name__ == "__main__":
    main()
