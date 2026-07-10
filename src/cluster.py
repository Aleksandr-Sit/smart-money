"""Модуль E — кластеризация кошельков (эвристика temporal/slot co-occurrence).

Идея: если два кошелька РЕГУЛЯРНО покупают ОДНИ И ТЕ ЖЕ токены в ОДНОМ/соседних слотах —
это, вероятно, один актор на многих кошельках (или скоординированная группа). Такие кошельки
надо схлопнуть в cluster_id, иначе watchlist переоценивает «широту» (15 кошельков = 1 актор).

Данные — из кэша early_pnl (Dune не нужен). Скоуп — кандидаты watchlist (мало, дёшево).
Funding/CEX-based кластеризация (нужны доп. on-chain данные) — отдельный шаг, здесь НЕ делается.

CLI:
  .venv\\Scripts\\python.exe -m src.cluster [--win-mult 5] [--slot-window 3]
      [--min-shared 2] [--min-early 2] [--min-wins 1]
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from . import db


class _UF:
    """Union-Find для связных компонент."""
    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def build(args) -> None:
    con = db.connect()

    # кандидаты: n_early>=min_early и n_win>=min_wins (win = multiple>=win_mult)
    cand = con.execute(
        """
        SELECT wallet FROM early_pnl
        GROUP BY wallet
        HAVING count(*) >= ? AND count(*) FILTER (WHERE multiple >= ?) >= ?
        """,
        [args.min_early, args.win_mult, args.min_wins],
    ).fetchall()
    cand = {w for (w,) in cand}
    print(f"Кандидатов для кластеризации: {len(cand)}")

    rows = con.execute(
        "SELECT wallet, mint, first_buy_slot FROM early_pnl WHERE first_buy_slot IS NOT NULL"
    ).fetchall()

    # wallet -> {mint: slot}, только кандидаты
    wmap: dict[str, dict[str, int]] = defaultdict(dict)
    # mint -> [(wallet, slot)] для эффективного попарного перебора внутри токена
    per_token: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for w, m, s in rows:
        if w in cand:
            wmap[w][m] = s
            per_token[m].append((w, s))

    # накапливаем совпадения пар: сколько токенов оба взяли в пределах slot-window
    pair_hits: dict[tuple[str, str], int] = defaultdict(int)
    for m, lst in per_token.items():
        lst.sort(key=lambda x: x[1])
        for i in range(len(lst)):
            wi, si = lst[i]
            for j in range(i + 1, len(lst)):
                wj, sj = lst[j]
                if sj - si > args.slot_window:
                    break
                if wi != wj:
                    pair_hits[(min(wi, wj), max(wi, wj))] += 1

    uf = _UF()
    for w in cand:
        uf.find(w)
    edges = 0
    for (a, b), n in pair_hits.items():
        if n >= args.min_shared:
            uf.union(a, b)
            edges += 1

    clusters: dict[str, list[str]] = defaultdict(list)
    for w in cand:
        clusters[uf.find(w)].append(w)
    multi = {cid: ws for cid, ws in clusters.items() if len(ws) > 1}

    # записать в DuckDB
    con.execute("CREATE OR REPLACE TABLE clusters (wallet VARCHAR, cluster_id VARCHAR, cluster_size INTEGER)")
    cid_map = {}
    n = 0
    for cid, ws in sorted(multi.items(), key=lambda kv: -len(kv[1])):
        n += 1
        label = f"C{n}"
        for w in ws:
            cid_map[w] = (label, len(ws))
    for w in cand:
        lab, sz = cid_map.get(w, (None, 1))
        con.execute("INSERT INTO clusters VALUES (?,?,?)", [w, lab, sz])

    print(f"Связей (пар с co-occurrence>={args.min_shared}, slot_window={args.slot_window}): {edges}")
    print(f"Кластеров (>1 кошелька): {len(multi)}; кошельков в кластерах: {sum(len(v) for v in multi.values())}\n")

    # детали кластеров + агрегат по n_win (сколько «побед» реально уникальны)
    for cid, ws in sorted(multi.items(), key=lambda kv: -len(kv[1]))[:15]:
        agg = con.execute(
            f"""SELECT count(DISTINCT mint), count(*) FILTER (WHERE multiple >= {args.win_mult}),
                       round(avg(entry_frac),4), round(median(roi),2)
                FROM early_pnl WHERE wallet IN ({','.join('?' * len(ws))})""",
            ws,
        ).fetchone()
        print(f"cluster ({len(ws)} кош.): уник.токенов={agg[0]}, суммарно побед={agg[1]}, "
              f"avg entryF={agg[2]}, med ROI={agg[3]}")
        for w in ws[:6]:
            print(f"    {w}")
        if len(ws) > 6:
            print(f"    ... ещё {len(ws) - 6}")

    con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Модуль E: кластеризация по slot co-occurrence")
    ap.add_argument("--win-mult", type=float, default=5.0)
    ap.add_argument("--slot-window", type=int, default=3, help="макс. разница слотов для co-entry")
    ap.add_argument("--min-shared", type=int, default=2, help="мин. общих токенов с co-entry для связи")
    ap.add_argument("--min-early", type=int, default=2)
    ap.add_argument("--min-wins", type=int, default=1)
    build(ap.parse_args())


if __name__ == "__main__":
    main()
