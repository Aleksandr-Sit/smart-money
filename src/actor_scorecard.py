"""Честная оценка акторов: кто стоит своего места в watchlist.

ЗАЧЕМ. Прежний рейтинг (07.08, зашит в комментарии конфига) строился на МОДЕЛЬНОЙ
цене входа — цене актора, которая недостижима. Он объявил пару XyPCF8Da/7p4AkPb9
лучшей когортой с win 0.90 и медианой +69.5%. На честной цене та же пара оказалась
худшей в списке: −$1.15 на сделку, win 0.22. Ошибка стоила того, что конфиг называл
приоритетными акторов, которые теряли деньги.

ЧТО СЧИТАЕТСЯ ЧЕСТНЫМ ВХОДОМ. Первая выборка ТРЕКЕРА после сигнала (`src != signal`),
по траектории со склейкой шкалы. Якорь `signal` — это и есть цена актора, входить
по нему значит повторить исходный дефект. Абсолютный уровень при таком входе всё
равно завышен: тик приходит до 15 секунд спустя. Поэтому рейтинг годится для
СРАВНЕНИЯ акторов между собой, а не как обещание доходности.

КРИТЕРИЙ УСТОЙЧИВОСТИ, а не суммы: сумма положительна И сумма без топ-3 положительна
И минимум 3 отрезка из 4 в плюсе. Доходность здесь хвостовая, и актор, у которого
весь плюс сделали три сделки, — это удача, а не источник.

Run:  python -m src.actor_scorecard [--издержки 0.12] [--telegram]
"""
from __future__ import annotations

import argparse
import statistics as st
from collections import defaultdict
from datetime import datetime

from . import analysis

CLIP = 10.0
ИЗДЕРЖКИ = 0.12          # замер 10.08 по дельтам транзакций; см. docs/live_readiness.md
МИН_СДЕЛОК = 40          # ниже — испытательный срок, а не оценка
ОКНО_НЕДЕЛЯ = 7 * 86400


def _эпоха(t) -> float:
    return datetime.fromisoformat(t).timestamp() if isinstance(t, str) else float(t)


def _сигналы() -> dict[str, list[dict]]:
    сиг: dict[str, list[dict]] = defaultdict(list)
    for r in analysis.read_jsonl("signals.log"):
        s = r.get("signal") or {}
        t = s.get("token_mint")
        if not t or s.get("ts") is None:
            continue
        сиг[t].append({"ts": float(s["ts"]),
                       "акторы": list(dict.fromkeys(s.get("actors") or []))})
    for v in сиг.values():
        v.sort(key=lambda x: x["ts"])
    return сиг


def собрать(издержки: float = ИЗДЕРЖКИ, мин_сделок: int = МИН_СДЕЛОК) -> dict:
    сиг = _сигналы()
    сделки = []
    for r in analysis.load_closed():
        t, ets = r.get("token_mint"), r.get("entry_ts")
        if not t or ets is None:
            continue
        к = [s for s in сиг.get(t, []) if s["ts"] <= float(ets) + 10]
        s = к[-1] if к else (сиг.get(t) or [None])[0]
        if not s:
            continue
        сделки.append({"tok": t, "сиг_ts": s["ts"], "вых_ts": r["exit_ts"],
                       "ts": float(ets), "акторы": s["акторы"]})
    if not сделки:
        return {"мало_данных": True, "сделок": 0}

    трек = analysis.траектории({x["tok"] for x in сделки})
    годные = []
    for x in сделки:
        v = трек.get(x["tok"])
        if not v:
            continue
        вход = next((p for ts, p, src in v if ts >= x["сиг_ts"] and src != "signal"), None)
        выход = None
        for ts, p, _ in v:
            if ts <= x["вых_ts"]:
                выход = p
            else:
                break
        if вход and выход and вход > 0:
            x["чист"] = (выход / вход) * (1 - издержки) - 1
            годные.append(x)
    if len(годные) < 100:
        return {"мало_данных": True, "сделок": len(годные)}

    годные.sort(key=lambda x: x["ts"])
    границы = [годные[int(len(годные) * i / 4)]["ts"] for i in range(1, 4)]
    конец = годные[-1]["ts"]

    def свод(v: list[dict]) -> dict:
        f = [x["чист"] for x in v]
        ss = sorted(f)
        сег: dict[int, list] = defaultdict(list)
        for x in v:
            сег[sum(1 for g in границы if x["ts"] >= g)].append(x["чист"])
        return {"n": len(f), "сум": sum(f) * CLIP, "без3": sum(ss[:-3]) * CLIP,
                "мед": st.median(f), "win": sum(1 for y in f if y > 0) / len(f),
                "сег": sum(1 for k in range(4) if сег[k] and sum(сег[k]) > 0),
                "на_сделку": sum(f) * CLIP / len(f)}

    по_актору: dict[str, list[dict]] = defaultdict(list)
    for x in годные:
        for a in set(x["акторы"]):
            по_актору[a].append(x)

    рейтинг, испытание = [], []
    for a, v in по_актору.items():
        m = свод(v)
        посл = sum(1 for x in v if x["ts"] >= конец - ОКНО_НЕДЕЛЯ)
        пред = sum(1 for x in v if конец - 2 * ОКНО_НЕДЕЛЯ <= x["ts"] < конец - ОКНО_НЕДЕЛЯ)
        m.update({"актор": a, "посл7": посл, "пред7": пред,
                  "тренд": (посл / пред) if пред else (None if not посл else float("inf"))})
        (рейтинг if len(v) >= мин_сделок else испытание).append(m)
    рейтинг.sort(key=lambda m: -m["на_сделку"])
    испытание.sort(key=lambda m: -m["на_сделку"])
    устойчивые = [m for m in рейтинг if m["сум"] > 0 and m["без3"] > 0 and m["сег"] >= 3]
    убыточные = [m for m in рейтинг if m["на_сделку"] < 0]
    return {"мало_данных": False, "сделок": len(годные), "поток": свод(годные),
            "рейтинг": рейтинг, "испытание": испытание,
            "устойчивые": устойчивые, "убыточные": убыточные,
            "суток": (конец - годные[0]["ts"]) / 86400, "издержки": издержки}


