"""Объединение свежих кандидатов discovery с проверенными на живых сделках акторами.

ЗАЧЕМ (аудит 09.08). Автообновление запускало discovery и ЗАМЕНЯЛО им watchlist.
Проверка первой же свежей выгрузки на 8031 нашей сделке показала, чем это кончается:

    вариант                             кошельков   потери
    A. всё подряд (старый + новый)          178     0 сделок,  0$
    B. без «ни разу не сигналивших»         129     0 сделок,  0$   <- этот
    C. активные за 7 суток                  121     3 сделки, -5$
    D. замена на discovery (было так)        36  3831 сделка, +4153$

Discovery отбирает по своим формальным признакам и не знает нашей статистики.
Приоритетный актор 7p4AkPb9… выбыл из выгрузки по критерию «3 ранних входа при
пороге 4», хотя в наших данных активен и продолжает сигналить. Поэтому discovery
ПРЕДЛАГАЕТ кандидатов, а исключение решается по нашим наблюдениям.

Правило B: выбрасываем только тех, чьё молчание ПОДТВЕРЖДЕНО наблюдением — ни
одного сигнала за всё время работы. Это факт о прошлом, а не прогноз о будущем.

Run:  python -m src.merge_watchlist [--candidates путь] [--min-days 14] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import analysis, config

WL = config.OUTPUT_DIR / "flow_watchlist.json"


def _scan_signals(wallet_to_actor: dict[str, str]) -> tuple[set[str], float]:
    """Один проход по signals.log → (акторы, подавшие хоть один сигнал; суток наблюдения).

    ОДИН проход, а не три: файл 11 МБ, и разбирать его несколько раз незачем.

    ВАЖНО (найдено аудитом 10.08): signals.log хранит ДВА вида записей — сигналы и
    выходы (delivery.deliver_exit пишет туда же). Прежняя версия брала ПЕРВУЮ и
    ПОСЛЕДНЮЮ строку файла и искала в них ключ "signal". Последняя строка почти
    всегда запись выхода, ключа там нет → срок наблюдения выходил 0 суток → порог
    min_days не достигался → ветка исключения молчунов не выполнялась НИ РАЗУ.
    Направление отказа безопасное (никого не выбрасываем), но функция была мертва.
    """
    seen: set[str] = set()
    lo = hi = None
    for r in analysis.read_jsonl("signals.log"):
        s = r.get("signal")
        if not s:
            continue                       # запись выхода, а не сигнал
        t = s.get("ts")
        if t:
            lo = t if lo is None else min(lo, t)
            hi = t if hi is None else max(hi, t)
        for a in s.get("actors", []):
            seen.add(a)
            seen.add(wallet_to_actor.get(a, a))
    days = (hi - lo) / 86400 if (lo is not None and hi is not None) else 0.0
    return seen, days


def _seen_actors(wallet_to_actor: dict[str, str]) -> set[str]:
    """Акторы, подавшие хотя бы один сигнал за всё время наблюдения."""
    return _scan_signals(wallet_to_actor)[0]


def _observation_days() -> float:
    """Сколько суток мы вообще наблюдаем. Мало данных → не выбрасываем никого."""
    return _scan_signals({})[1]


def merge(candidates: list[dict], current: list[dict], min_days: float = 14.0,
          grace_days: float = 14.0) -> tuple[list[dict], dict]:
    """→ (объединённый список, отчёт). Никогда не выбрасывает актора без данных о нём.

    ИСПЫТАТЕЛЬНЫЙ СРОК. Молчание — повод для исключения только если актора реально
    слушали. Актор, добавленный вчера, не сигналил не потому, что плох, а потому что
    у него не было возможности. Без этой проверки первый же холостой прогон пометил
    на выброс все 14 акторов, добавленных в тот же день (найдено 09.08 при отладке).
    Дата добавления пишется в поле added_ts; у списков без неё считаем актора старым.
    """
    now = datetime.now(timezone.utc).timestamp()
    w2a = {w: a["actor_id"] for a in current for w in a["wallets"]}
    days = _observation_days()
    seen = _seen_actors(w2a) if days >= min_days else None

    def young(a: dict) -> bool:
        ts = a.get("added_ts")
        return bool(ts) and (now - ts) < grace_days * 86400

    if seen is None:
        keep, dropped = list(current), []
    else:
        keep = [a for a in current if a["actor_id"] in seen or young(a)]
        dropped = [a for a in current if a["actor_id"] not in seen and not young(a)]

    merged = {a["actor_id"]: {**a, "source": a.get("source", "live_proven")} for a in keep}
    added = 0
    for a in candidates:
        aid = a["actor_id"]
        if aid in merged:
            w = sorted(set(merged[aid]["wallets"]) | set(a["wallets"]))
            merged[aid].update(wallets=w, n_wallets=len(w), source="both")
        else:
            merged[aid] = {**a, "source": "discovery", "added_ts": now}
            added += 1

    out = sorted(merged.values(), key=lambda x: -x.get("weight", 0))
    report = {
        "наблюдение_суток": round(days, 1),
        "было_акторов": len(current),
        "кандидатов": len(candidates),
        "выброшено_молчунов": len(dropped),
        "добавлено_новых": added,
        "итого_акторов": len(out),
        "итого_кошельков": len({w for a in out for w in a["wallets"]}),
        "порог_не_достигнут": seen is None,
        "на_испытательном": sum(1 for a in keep if young(a)),
    }
    return out, report


def main() -> None:
    ap = argparse.ArgumentParser(description="Объединить кандидатов discovery с проверенными акторами")
    ap.add_argument("--candidates", default=str(WL),
                    help="файл со свежими кандидатами (по умолчанию — то, что записал discovery)")
    ap.add_argument("--current", default="", help="прежний список (по умолчанию — последний backup)")
    ap.add_argument("--min-days", type=float, default=14.0,
                    help="меньше этого срока наблюдения — никого не выбрасываем")
    ap.add_argument("--grace-days", type=float, default=14.0,
                    help="испытательный срок для недавно добавленных акторов")
    ap.add_argument("--dry-run", action="store_true", help="только показать, не записывать")
    a = ap.parse_args()

    cand = json.loads(Path(a.candidates).read_text(encoding="utf-8"))
    if a.current:
        cur = json.loads(Path(a.current).read_text(encoding="utf-8"))
    else:
        baks = sorted(config.OUTPUT_DIR.glob("flow_watchlist_prev.json"))
        if not baks:
            print("MERGE_SKIP: нет прежнего списка для сравнения — оставляю кандидатов как есть")
            return
        cur = json.loads(baks[-1].read_text(encoding="utf-8"))

    out, rep = merge(cand, cur, a.min_days, a.grace_days)
    for k, v in rep.items():
        print(f"  {k}: {v}")
    if rep["порог_не_достигнут"]:
        print(f"  ВНИМАНИЕ: наблюдения меньше {a.min_days} суток — никого не выбрасывал")
    if a.dry_run:
        print("MERGE_DRY_RUN: файл не записан")
        return
    if WL.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        shutil.copy(WL, config.OUTPUT_DIR / f"flow_watchlist_backup_{stamp}.json")
    WL.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"MERGE_OK: {rep['итого_акторов']} акторов, {rep['итого_кошельков']} кошельков → {WL}")


if __name__ == "__main__":
    main()
