"""Испытательный срок: добавление кандидатов в watchlist с пометкой и без доверия.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ, а не разовая правка файла. Кандидат приходит из обратного
поиска, где он найден НА ТЕХ ЖЕ данных, по которым отбирались победители. Доверять
такому списку нельзя, и единственная защита — форвард. Значит в watchlist должно
быть видно, кто пришёл на испытание и когда: без пометки через неделю никто не
вспомнит, какие кошельки надо судить строже.

ЧТО ФИЛЬТРУЕТСЯ. Берутся только кандидаты, прошедшие ОБА порога: не меньше
`мин_побед` попаданий в победителей И подъём не ниже `мин_подъём`. Подъём считается
против контрольной группы невыросших токенов, и он решает: на замере 12.08 кошелёк
`2CQgjcdN` имел ШЕСТЬ победителей и подъём 0.7x — то есть у неудачников он
встречался чаще, чем у победителей. Без контроля он выглядел бы находкой.

Run:  python -m src.probation [--мин-побед 3] [--мин-подъём 1.5] [--применить]
"""
from __future__ import annotations

import argparse
import json
import time

from . import config

ФАЙЛ = "flow_watchlist.json"
КАНДИДАТЫ = "discover_candidates.json"


def _загрузить(имя: str):
    with open(config.OUTPUT_DIR / имя, encoding="utf-8") as f:
        return json.load(f)


def известные(watchlist: list) -> set[str]:
    """Все адреса, уже присутствующие в списке — и как actor_id, и внутри wallets."""
    из: set[str] = set()
    for a in watchlist:
        if not isinstance(a, dict):
            continue
        if a.get("actor_id"):
            из.add(a["actor_id"])
        for w in (a.get("wallets") or []):
            из.add(w)
    return из


def отобрать(кандидаты: list[dict], мин_побед: int, мин_подъём: float,
             уже: set[str]) -> list[dict]:
    из = []
    for k in кандидаты:
        if k.get("в_списке") or k.get("кошелёк") in уже:
            continue
        if (k.get("победителей") or 0) < мин_побед:
            continue
        п = k.get("подъём")
        if п is None or п < мин_подъём:
            continue
        из.append(k)
    return sorted(из, key=lambda k: -(k.get("подъём") or 0))


def запись(k: dict) -> dict:
    """Актор на испытательном сроке. Вес 1.0 — как у всех: занижать вес значило бы
    предрешить исход замера, а мы именно его и хотим получить."""
    return {"actor_id": k["кошелёк"], "wallets": [k["кошелёк"]], "weight": 1.0,
            "испытательный_срок": True,
            "источник": "обратный поиск по победителям",
            "победителей": k.get("победителей"), "неудачников": k.get("неудачников"),
            "подъём": round(k.get("подъём") or 0, 2),
            "добавлен_ts": time.time()}


def применить(новые: list[dict], watchlist: list) -> list:
    return list(watchlist) + [запись(k) for k in новые]


def сохранить(watchlist: list) -> None:
    """С резервной копией: watchlist — вход стратегии, и откат должен быть возможен."""
    п = config.OUTPUT_DIR / ФАЙЛ
    рез = config.OUTPUT_DIR / "flow_watchlist_before_probation.json"
    try:
        рез.write_text(п.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[испытание] резервная копия не сделана: {type(e).__name__}")
    tmp = config.OUTPUT_DIR / (ФАЙЛ + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=1)
    tmp.replace(п)


def отчёт(новые: list[dict], всего: int, применено: bool) -> str:
    if not новые:
        return ("ИСПЫТАТЕЛЬНЫЙ СРОК: подходящих кандидатов нет. Порог проходят только "
                "те, у кого хватает и попаданий, и подъёма над контрольной группой.")
    строки = [f"ИСПЫТАТЕЛЬНЫЙ СРОК: {'добавлено' if применено else 'ОТБОР (без записи)'} "
              f"{len(новые)} кошельков, в списке станет {всего}", ""]
    for k in новые:
        строки.append(f"  {k['кошелёк'][:8]} · победителей {k['победителей']} · "
                      f"неудачников {k.get('неудачников', 0)} · подъём {k['подъём']:.1f}x")
    строки += ["",
               "Судить по форварду через src/actor_scorecard.py, раздел «испытательный "
               "срок». Кандидаты найдены на тех же данных, где отбирались победители, "
               "поэтому их история не является доводом.",
               "Монитор читает watchlist при старте — правка применится после перезапуска."]
    return "\n".join(строки)


def main() -> None:
    ap = argparse.ArgumentParser(description="Кандидаты на испытательный срок")
    ap.add_argument("--мин-побед", type=int, default=3)
    ap.add_argument("--мин-подъём", type=float, default=1.5)
    ap.add_argument("--применить", action="store_true",
                    help="без него только показывает отбор, файл не трогает")
    a = ap.parse_args()
    wl = _загрузить(ФАЙЛ)
    d = _загрузить(КАНДИДАТЫ)
    новые = отобрать(d.get("кандидаты") or [], getattr(a, "мин_побед"),
                     getattr(a, "мин_подъём"), известные(wl))
    if a.применить and новые:
        wl = применить(новые, wl)
        сохранить(wl)
    print(отчёт(новые, len(wl) + (0 if a.применить else len(новые)), a.применить))


if __name__ == "__main__":
    main()
