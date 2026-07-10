"""Модуль E v2 — funder-кластеризация: схлопнуть РОТИРУЕМЫЕ кошельки по общему фандеру.

Гипотеза: селективный трейдер берёт свежий адрес на сделку → «одноразовые» кошельки.
Фандер (кто первым залил SOL) общий → это один актор. CEX/сервисы (заливают многих) НЕ мержим.

Тянет из system_program_solana.system_program_call_transfer: account_from -> account_to.
Run:  .venv\\Scripts\\python.exe -m src.funder [--since 2026-01-01] [--force] [--cex-max 10]
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from . import db, dune_fetch

FUNDER_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_funder (
    wallet VARCHAR, funder VARCHAR, funded_at TIMESTAMP, n_inflows BIGINT
);
"""


def fetch(since: str, min_early: int, min_wins: int, win_mult: float, force: bool) -> None:
    con = db.connect()
    con.execute(FUNDER_SCHEMA)
    cands = [w for (w,) in con.execute(
        "SELECT wallet FROM early_pnl GROUP BY wallet "
        "HAVING count(*) >= ? AND count(*) FILTER (WHERE multiple >= ?) >= ?",
        [min_early, win_mult, min_wins]).fetchall()]
    if not force:
        have = {w for (w,) in con.execute("SELECT wallet FROM wallet_funder").fetchall()}
        cands = [w for w in cands if w not in have]
    if not cands:
        print("[cache] funder всех кандидатов уже есть")
        con.close()
        return

    print(f"[dune] funder по {len(cands)} кандидатам, с {since} ...")
    wl = ",".join("'" + w + "'" for w in cands)
    sql = f"""
    SELECT account_to AS wallet,
        min_by(account_from, call_block_time) AS funder,
        min(call_block_time)                  AS funded_at,
        count(*)                              AS n_inflows
    FROM system_program_solana.system_program_call_transfer
    WHERE account_to IN ({wl})
      AND call_block_time > TIMESTAMP '{since}'
    GROUP BY 1
    """
    rows = dune_fetch.run_sql(sql)
    con.execute("DELETE FROM wallet_funder")
    con.executemany(
        "INSERT INTO wallet_funder VALUES (?,?,?,?)",
        [(r["wallet"], r["funder"], dune_fetch._parse_ts(r["funded_at"]),
          int(r["n_inflows"])) for r in rows])
    con.close()
    print(f"[dune] funder получен для {len(rows)} кошельков")


def summarize(cex_max: int) -> None:
    con = db.connect()
    rows = con.execute("SELECT wallet, funder FROM wallet_funder").fetchall()
    con.close()
    by_funder: dict[str, list[str]] = defaultdict(list)
    for w, f in rows:
        by_funder[f].append(w)
    shared = {f: ws for f, ws in by_funder.items() if len(ws) > 1}
    cex = {f: ws for f, ws in shared.items() if len(ws) > cex_max}
    actors = {f: ws for f, ws in shared.items() if len(ws) <= cex_max}
    merged = sum(len(ws) for ws in actors.values())
    print(f"\nКандидатов с фандером: {len(rows)}")
    print(f"Фандеров, заливших >1 кандидата: {len(shared)}")
    print(f"  из них вероятные CEX/сервисы (>{cex_max} кандидатов): {len(cex)} "
          f"(суммарно {sum(len(ws) for ws in cex.values())} кошельков — НЕ мержим)")
    print(f"  вероятные ЛИЧНЫЕ мастер-фандеры (2..{cex_max}): {len(actors)} "
          f"→ схлопывают {merged} кошельков в {len(actors)} акторов")
    print("\nТоп общих фандеров (личные, по числу схлопнутых кошельков):")
    for f, ws in sorted(actors.items(), key=lambda kv: -len(kv[1]))[:15]:
        print(f"  funder {f[:20]}.. → {len(ws)} кошельков: {', '.join(w[:8] for w in ws[:6])}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Модуль E v2: funder-кластеризация")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--min-early", type=int, default=2)
    ap.add_argument("--min-wins", type=int, default=1)
    ap.add_argument("--win-mult", type=float, default=5.0)
    ap.add_argument("--cex-max", type=int, default=10, help="фандер >этого кандидатов = CEX/сервис")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--summary-only", action="store_true", help="не тянуть, только сводка из кэша")
    a = ap.parse_args()
    if not a.summary_only:
        fetch(a.since, a.min_early, a.min_wins, a.win_mult, a.force)
    summarize(a.cex_max)


if __name__ == "__main__":
    main()
