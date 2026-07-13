"""Consumer-mode реплей: честная оценка входа с задержкой + калибровка выхода.

Источник: output/price_history.jsonl (траектории от price_track) + output/signals.log
(момент сигнала, level, n_actors). Для каждого сигнала симулируем вход С ЗАДЕРЖКОЙ
(бот +5с / человек +60с) по фактической цене в тот момент и прогоняем выход по политике
TP/SL/trailing/timeout — как если бы держали по правилам. PnL — за вычетом комиссии.

Две задачи:
  1) --compare : один и тот же выход, вход +5с vs +60с → видно, для кого есть edge.
  2) --grid    : перебор EXIT_CFG (TP/SL/trail/max-hold) → калибровка выхода данными.

Run:
  .venv\\Scripts\\python.exe -m src.replay                      # дефолт: сравнение режимов
  .venv\\Scripts\\python.exe -m src.replay --grid --delay 5
  .venv\\Scripts\\python.exe -m src.replay --selftest          # синтетика (без данных)
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict

from . import config


# ---------- загрузка ----------
def load_trajectories(rows: list[dict] | None = None) -> dict[str, list[tuple[float, float]]]:
    """mint -> отсортированный список (ts, price_usd)."""
    if rows is None:
        p = config.OUTPUT_DIR / "price_history.jsonl"
        rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip()] if p.exists() else []
    traj: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        if r.get("price_usd"):
            traj[r["mint"]].append((float(r["ts"]), float(r["price_usd"])))
    for m in traj:
        traj[m].sort()
    return traj


def load_signal_meta(rows: list[dict] | None = None) -> dict[str, dict]:
    """mint -> {ts, level, n_actors} по ПЕРВОМУ сигналу входа (не exit)."""
    if rows is None:
        p = config.OUTPUT_DIR / "signals.log"
        rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip()] if p.exists() else []
    meta: dict[str, dict] = {}
    for r in rows:
        sig = r.get("signal")
        if not sig:                     # exit-записи пропускаем
            continue
        mint = sig["token_mint"]
        if mint not in meta:            # первый сигнал = момент входа
            meta[mint] = {"ts": sig["ts"], "level": sig["level"], "n_actors": sig["n_actors"]}
    return meta


# ---------- симуляция одной сделки ----------
DEFAULT_EXIT = {"TP": 2.5, "SL": 0.5, "TRAIL": 0.35, "TRAIL_ARM": 1.5, "MAX_HOLD_S": 1800}


def _price_at(traj: list[tuple[float, float]], t: float) -> tuple[int, float] | None:
    """Первый сэмпл с ts >= t → (индекс, цена). None если траектория кончилась раньше."""
    for i, (ts, pr) in enumerate(traj):
        if ts >= t:
            return i, pr
    return None


def simulate(traj: list[tuple[float, float]], entry_ts: float, delay_s: float,
             cfg: dict, fee: float) -> dict | None:
    """Вход в entry_ts+delay, выход по политике. → {ret, reason, hold_s} или None (не вошли)."""
    ent = _price_at(traj, entry_ts + delay_s)
    if ent is None:
        return None                     # к моменту входа ликвидности/данных уже нет
    i0, entry_price = ent
    if entry_price <= 0:
        return None
    peak = entry_price
    for ts, price in traj[i0:]:
        mult = price / entry_price
        peak = max(peak, price)
        hold = ts - (entry_ts + delay_s)
        if mult >= cfg["TP"]:
            return {"ret": mult - 1 - fee, "reason": "tp", "hold_s": hold}
        if mult <= cfg["SL"]:
            return {"ret": mult - 1 - fee, "reason": "sl", "hold_s": hold}
        if peak / entry_price >= cfg["TRAIL_ARM"] and price <= peak * (1 - cfg["TRAIL"]):
            return {"ret": mult - 1 - fee, "reason": "trail", "hold_s": hold}
        if hold >= cfg["MAX_HOLD_S"]:
            return {"ret": mult - 1 - fee, "reason": "timeout", "hold_s": hold}
    last_ts, last_price = traj[-1]      # данные кончились раньше правил → выход по последней
    return {"ret": last_price / entry_price - 1 - fee, "reason": "end",
            "hold_s": last_ts - (entry_ts + delay_s)}


# ---------- агрегация ----------
def _summ(rets: list[float]) -> str:
    if not rets:
        return "n=0"
    w = sum(1 for x in rets if x > 0)
    return (f"n={len(rets):<4} win={w / len(rets):.2f} median={statistics.median(rets):+6.1%} "
            f"mean={statistics.mean(rets):+6.1%} sum={sum(rets):+.0%}")


def evaluate(delay_s: float, cfg: dict, fee: float,
             traj: dict, meta: dict) -> dict[str, list[float]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    no_entry = 0
    for mint, m in meta.items():
        t = traj.get(mint)
        if not t or len(t) < 2:
            continue
        res = simulate(t, m["ts"], delay_s, cfg, fee)
        if res is None:
            no_entry += 1
            continue
        buckets["all"].append(res["ret"])
        buckets[m["level"]].append(res["ret"])
        buckets[f"reason:{res['reason']}"].append(res["ret"])
    buckets["_no_entry"] = [0.0] * no_entry
    return buckets


def _print_eval(title: str, delay_s: float, cfg: dict, fee: float, traj: dict, meta: dict) -> None:
    b = evaluate(delay_s, cfg, fee, traj, meta)
    print(f"\n=== {title} · вход +{delay_s:.0f}с · fee {fee:.0%} · "
          f"TP {cfg['TP']} SL {cfg['SL']} trail {cfg['TRAIL']} hold {cfg['MAX_HOLD_S']:.0f}с ===")
    print(f"  ALL    {_summ(b.get('all', []))}")
    print(f"  strong {_summ(b.get('strong', []))}")
    print(f"  weak   {_summ(b.get('weak', []))}")
    print(f"  не вошли (нет цены к +{delay_s:.0f}с): {len(b.get('_no_entry', []))}")
    reasons = {k[7:]: v for k, v in b.items() if k.startswith("reason:")}
    for rn, rr in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        print(f"    {rn:<8} {_summ(rr)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Consumer-mode реплей сигналов")
    ap.add_argument("--delay", type=float, default=None, help="задержка входа, сек (для --grid)")
    ap.add_argument("--fee", type=float, default=0.06, help="round-trip издержки (доля)")
    ap.add_argument("--grid", action="store_true", help="перебор EXIT_CFG")
    ap.add_argument("--compare", action="store_true", help="сравнить вход +5с vs +60с (дефолт)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return

    traj, meta = load_trajectories(), load_signal_meta()
    tracked = sum(1 for m in meta if m in traj and len(traj[m]) >= 2)
    print(f"сигналов: {len(meta)} · с траекторией (≥2 точек): {tracked} · "
          f"всего токенов в истории: {len(traj)}")
    if tracked == 0:
        print("\n[!] Траекторий пока нет — price_track ещё копит данные. "
              "Запусти реплей через несколько часов после деплоя.")
        return

    if args.grid:
        delay = args.delay if args.delay is not None else 5.0
        print(f"\n--- GRID калибровка выхода · вход +{delay:.0f}с · fee {args.fee:.0%} ---")
        best = None
        for tp in (1.5, 2.0, 2.5, 3.0, 4.0):
            for sl in (0.4, 0.5, 0.6):
                for hold in (600, 1200, 1800, 3600):
                    cfg = {**DEFAULT_EXIT, "TP": tp, "SL": sl, "MAX_HOLD_S": hold}
                    rr = evaluate(delay, cfg, args.fee, traj, meta).get("all", [])
                    if len(rr) < 20:
                        continue
                    med = statistics.median(rr)
                    row = (med, sum(rr) / len(rr), tp, sl, hold, len(rr))
                    if best is None or med > best[0]:
                        best = row
        if best:
            print(f"лучший по медиане: TP={best[2]} SL={best[3]} hold={best[4]}с → "
                  f"median={best[0]:+.1%} mean={best[1]:+.1%} n={best[5]}")
        else:
            print("недостаточно данных для сетки (нужно ≥20 сделок на конфиг).")
        return

    # дефолт: сравнение режимов потребителя на дефолтном выходе
    for delay in (5.0, 60.0):
        _print_eval("СРАВНЕНИЕ РЕЖИМОВ", delay, DEFAULT_EXIT, args.fee, traj, meta)


# ---------- синтетический самотест ----------
def _selftest() -> None:
    # 2 токена: pumper (2x потом падение) и rug (сразу вниз)
    sig_rows = [
        {"signal": {"token_mint": "PUMP", "ts": 1000.0, "level": "strong", "n_actors": 3}},
        {"signal": {"token_mint": "RUG", "ts": 2000.0, "level": "weak", "n_actors": 2}},
        {"type": "exit", "token_mint": "PUMP"},   # должна игнорироваться
    ]
    hist_rows = (
        [{"ts": 1000.0, "mint": "PUMP", "price_usd": 1.0}]
        + [{"ts": 1000.0 + i * 15, "mint": "PUMP", "price_usd": 1.0 + i * 0.25} for i in range(1, 12)]
        + [{"ts": 1000.0 + 12 * 15, "mint": "PUMP", "price_usd": 1.6}]      # откат от пика ~3.75
        + [{"ts": 2000.0 + i * 15, "mint": "RUG", "price_usd": max(0.05, 1.0 - i * 0.2)} for i in range(0, 8)]
    )
    traj, meta = load_trajectories(hist_rows), load_signal_meta(sig_rows)
    assert "PUMP" in meta and "RUG" in meta and len(meta) == 2, "exit-запись просочилась"
    assert len(traj["PUMP"]) >= 12, "траектория PUMP не собралась"
    print("meta:", meta)
    for delay in (5.0, 60.0):
        _print_eval("SELFTEST", delay, DEFAULT_EXIT, 0.06, traj, meta)
    # PUMP должен взять TP (доходит до 3.75x > 2.5), RUG — SL
    r_pump = simulate(traj["PUMP"], 1000.0, 5.0, DEFAULT_EXIT, 0.06)
    r_rug = simulate(traj["RUG"], 2000.0, 5.0, DEFAULT_EXIT, 0.06)
    print("PUMP:", r_pump, "| RUG:", r_rug)
    assert r_pump and r_pump["reason"] == "tp", "PUMP должен был взять тейк"
    assert r_rug and r_rug["reason"] == "sl", "RUG должен был словить стоп"
    print("\n[selftest OK] логика входа-с-задержкой и выхода работает.")


if __name__ == "__main__":
    main()
