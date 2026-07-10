"""Контур 2 — генератор флоу-watchlist для live-мониторинга (гибрид: watchlist + velocity).

Наш вывод: селективные кошельки не работают, а ШИРОКИЙ ранний флоу персистентен (~4x).
Значит watchlist = широкие устойчивые ранние покупатели, схлопнутые в АКТОРОВ (funder+co-slot),
без мега-spray (широта > breadth_max — покупают всё, шум) и без тривиальных.

Выход: output/flow_watchlist.json — акторы + их кошельки + вес (для подписки монитором).
Всё локально из кэша (early_pnl, footprint, clusters, wallet_funder). Dune не нужен.

Run:  .venv\\Scripts\\python.exe -m src.flow_watchlist [--min-early 4] [--breadth-max 20000] [--top 300]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from . import config, db


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


def _actor_map(con, cex_max: int):
    """Схлопнуть кошельки в акторов: co-slot кластеры + личные funder'ы (не CEX)."""
    uf = _UF()
    try:
        cl = defaultdict(list)
        for w, cid in con.execute(
                "SELECT wallet, cluster_id FROM clusters WHERE cluster_id IS NOT NULL").fetchall():
            cl[cid].append(w)
        for ws in cl.values():
            for w in ws[1:]:
                uf.union(ws[0], w)
    except Exception:  # noqa: BLE001
        pass
    try:
        fg = defaultdict(list)
        for w, f in con.execute("SELECT wallet, funder FROM wallet_funder").fetchall():
            fg[f].append(w)
        for f, ws in fg.items():
            if 2 <= len(ws) <= cex_max:
                for w in ws[1:]:
                    uf.union(ws[0], w)
    except Exception:  # noqa: BLE001
        pass
    return uf


def build(args) -> None:
    con = db.connect()
    fp = {w: (tt or 0) for w, tt in
          con.execute("SELECT wallet, total_tokens FROM wallet_footprint").fetchall()}
    # per-wallet n_early, n_win
    rows = con.execute(
        "SELECT wallet, count(*) AS n_early, "
        "count(*) FILTER (WHERE multiple >= ?) AS n_win, sum(realized) AS agg "
        "FROM early_pnl GROUP BY wallet", [args.win_mult]).fetchall()
    uf = _actor_map(con, args.cex_max)
    con.close()

    # агрегируем в акторов
    actors: dict = defaultdict(lambda: {"wallets": [], "n_early": 0, "n_win": 0,
                                        "agg": 0.0, "breadth": 0})
    for w, n_early, n_win, agg in rows:
        a = uf.find(w)
        A = actors[a]
        A["wallets"].append(w)
        A["n_early"] += n_early           # сумма по членам (ротация = разные токены)
        A["n_win"] += n_win
        A["agg"] += (agg or 0.0)
        A["breadth"] += fp.get(w, 0)

    # фильтр флоу-акторов
    flow = []
    for a, A in actors.items():
        if (A["n_early"] >= args.min_early
                and args.breadth_min <= A["breadth"] <= args.breadth_max):
            flow.append({
                "actor_id": a,
                "wallets": A["wallets"],
                "n_wallets": len(A["wallets"]),
                "n_early": A["n_early"],
                "n_win": A["n_win"],
                "hit_rate": round(A["n_win"] / A["n_early"], 3) if A["n_early"] else 0,
                "breadth": A["breadth"],
                "agg_realized": round(A["agg"]),
                "weight": round(A["n_win"] + 0.3 * A["n_early"], 2),  # вес для конфлюенс-скора
            })
    flow.sort(key=lambda x: x["weight"], reverse=True)
    flow = flow[: args.top]

    (config.OUTPUT_DIR / "flow_watchlist.json").write_text(
        json.dumps(flow, indent=2, ensure_ascii=False), encoding="utf-8")
    n_wallets = sum(f["n_wallets"] for f in flow)
    print(f"Флоу-watchlist: {len(flow)} акторов, {n_wallets} кошельков → output/flow_watchlist.json\n")
    print(f"{'actor':<46} {'wall':>4} {'early':>5} {'win':>4} {'hit':>5} {'breadth':>8} {'weight':>6}")
    for f in flow[:25]:
        print(f"{f['actor_id']:<46} {f['n_wallets']:>4} {f['n_early']:>5} {f['n_win']:>4} "
              f"{f['hit_rate']:>5} {f['breadth']:>8} {f['weight']:>6}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Контур 2: флоу-watchlist")
    ap.add_argument("--win-mult", type=float, default=5.0)
    ap.add_argument("--min-early", type=int, default=4, help="мин. суммарных ранних входов актора")
    ap.add_argument("--breadth-min", type=int, default=50, help="исключить тривиальных")
    ap.add_argument("--breadth-max", type=int, default=20000, help="исключить мега-spray (покупают всё)")
    ap.add_argument("--cex-max", type=int, default=10)
    ap.add_argument("--top", type=int, default=300)
    build(ap.parse_args())


if __name__ == "__main__":
    main()
