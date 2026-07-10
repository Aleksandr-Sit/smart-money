"""Модуль D (флаги copyability/insider/bot) + скоринг/ранжирование → watchlist.

Работает на кэшированной early_pnl (Dune не нужен). Флаги:
  - snipe (деплой-блок/same-slot): first_buy_slot близко к launch-слоту токена → не успеть повторить.
  - unbacked (инсайдер/трансфер): продажи без DEX-базиса (уже в early_pnl.has_unbacked).
  - too_fast: hold < MIN_HOLD_SECONDS → бот/вошинг, низкая копируемость.
  - bot_like: слишком много сделок на токен ИЛИ доля быстрых выходов высокая.

score = w_hit*hit_rate + w_nwin*log1p(n_win) + w_rec*recency + w_profit*log1p(agg_realized)
        − p_snipe*frac_snipe − p_bot*bot_like − p_unbk*frac_unbacked

CLI:
  .venv\\Scripts\\python.exe -m src.score [--win-mult 5] [--min-early 2] [--min-wins 1]
      [--snipe-slots 2] [--min-hold-sec 300] [--recency-days 30] [--top 200]
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone

from . import config, db

WEIGHTS_DEFAULT = {"hit_rate": 1.0, "log_n_wins": 1.0, "recency": 0.5, "agg_profit": 0.5}
PENALTY = {"snipe": 1.0, "bot": 1.0, "unbacked": 0.5}


def _wallet_rows(con, win_mult, snipe_slots, min_hold_sec):
    return con.execute(
        """
        WITH launch AS (
            SELECT mint, min(first_buy_slot) AS launch_slot FROM early_pnl GROUP BY mint
        ),
        enr AS (
            SELECT e.*,
                   (e.first_buy_slot - l.launch_slot) AS slot_delay,
                   CASE WHEN e.last_sell_time IS NOT NULL
                        THEN date_diff('second', e.first_buy_time, e.last_sell_time) END AS hold_s
            FROM early_pnl e JOIN launch l USING (mint)
        )
        SELECT wallet,
            count(*)                                             AS n_early,
            count(*) FILTER (WHERE class='pumped')               AS n_pumped,
            count(*) FILTER (WHERE class='dead')                 AS n_dead,
            count(*) FILTER (WHERE multiple >= ?)                AS n_win,
            sum(realized)                                        AS agg_realized,
            median(roi)                                          AS median_roi,
            avg(entry_frac)                                      AS avg_entry_frac,
            median(hold_s)                                       AS median_hold_s,
            avg(CASE WHEN slot_delay <= ? THEN 1.0 ELSE 0.0 END) AS frac_snipe,
            avg(CASE WHEN has_unbacked THEN 1.0 ELSE 0.0 END)    AS frac_unbacked,
            avg(CASE WHEN hold_s IS NOT NULL AND hold_s < ? THEN 1.0 ELSE 0.0 END) AS frac_too_fast,
            sum(n_buys + n_sells) * 1.0 / count(*)               AS avg_trades_per_token,
            max(greatest(coalesce(first_buy_time,  TIMESTAMP '1970-01-01'),
                         coalesce(last_sell_time, TIMESTAMP '1970-01-01'))) AS last_active
        FROM enr GROUP BY wallet
        """,
        [win_mult, snipe_slots, min_hold_sec],
    ).fetchall()


def build(args) -> None:
    con = db.connect()
    try:
        params = config.load_params()
        weights = {**WEIGHTS_DEFAULT, **(params.get("scoring", {}).get("weights", {}) or {})}
    except Exception:  # noqa: BLE001
        weights = WEIGHTS_DEFAULT

    cols = ["wallet", "n_early", "n_pumped", "n_dead", "n_win", "agg_realized", "median_roi",
            "avg_entry_frac", "median_hold_s", "frac_snipe", "frac_unbacked", "frac_too_fast",
            "avg_trades_per_token", "last_active"]
    raw = [dict(zip(cols, r)) for r in
           _wallet_rows(con, args.win_mult, args.snipe_slots, args.min_hold_sec)]
    try:  # Модуль E: кластеры (если посчитаны src.cluster)
        clusters = {w: (cid, sz) for w, cid, sz in
                    con.execute("SELECT wallet, cluster_id, cluster_size FROM clusters").fetchall()}
    except Exception:  # noqa: BLE001
        clusters = {}
    try:  # широта следа (если посчитана src.footprint) — детекция spray-ботов
        footprint = {w: tt for w, tt in
                     con.execute("SELECT wallet, total_tokens FROM wallet_footprint").fetchall()}
    except Exception:  # noqa: BLE001
        footprint = {}
    con.close()

    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC (DuckDB timestamps naive)
    scored = []
    for w in raw:
        w["cluster_id"], w["cluster_size"] = clusters.get(w["wallet"], (None, 1))
        w["total_tokens"] = footprint.get(w["wallet"])   # широта следа (spray-бот)
        w["hit_rate"] = w["n_win"] / w["n_early"] if w["n_early"] else 0.0
        # recency
        la = w["last_active"]
        days = (now - la).days if la else 9999
        w["recency"] = 1.0 if days <= args.recency_days else max(0.0, 1 - (days - args.recency_days) / args.recency_days)
        # bot / copyability: + широта (spray-бот берёт тысячи токенов)
        spray = (w["total_tokens"] or 0) > args.breadth_max
        w["bot_like"] = spray or (w["avg_trades_per_token"] > args.bot_trades_per_token) or (w["frac_too_fast"] > 0.5)
        w["copyability"] = (not w["bot_like"]) and w["frac_snipe"] < 0.34 and w["frac_unbacked"] < 0.34
        # score
        w["score"] = round(
            weights["hit_rate"] * w["hit_rate"]
            + weights["log_n_wins"] * math.log1p(w["n_win"])
            + weights["recency"] * w["recency"]
            + weights["agg_profit"] * math.log1p(max(0.0, w["agg_realized"]))
            - PENALTY["snipe"] * w["frac_snipe"]
            - PENALTY["bot"] * (1.0 if w["bot_like"] else 0.0)
            - PENALTY["unbacked"] * w["frac_unbacked"], 4)
        scored.append(w)

    # отсев one-hit-wonders + шума
    kept = [w for w in scored if w["n_early"] >= args.min_early and w["n_win"] >= args.min_wins]
    kept.sort(key=lambda w: w["score"], reverse=True)
    watch = kept[: args.top]

    _write_outputs(watch)

    passing_real = [w for w in scored if w["n_early"] >= 5 and w["n_win"] >= 3]
    # уникальные сущности = кластеры схлопнуты в одну (cluster_id), синглтоны — сами по себе
    entities = len({(w["cluster_id"] or w["wallet"]) for w in kept})
    print(f"\nВсего кошельков: {len(scored)}")
    print(f"Проходят БОЕВОЙ порог (n_early>=5, n_win>=3): {len(passing_real)}")
    print(f"Проходят pilot-порог (n_early>={args.min_early}, n_win>={args.min_wins}): {len(kept)}")
    print(f"  из них УНИКАЛЬНЫХ сущностей (кластеры схлопнуты): {entities}")
    print(f"Watchlist сохранён: output/watchlist.csv / .json (top {len(watch)})\n")

    print(f"{'wallet':<46} {'score':>6} {'early':>5} {'win':>3} {'hit':>5} "
          f"{'real$':>8} {'tokens':>7} {'copy':>4} {'bot':>3} {'clust':>5}")
    for w in watch[:30]:
        cl = f"{w['cluster_id']}:{w['cluster_size']}" if w.get("cluster_id") else "-"
        print(f"{w['wallet']:<46} {w['score']:>6} {w['n_early']:>5} {w['n_win']:>3} "
              f"{round(w['hit_rate'],2):>5} {round(w['agg_realized']):>8} "
              f"{str(w['total_tokens']):>7} "
              f"{'Y' if w['copyability'] else 'n':>4} {'Y' if w['bot_like'] else 'n':>3} {cl:>5}")


def _write_outputs(watch: list[dict]) -> None:
    fields = ["wallet", "score", "hit_rate", "n_win", "n_early", "n_pumped", "n_dead",
              "agg_realized", "median_roi", "avg_entry_frac", "median_hold_s",
              "copyability", "bot_like", "total_tokens", "cluster_id", "cluster_size",
              "frac_snipe", "frac_unbacked", "last_active", "solscan_url", "gmgn_url"]
    out = []
    for w in watch:
        row = {k: w.get(k) for k in fields if k in w}
        row["solscan_url"] = f"https://solscan.io/account/{w['wallet']}"
        row["gmgn_url"] = f"https://gmgn.ai/sol/address/{w['wallet']}"
        row["last_active"] = w["last_active"].isoformat() if w.get("last_active") else None
        out.append(row)
    (config.OUTPUT_DIR / "watchlist.json").write_text(
        json.dumps(out, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    with open(config.OUTPUT_DIR / "watchlist.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        wr.writerows(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Модуль D + скоринг → watchlist")
    ap.add_argument("--win-mult", type=float, default=5.0)
    ap.add_argument("--min-early", type=int, default=2, help="pilot; боевой = 5")
    ap.add_argument("--min-wins", type=int, default=1, help="pilot; боевой = 3")
    ap.add_argument("--snipe-slots", type=int, default=2, help="<= этого от launch-слота = снайп деплоя")
    ap.add_argument("--min-hold-sec", type=int, default=300)
    ap.add_argument("--bot-trades-per-token", type=float, default=40)
    ap.add_argument("--breadth-max", type=int, default=300, help="total_tokens > этого = spray-бот")
    ap.add_argument("--recency-days", type=int, default=30)
    ap.add_argument("--top", type=int, default=200)
    build(ap.parse_args())


if __name__ == "__main__":
    main()