def отчёт(d: dict, показать: int = 8) -> str:
    if d.get("мало_данных"):
        return f"ОЦЕНКА АКТОРОВ: данных мало — {d['сделок']} сделок с честным входом."
    п = d["поток"]
    строки = [
        f"ОЦЕНКА АКТОРОВ · {d['сделок']} сделок за {d['суток']:.1f} сут · "
        f"издержки {d['издержки']:.0%}",
        f"весь поток: ${п['сум']:+.0f} · без топ-3 ${п['без3']:+.0f} · "
        f"${п['на_сделку']:+.2f}/сделку · {п['сег']}/4 отрезка",
        "",
        f"УСТОЙЧИВЫХ: {len(d['устойчивые'])} из {len(d['рейтинг'])} "
        f"(сумма и без топ-3 > 0, отрезков >= 3)",
    ]
    for m in d["устойчивые"][:показать]:
        т = ("" if m["тренд"] is None else
             " · РАСТЁТ x%.1f" % m["тренд"] if m["тренд"] >= 1.5 else
             " · ⚠ иссякает" if m["тренд"] < 0.5 else "")
        строки.append(f"  {m['актор'][:8]} n={m['n']:4d} ${m['на_сделку']:+.2f}/сделку "
                      f"без топ-3 ${m['без3']:+.0f} {m['сег']}/4 · за 7д {m['посл7']}{т}")
    if d["убыточные"]:
        строки.append("")
        строки.append("УБЫТОЧНЫЕ (на честной цене входа):")
        for m in d["убыточные"][:показать]:
            строки.append(f"  {m['актор'][:8]} n={m['n']:4d} ${m['на_сделку']:+.2f}/сделку "
                          f"win {m['win']:.2f} {m['сег']}/4")
        строки.append("  ВАЖНО: убирать их из watchlist — отдельное решение. Замер 12.08 "
                      "показал, что поверх фильтра независимости удаление даёт РОВНО НОЛЬ.")
    if d["испытание"]:
        строки.append("")
        строки.append(f"ИСПЫТАТЕЛЬНЫЙ СРОК (меньше {МИН_СДЕЛОК} сделок, судить рано):")
        for m in d["испытание"][:показать]:
            строки.append(f"  {m['актор'][:8]} n={m['n']:3d} ${m['на_сделку']:+.2f}/сделку "
                          f"{m['сег']}/4 · за 7д {m['посл7']}")
    строки.append("")
    строки.append("Рейтинг сравнивает акторов между собой. Абсолютный уровень завышен: "
                  "вход берётся по первому тику трекера, а он приходит до 15с спустя.")
    return "\n".join(строки)


def main() -> None:
    ap = argparse.ArgumentParser(description="Честная оценка акторов watchlist")
    ap.add_argument("--издержки", type=float, default=ИЗДЕРЖКИ)
    ap.add_argument("--мин-сделок", type=int, default=МИН_СДЕЛОК)
    ap.add_argument("--telegram", action="store_true")
    a = ap.parse_args()
    d = собрать(getattr(a, "издержки"), getattr(a, "мин_сделок"))
    txt = отчёт(d)
    print(txt)
    if a.telegram:
        from . import delivery
        delivery.send_telegram("🧭 " + txt)


if __name__ == "__main__":
    main()
