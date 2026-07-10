"""Модуль C — кросс-токенная агрегация: wallet -> [токены], hit_rate с честным знаменателем.

Знаменатель hit_rate = ВСЕ ранние входы кошелька по universe (pumped + dead),
а не только выигрыши. Мёртвые токены в universe специально → анти-survivorship.

realized ≈ (avg_sell − avg_buy) × min(bought_qty, sold_qty):
  = точному avg-cost на закрытых позициях, и исключает безбазисные продажи инсайдеров.

early_pnl кэшируется в DuckDB → тюнинг win_mult/min_early бесплатен (без Dune).
--refresh форсит перезабор из Dune (нужен при смене early_frac/min_bought/since).

CLI:
  .venv\\Scripts\\python.exe -m src.aggregate [--since 2026-06-26] [--early-frac 0.05]
      [--min-bought 10] [--win-mult 5.0] [--min-early 2] [--refresh]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import db, dune_fetch

SQL_DIR = Path(__file__).resolve().parent / "sql"

EARLY_PNL_SCHEMA = """
CREATE TABLE IF NOT EXISTS early_pnl (
    mint VARCHAR, class VARCHAR, wallet VARCHAR,
    n_buys INTEGER, n_sells INTEGER,
    bought_usd DOUBLE, sold_usd DOUBLE, bought_qty DOUBLE, sold_qty DOUBLE,
    realized DOUBLE, roi DOUBLE, multiple DOUBLE, entry_frac DOUBLE,
    first_buy_time TIMESTAMP, first_buy_slot BIGINT, last_sell_time TIMESTAMP,
    has_unbacked BOOLEAN
);
"""


def _fetch_early(con, since: str, early_mc: float, min_bought: float) -> list[dict]:
    uni = con.execute("SELECT mint, peak_mc_p99, class FROM universe").fetchall()
    if not uni:
        raise RuntimeError("universe пуст — сначала src.universe")
    peaks = {m: (p99 / db.FIXED_SUPPLY) for m, p99, _ in uni}   # peak_price (только для entry_frac)
    classes = {m: c for m, _, c in uni}
    mints = list(peaks)
    early_price = early_mc / db.FIXED_SUPPLY   # порог цены раннего входа (абсолютная MC)

    mints_sql = ",".join(f"'{m}'" for m in mints)
    sql = (SQL_DIR / "early_buyer_pnl.sql").read_text(encoding="utf-8")
    sql = (sql.replace("{{mints}}", mints_sql)
              .replace("{{since}}", since)
              .replace("{{early_price}}", repr(early_price))
              .replace("{{min_bought}}", str(min_bought)))
    print(f"[dune] early-buyer P&L: {len(mints)} tokens, early_MC<=${early_mc:,.0f}, "
          f"min_bought={min_bought}, since={since} ...")
    rows = dune_fetch.run_sql(sql)
    print(f"[dune] получено {len(rows)} строк (токен×кошелёк ранних покупателей)")

    con.execute(EARLY_PNL_SCHEMA)
    con.execute("DELETE FROM early_pnl")
    out = []
    for r in rows:
        mint = r["mint"]
        bqty = float(r["bought_qty"] or 0)
        busd = float(r["bought_usd"] or 0)
        squty = float(r["sold_qty"] or 0)
        susd = float(r["sold_usd"] or 0)
        avg_buy = busd / bqty if bqty > 0 else 0.0
        avg_sell = susd / squty if squty > 0 else 0.0
        matched = min(bqty, squty)
        realized = (avg_sell - avg_buy) * matched
        roi = realized / busd if busd > 0 else 0.0
        multiple = 1 + roi
        peak_price = peaks[mint]
        fbp = float(r["first_buy_price"] or 0)
        entry_frac = fbp / peak_price if peak_price > 0 else None
        has_unbacked = squty > bqty * 1.05
        out.append((mint, classes[mint], r["wallet"], int(r["n_buys"]), int(r["n_sells"]),
                    busd, susd, bqty, squty, realized, roi, multiple, entry_frac,
                    dune_fetch._parse_ts(r["first_buy_time"]), int(r["first_buy_slot"]),
                    dune_fetch._parse_ts(r["last_sell_time"]), has_unbacked))
    if out:
        con.executemany("INSERT INTO early_pnl VALUES (" + ",".join(["?"] * 17) + ")", out)
    return rows


def run(args) -> None:
    con = db.connect()
    db.ensure_schema(con)
    con.execute(EARLY_PNL_SCHEMA)

    have = con.execute("SELECT count(*) FROM early_pnl").fetchone()[0]
    if args.refresh or not have:
        _fetch_early(con, args.since, args.early_mc, args.min_bought)
    else:
        print(f"[cache] early_pnl уже заполнена ({have} строк); --refresh для перезабора")

    # диагностика знаменателя
    dd = con.execute(
        "SELECT class, count(*) AS n_rows, count(DISTINCT wallet) AS n_wallets, "
        "count(DISTINCT mint) AS n_tokens FROM early_pnl GROUP BY class"
    ).fetchall()
    print("\nЗнаменатель (ранние входы по классам токенов):")
    for cls, n_rows, n_wallets, n_tokens in dd:
        print(f"  {cls:<7} ранних входов: {n_rows:>6}, кошельков: {n_wallets:>6}, токенов: {n_tokens:>3}")

    wm = args.win_mult
    scores = con.execute(
        """
        SELECT wallet,
            count(*)                                    AS n_early,
            count(*) FILTER (WHERE class='pumped')      AS n_pumped,
            count(*) FILTER (WHERE class='dead')        AS n_dead,
            count(*) FILTER (WHERE multiple >= ?)       AS n_win,
            round(count(*) FILTER (WHERE multiple >= ?) * 1.0 / count(*), 3) AS hit_rate,
            round(sum(realized))                        AS agg_realized,
            round(median(roi), 2)                       AS median_roi,
            round(avg(entry_frac), 4)                   AS avg_entry_frac,
            count(*) FILTER (WHERE has_unbacked)        AS n_unbk
        FROM early_pnl
        GROUP BY wallet
        HAVING count(*) >= ?
        ORDER BY n_win DESC, hit_rate DESC, agg_realized DESC
        LIMIT 30
        """,
        [wm, wm, args.min_early],
    ).fetchall()

    print(f"\nТОП кошельков (win = multiple >= {wm}x, min_early >= {args.min_early}):")
    print(f"{'wallet':<46} {'early':>5} {'pump':>4} {'dead':>4} {'win':>3} "
          f"{'hit':>5} {'realized$':>10} {'medROI':>6} {'entryF':>7} {'unbk':>4}")
    for w, ne, npu, nde, nw, hr, ar, mroi, ef, nu in scores:
        print(f"{w:<46} {ne:>5} {npu:>4} {nde:>4} {nw:>3} {str(hr):>5} "
              f"{str(ar):>10} {str(mroi):>6} {str(ef):>7} {nu:>4}")

    con.close()
    print("\nГотово (Модуль C). Дальше: скоринг/ранжирование + train/validation split.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Модуль C: кросс-токенная агрегация")
    ap.add_argument("--since", default="2026-06-26")
    ap.add_argument("--early-mc", type=float, default=50_000, help="ранний вход: MC <= этого (абс., без look-ahead)")
    ap.add_argument("--min-bought", type=float, default=10)
    ap.add_argument("--win-mult", type=float, default=5.0)
    ap.add_argument("--min-early", type=int, default=2)
    ap.add_argument("--refresh", action="store_true")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
