"""Модуль B — построение universe: когорта pump.fun токенов (запампившие + мёртвые).

Анти-survivorship: в universe входят НЕ только запампившие, но и выборка мёртвых —
чтобы знаменатель hit_rate в Модуле C считался по ВСЕМ ранним входам, а не по победам.

Сырая когорта кэшируется в DuckDB (universe_cohort) → калибровка порогов бесплатна,
без повторных запросов к Dune. --refresh форсит перезабор.

CLI:
  .venv\\Scripts\\python.exe -m src.universe \
      --launch-start 2026-06-27 --launch-end 2026-06-29 --maturity-days 5 \
      --peak-min 2000000 --min-pumped-vol 50000 --dead-max 300000 \
      --dead-min-traders 15 --min-trades 20 --n-per-class 25 [--refresh]
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

from . import db, dune_fetch

SQL_DIR = Path(__file__).resolve().parent / "sql"


def _fetch_cohort(scan_start, scan_end, launch_start, launch_end, min_trades) -> list[dict]:
    sql = (SQL_DIR / "universe.sql").read_text(encoding="utf-8")
    sql = (sql.replace("{{scan_start}}", scan_start)
              .replace("{{scan_end}}", scan_end)
              .replace("{{launch_start}}", launch_start)
              .replace("{{launch_end}}", launch_end)
              .replace("{{min_trades}}", str(min_trades)))
    print(f"[dune] universe cohort: launch [{launch_start}..{launch_end}], "
          f"scan [{scan_start}..{scan_end}], min_trades={min_trades} ...")
    return dune_fetch.run_sql(sql)


def _store_cohort(con, rows: list[dict], append: bool = False) -> None:
    tuples = [
        (r["mint"], dune_fetch._parse_ts(r["first_seen"]), dune_fetch._parse_ts(r["last_seen"]),
         int(r["n_trades"]), int(r["n_traders"]), float(r["usd_vol"] or 0),
         float(r["peak_mc_max"] or 0), float(r["peak_mc_p99"] or 0))
        for r in rows
    ]
    if append:  # дополнить: убрать только пересекающиеся mint, остальное сохранить
        mints = [t[0] for t in tuples]
        if mints:
            con.execute(
                f"DELETE FROM universe_cohort WHERE mint IN ({','.join('?' * len(mints))})", mints)
    else:
        con.execute("DELETE FROM universe_cohort")
    con.executemany("INSERT INTO universe_cohort VALUES (?,?,?,?,?,?,?,?)", tuples)


def build(args) -> None:
    con = db.connect()
    db.ensure_schema(con)

    have = con.execute("SELECT count(*) FROM universe_cohort").fetchone()[0]
    if args.refresh or args.append or not have:
        launch_start, launch_end = args.launch_start, args.launch_end
        scan_start = (datetime.strptime(launch_start, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        scan_end = (datetime.strptime(launch_end, "%Y-%m-%d") + timedelta(days=args.maturity_days)).strftime("%Y-%m-%d")
        rows = _fetch_cohort(scan_start, scan_end, launch_start, launch_end, args.min_trades)
        _store_cohort(con, rows, append=args.append)
        total = con.execute("SELECT count(*) FROM universe_cohort").fetchone()[0]
        print(f"[dune] когорта {'дополнена' if args.append else 'закэширована'}: +{len(rows)}, всего {total}")
    else:
        print(f"[cache] использую закэшированную когорту ({have} токенов); --refresh для перезабора")

    coh = con.execute(
        """SELECT mint, n_trades, n_traders, usd_vol, peak_mc_max, peak_mc_p99, first_seen, last_seen
           FROM universe_cohort ORDER BY peak_mc_p99 DESC"""
    ).fetchall()
    cols = ["mint", "n_trades", "n_traders", "usd_vol", "peak_mc_max", "peak_mc_p99", "first_seen", "last_seen"]
    coh = [dict(zip(cols, r)) for r in coh]

    print("\nТоп когорты по peak_mc_p99 (для калибровки):")
    print(f"{'mint':<46} {'trades':>7} {'traders':>8} {'peak_mc_p99':>13} {'usd_vol':>12}")
    for r in coh[:12]:
        print(f"{r['mint']:<46} {r['n_trades']:>7} {r['n_traders']:>8} "
              f"{round(r['peak_mc_p99']):>13} {round(r['usd_vol']):>12}")

    pumped, dead, middle = [], [], []
    for r in coh:
        p99, vol, ntr = r["peak_mc_p99"], r["usd_vol"], r["n_traders"]
        # верхний потолок пика: >peak_max = артефакт decimals (реальные pump.fun максимум ~$362M)
        if args.peak_min <= p99 <= args.peak_max and vol >= args.min_pumped_vol:
            pumped.append(r)
        elif p99 <= args.dead_max and ntr >= args.dead_min_traders:
            dead.append(r)
        else:
            middle.append(r)

    print(f"\n  pumped ({args.peak_min:,}<=p99<={args.peak_max:,} и vol>={args.min_pumped_vol:,}): {len(pumped)}")
    print(f"  dead   (p99<={args.dead_max:,} и traders>={args.dead_min_traders}): {len(dead)}")
    print(f"  middle/отсеяно: {len(middle)}")

    rng = random.Random(42)
    n = args.n_per_class
    pumped_s = rng.sample(pumped, min(n, len(pumped)))
    dead_s = rng.sample(dead, min(n, len(dead)))

    con.execute("DELETE FROM universe")
    for cls, sample in (("pumped", pumped_s), ("dead", dead_s)):
        for r in sample:
            con.execute(
                "INSERT INTO universe VALUES (?,?,?,?,?,?,?,?,?)",
                [r["mint"], r["first_seen"], r["last_seen"], r["n_trades"], r["n_traders"],
                 r["usd_vol"], r["peak_mc_max"], r["peak_mc_p99"], cls],
            )
    con.close()

    print(f"\n=== UNIVERSE сохранён: {len(pumped_s)} pumped + {len(dead_s)} dead ===")
    _print_sample("PUMPED", pumped_s)
    _print_sample("DEAD", dead_s)


def _print_sample(title: str, rows: list[dict], k: int = 8) -> None:
    print(f"\n{title} (первые {min(k, len(rows))}):")
    print(f"{'mint':<46} {'trades':>7} {'traders':>8} {'peak_mc_p99':>13} {'usd_vol':>12}")
    for r in rows[:k]:
        print(f"{r['mint']:<46} {r['n_trades']:>7} {r['n_traders']:>8} "
              f"{round(r['peak_mc_p99']):>13} {round(r['usd_vol']):>12}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Модуль B: построение universe (когорта pump.fun)")
    ap.add_argument("--launch-start", default="2026-06-27")
    ap.add_argument("--launch-end", default="2026-06-29")
    ap.add_argument("--maturity-days", type=int, default=5)
    ap.add_argument("--peak-min", type=int, default=2_000_000, help="pumped если peak_mc_p99 >= этого")
    ap.add_argument("--peak-max", type=int, default=1_000_000_000, help="потолок пика (>этого = артефакт decimals)")
    ap.add_argument("--min-pumped-vol", type=int, default=50_000, help="+ usd_vol >= этого (анти-артефакт)")
    ap.add_argument("--dead-max", type=int, default=300_000, help="dead если peak_mc_p99 <= этого")
    ap.add_argument("--dead-min-traders", type=int, default=15, help="+ n_traders >= этого (нужны ранние покупатели)")
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--n-per-class", type=int, default=25)
    ap.add_argument("--refresh", action="store_true", help="перезабрать когорту из Dune (перезатереть)")
    ap.add_argument("--append", action="store_true", help="ДОПОЛНИТЬ когорту новым окном (не затирать)")
    build(ap.parse_args())


if __name__ == "__main__":
    main()
