"""Решающий тест E v2: temporal split с funder+co-slot акторами (схлопнута ротация кошельков).

Вопрос: если селективный трейдер ротирует адреса, схлопывание по фандеру должно вернуть
устойчивость на out-of-sample. Всё локально (кэш), Dune не нужен.

Run:  .venv\\Scripts\\python.exe -m src.funder_test [--split 2026-05-01] [--cex-max 10]
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime

from . import db


class _UF:
    def __init__(self):
        self.p: dict = {}

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="2026-05-01")
    ap.add_argument("--win-mult", type=float, default=5.0)
    ap.add_argument("--cex-max", type=int, default=10)
    args = ap.parse_args()

    con = db.connect()
    clusters = con.execute("SELECT wallet, cluster_id FROM clusters WHERE cluster_id IS NOT NULL").fetchall()
    funders = con.execute("SELECT wallet, funder FROM wallet_funder").fetchall()
    fp = {w: (tt or 0) for w, tt in con.execute("SELECT wallet, total_tokens FROM wallet_footprint").fetchall()}
    rows = con.execute(
        "SELECT e.mint, e.wallet, e.multiple, u.first_seen "
        "FROM early_pnl e JOIN universe u ON u.mint = e.mint").fetchall()
    con.close()

    uf = _UF()
    # co-slot кластеры
    cl_groups: dict = defaultdict(list)
    for w, cid in clusters:
        cl_groups[cid].append(w)
    for ws in cl_groups.values():
        for w in ws[1:]:
            uf.union(ws[0], w)
    # funder (только личные, не CEX)
    f_groups: dict = defaultdict(list)
    for w, f in funders:
        f_groups[f].append(w)
    n_funder_merges = 0
    for f, ws in f_groups.items():
        if 2 <= len(ws) <= args.cex_max:
            for w in ws[1:]:
                uf.union(ws[0], w)
            n_funder_merges += len(ws) - 1

    ent = uf.find
    actor_breadth: dict = defaultdict(int)
    for w, tt in fp.items():
        actor_breadth[ent(w)] += tt

    T = datetime.strptime(args.split, "%Y-%m-%d")
    aw, bw = defaultdict(int), defaultdict(int)
    for mint, wallet, mult, fs in rows:
        e = ent(wallet)
        win = 1 if (mult is not None and mult >= args.win_mult) else 0
        d = aw if fs < T else bw
        d[(e, mint)] = max(d[(e, mint)], win)
    ea, wa, eb, wb = defaultdict(int), defaultdict(int), defaultdict(int), defaultdict(int)
    for (e, _), win in aw.items():
        ea[e] += 1; wa[e] += win
    for (e, _), win in bw.items():
        eb[e] += 1; wb[e] += win
    base_e, base_w = sum(eb.values()), sum(wb.values())
    base_hit = base_w / base_e if base_e else 0.0

    n_cand = len(fp)
    n_actors = len({ent(w) for w in fp})
    print(f"Схлопывание: {n_cand} кандидатов-кошельков → {n_actors} акторов "
          f"(funder-мержей: {n_funder_merges})")
    print(f"Temporal split {args.split} (train<split → validation>=split), baseline hit={base_hit:.3f}\n")
    print(f"{'breadth<=':>10} {'clean actors':>13} {'val entries':>12} {'selected hit':>13} {'lift':>6}")
    for bmax in (300, 800, 1500, 3000, 20000, 10_000_000):
        selected = {e for e in ea if ea[e] >= 2 and wa[e] >= 1 and actor_breadth.get(e, 0) <= bmax}
        se = sum(eb.get(e, 0) for e in selected)
        sw = sum(wb.get(e, 0) for e in selected)
        hit = sw / se if se else 0.0
        lift = (hit / base_hit) if base_hit else 0.0
        print(f"{bmax:>10} {len(selected):>13} {se:>12} {hit:>13.3f} {lift:>5.1f}x")


if __name__ == "__main__":
    main()
