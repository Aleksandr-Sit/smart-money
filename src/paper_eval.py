"""Оценка PAPER-PnL — форвард-валидация флоу-сигнала.

Читает output/paper_positions.jsonl, по каждой позиции тянет текущую цену/MC (DexScreener),
считает доходность (по цене, иначе по MC), выдаёт win-rate и медиану. Запускать периодически
после накопления сигналов.

Run:  .venv\\Scripts\\python.exe -m src.paper_eval
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone

from . import config, market


def main() -> None:
    path = config.OUTPUT_DIR / "paper_positions.jsonl"
    if not path.exists():
        print("нет paper_positions.jsonl")
        return
    positions = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    print(f"PAPER-позиций: {len(positions)}")

    rows = []
    for p in positions:
        info = market.token_info(p["token_mint"])
        cur_mc, cur_price = info.get("mc"), info.get("price_usd")
        e_price, e_mc = p.get("entry_price_usd"), p.get("entry_mc")
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(p["ts"])).total_seconds() / 3600
        except Exception:  # noqa: BLE001
            age_h = None
        if e_price and cur_price:
            ret = cur_price / e_price - 1
        elif e_mc and cur_mc:
            ret = cur_mc / e_mc - 1
        elif (e_price or e_mc) and not (cur_price or cur_mc) and age_h is not None and age_h > 1:
            ret = -1.0    # нет на DexScreener спустя >1ч → мёртвый токен (тотал-лосс)
        else:
            ret = None    # ещё рано судить (нет entry ИЛИ токен молодой без данных)
        rows.append({**p, "cur_mc": cur_mc, "ret": ret})

    scored = [r for r in rows if r["ret"] is not None]
    if scored:
        rets = [r["ret"] for r in scored]
        wins = sum(1 for x in rets if x > 0)
        print(f"\nОценено: {len(scored)} | win-rate: {wins/len(scored):.2f} | "
              f"median ret: {statistics.median(rets):+.2%} | mean: {statistics.mean(rets):+.2%}")
        for lvl in ("strong", "weak"):
            sub = [r["ret"] for r in scored if r.get("level") == lvl]
            if sub:
                w = sum(1 for x in sub if x > 0)
                print(f"  {lvl:<6}: n={len(sub)} win-rate {w/len(sub):.2f} median {statistics.median(sub):+.2%}")

    print(f"\n{'token':<14} {'lvl':<6} {'entry_mc':>10} {'cur_mc':>10} {'ret':>8}")
    for r in sorted(rows, key=lambda x: (x["ret"] is None, -(x["ret"] or 0))):
        ret = f"{r['ret']:+.1%}" if r["ret"] is not None else "n/a"
        print(f"{r['token_mint'][:12]:<14} {str(r.get('level')):<6} "
              f"{str(r.get('entry_mc')):>10} {str(r.get('cur_mc')):>10} {ret:>8}")


if __name__ == "__main__":
    main()
