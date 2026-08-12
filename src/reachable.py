"""Достижимая цена: сколько стоит токен через N секунд после сигнала.

ЗАЧЕМ. Между ценой, по которой купил актор, и ценой, по которой купили бы мы,
лежит разрыв, стоивший живому прогону 10.08 −6.38% на входе. Замерить его на
траекториях нельзя: трекер снимает цену раз в 15 секунд, а наш реальный лаг
1–2 секунды — окно 0–15с в данных отсутствует вовсе.

Насколько это важно, показала проверка лаунч-снайпа 11.08: у связки XyPCF8Da/
7p4AkPb9 вход по цене актора давал медиану +39.7% (win 0.79), а вход на СЛЕДУЮЩЕМ
тике (~15с) — уже −13.5% (win 0.32). Следующий тик стоил 1.40x цены актора.
Безубыток лежит около 1.2x, то есть ответ целиком решается внутри слепого окна.

Модуль закрывает это окно: после каждого сигнала снимает цену кривой на +2с и +5с
и пишет рядом с ценой сигнала. Через несколько суток «сколько мы реально платим»
станет измеренной величиной, а не оценкой сверху.

БЕЗОПАСНОСТЬ ПУТИ ТОРГОВЛИ: замер запускается отдельной задачей и никогда не
поднимает исключение наверх. Он не должен ни задержать вход, ни уронить монитор.

Run:  python -m src.reachable            # отчёт по накопленному
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from collections import Counter

from . import config, helius, market, price_track

# Нулевая отсечка обязательна. `цена_сигнала` приходит из DexScreener ИЛИ из он-чейн
# сделки, а замеры — с бондинг-кривой; сравнивая их напрямую, мы мерили бы расхождение
# источников пополам с движением цены. Первые 19 замеров дали медиану 0.838x — «цена
# упала за две секунды», чего на растущем токене быть не должно. Замер на +0с даёт
# базу С ТОЙ ЖЕ кривой, и множитель становится честным.
ЗАДЕРЖКИ = (0.0, 2.0, 5.0)      # секунды после сигнала; 15с и дальше уже даёт трекер
ФАЙЛ = "reachable.jsonl"


СБОИ: Counter = Counter()      # почему замер не состоялся — видно в пульсе


def цена_кривой(mint: str, попыток: int = 2) -> float | None:
    """Цена токена по бондинг-кривой прямо сейчас. None = кривой нет или ошибка узла.

    ПРИЧИНА ОТКАЗА ОБЯЗАТЕЛЬНО СЧИТАЕТСЯ. Первый прогон 11.08 дал сплошные `null`,
    и по журналу было не понять, отказал ли узел, закрылась ли кривая или не пришёл
    курс SOL. Молчаливый None — тот же класс дефекта, что стоил первой живой покупки
    (`helius.rpc` возвращал ошибку узла неотличимо от «аккаунта нет»).

    Публичный узел отвечает 429 под нагрузкой, поэтому одна повторная попытка: замер
    идёт вне торгового пути, задержка в полсекунды здесь ничего не стоит.
    """
    for попытка in range(попыток):
        try:
            pda = price_track.bonding_curve_pda(mint)
            res = helius.rpc("getAccountInfo", [pda, {"encoding": "base64"}])
            acc = (res.get("result") or {}).get("value")
            if not acc or not acc.get("data"):
                СБОИ["нет аккаунта кривой"] += 1
                return None
            curve = price_track.parse_curve(base64.b64decode(acc["data"][0]))
            if not curve:
                СБОИ["кривая не разобрана"] += 1
                return None
            if curve.get("complete"):
                СБОИ["кривая закрыта (грэдуэйшн)"] += 1
                return None
            sol = market.sol_price()
            if not sol:
                СБОИ["нет курса SOL"] += 1
                return None
            return curve["price_sol"] * sol
        except Exception as e:  # noqa: BLE001 — замер наблюдательный, ничего не ломает
            if попытка + 1 < попыток:
                time.sleep(0.5)
                continue
            СБОИ[type(e).__name__] += 1
            return None
    return None


def сбои_текстом() -> str:
    return ", ".join(f"{k}={v}" for k, v in СБОИ.most_common(4)) or "нет"


def записать(rec: dict) -> None:
    try:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.OUTPUT_DIR / ФАЙЛ, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


async def замерить(mint: str, цена_сигнала: float | None, задержки=ЗАДЕРЖКИ,
                   независимых: int | None = None, объём: float | None = None,
                   n_actors: int | None = None) -> dict:
    """Снимает цену на каждой задержке и пишет строку. Возвращает её же (для тестов)."""
    loop = asyncio.get_event_loop()
    замеры: dict[str, float | None] = {}
    прошло = 0.0
    for d in sorted(задержки):
        пауза = d - прошло
        if пауза > 0:
            await asyncio.sleep(пауза)
        прошло = d
        замеры[str(d)] = await loop.run_in_executor(None, цена_кривой, mint)
    rec = {"ts": time.time(), "mint": mint, "цена_сигнала": цена_сигнала,
           "замеры": замеры, "независимых": независимых, "объём": объём,
           "n_actors": n_actors}
    записать(rec)
    return rec


def запустить(mint: str, цена_сигнала: float | None, **kw) -> None:
    """Пустить замер фоном. Ошибка планирования не должна касаться торговли."""
    try:
        asyncio.get_event_loop().create_task(замерить(mint, цена_сигнала, **kw))
    except Exception:  # noqa: BLE001
        pass


def собрать(путь=None) -> dict:
    """→ по каждой задержке: во сколько раз цена отличается от цены сигнала."""
    p = путь or (config.OUTPUT_DIR / ФАЙЛ)
    строки = []
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                try:
                    строки.append(json.loads(ln))
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        return {"n": 0, "по_задержкам": {}}
    по: dict[str, list[float]] = {}          # база — замер кривой на +0с
    источник: list[float] = []               # расхождение цены сигнала с кривой
    for r in строки:
        замеры = r.get("замеры") or {}
        база = замеры.get("0.0")
        ц0 = r.get("цена_сигнала")
        if ц0 and ц0 > 0 and база and база > 0:
            источник.append(база / ц0)
        if not база or база <= 0:
            continue
        for d, p1 in замеры.items():
            if d != "0.0" and p1 and p1 > 0:
                по.setdefault(d, []).append(p1 / база)

    def свод(v):
        v.sort()
        n = len(v)
        return {"n": n, "медиана": v[n // 2], "p25": v[n // 4],
                "p75": v[int(n * 0.75)], "дороже_2проц": sum(1 for x in v if x > 1.02) / n}

    итог = {d: свод(v) for d, v in по.items() if v}
    return {"n": len(строки), "по_задержкам": итог,
            "источник": свод(источник) if источник else None}


def отчёт(d: dict) -> str:
    if not d["по_задержкам"]:
        return ("ДОСТИЖИМАЯ ЦЕНА: замеров пока нет. Данные копятся с момента деплоя, "
                "нужно несколько сотен сигналов.")
    строки = [f"ДОСТИЖИМАЯ ЦЕНА · сигналов замерено {d['n']}", ""]
    for задержка in sorted(d["по_задержкам"], key=float):
        m = d["по_задержкам"][задержка]
        строки.append(f"  +{задержка}с  n={m['n']:4d}  медиана {m['медиана']:.3f}x  "
                      f"p25 {m['p25']:.3f}x  p75 {m['p75']:.3f}x  "
                      f"дороже сигнала {m['дороже_2проц']:.0%}")
    строки.append("")
    строки.append("База — цена КРИВОЙ в момент сигнала, не цена сигнала: они из разных "
                  "источников, и их прямое сравнение мерило бы расхождение источников "
                  "пополам с движением цены.")
    и = d.get("источник")
    if и:
        строки.append(f"  расхождение источников: кривая/цена сигнала медиана "
                      f"{и['медиана']:.3f}x (n={и['n']})")
    строки.append("Множитель выше 1.0 = мы платим дороже актора. Это и есть разрыв входа, "
                  "который на живом прогоне 10.08 составил −6.38%.")
    return "\n".join(строки)


def main() -> None:
    ap = argparse.ArgumentParser(description="Достижимая цена входа")
    ap.add_argument("--telegram", action="store_true")
    a = ap.parse_args()
    txt = отчёт(собрать())
    print(txt)
    if a.telegram:
        from . import delivery
        delivery.send_telegram("📏 " + txt)


if __name__ == "__main__":
    main()
