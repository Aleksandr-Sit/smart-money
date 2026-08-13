"""Обратный поиск: кто покупал НАШИХ победителей раньше всех.

ЗАЧЕМ. Watchlist из 59 кошельков собран Контуром 1 на Dune и с тех пор почти не
менялся. Его слепота измерена: **76% токенов, купленных нашими же акторами, никогда
не складываются в конфлюенс** (2664 из 11092), то есть три четверти находок мы просто
не видим. Нужен способ добавлять кошельки, которых в списке нет.

ИСТОЧНИК — СВОИ ЖЕ ДАННЫЕ, НЕ ЧУЖОЙ СПИСОК. Мы наблюдали ~10 000 токенов и знаем,
какие из них выросли: 277 сделали 10x и 84 сделали 20x от первой выборки (рост
считается по СКЛЕЕННОЙ траектории, иначе 14.6% скачка на стыке кривая→DEX сойдут
за прибыль). По каждому победителю поднимаем самые ранние покупки прямо из цепи
и смотрим, кто там был.

ПОЧЕМУ БЕЗ DUNE. Публичный узел отдаёт 1000 подписей за 0.2–0.7 секунды, поэтому
весь разбор стоит ноль кредитов. Dune тут тратить незачем: тот же результат
получается бесплатно, а кредиты нужны для вещей, которые из цепи не достать.

ГЛАВНАЯ ОГОВОРКА, БЕЗ КОТОРОЙ ЭТОТ МОДУЛЬ ВРЕДЕН. Список кандидатов получен НА ТЕХ ЖЕ
данных, по которым отбирались победители. Это ошибка выжившего в чистом виде: кошелёк,
случайно оказавшийся в трёх удачных токенах, выглядит гением. Контур 1 ровно так и
провалился на полугодовом out-of-sample. Поэтому здесь считается ещё и **знаменатель** —
сколько ВСЕГО токенов кошелёк покупал в выборке, — а решение принимается только после
испытательного срока вперёд (`src/actor_scorecard.py`, раздел «испытательный срок»).

Run:  python -m src.discover_buyers [--рост 10] [--токенов 40] [--ранних 25]
"""
from __future__ import annotations

import argparse
import time
from collections import Counter, defaultdict

from . import analysis, config, helius, log_parse, price_track

ПАУЗА_С = 0.25          # публичный узел отвечает 429 на плотной серии
ПОВТОРОВ = 3


def _rpc(метод: str, params: list):
    """Вызов с паузой и повтором: узел общий, 429 здесь норма, а не поломка."""
    for попытка in range(ПОВТОРОВ):
        try:
            time.sleep(ПАУЗА_С)
            return helius.rpc(метод, params)
        except Exception:  # noqa: BLE001
            if попытка + 1 == ПОВТОРОВ:
                return None
            time.sleep(1.5 * (попытка + 1))
    return None


def победители(мин_рост: float = 10.0) -> list[tuple[float, str]]:
    """→ [(рост, mint), ...] по СКЛЕЕННОЙ траектории, от максимального роста."""
    трек = analysis.траектории()          # склейка включена по умолчанию
    из = []
    for m, v in трек.items():
        if len(v) < 3:
            continue
        первая = v[0][1]
        макс = max(p for _, p, _ in v)
        if первая > 0 and макс / первая >= мин_рост:
            из.append((макс / первая, m))
    из.sort(reverse=True)
    return из


def ранние_покупатели(mint: str, сколько: int = 25) -> list[str]:
    """Кошельки первых покупок токена, из цепи. Пусто — если не удалось дойти до начала."""
    pda = price_track.bonding_curve_pda(mint)
    старейшие: list[dict] = []
    before = None
    for _ in range(8):                    # 8 страниц по 1000 = 8000 транзакций максимум
        r = _rpc("getSignaturesForAddress",
                 [pda, {"limit": 1000, **({"before": before} if before else {})}])
        пачка = (r or {}).get("result") or []
        if not пачка:
            break
        старейшие = пачка                 # подписи идут от новых к старым
        before = пачка[-1]["signature"]
        if len(пачка) < 1000:
            break                         # дошли до самой первой транзакции
    if not старейшие:
        return []
    кандидаты = [s for s in reversed(старейшие) if not s.get("err")][:сколько]
    кошельки: list[str] = []
    for s in кандидаты:
        r = _rpc("getTransaction",
                 [s["signature"], {"maxSupportedTransactionVersion": 0,
                                   "encoding": "jsonParsed"}])
        логи = (((r or {}).get("result") or {}).get("meta") or {}).get("logMessages") or []
        for e in log_parse._events(логи):
            if e.get("is_buy") and e.get("mint") == mint and e.get("user"):
                кошельки.append(e["user"])
    # порядок сохраняем: первым идёт тот, кто купил раньше
    return list(dict.fromkeys(кошельки))


def _свои_кошельки() -> set[str]:
    """Кошельки текущего watchlist.

    ЧИТАЕТСЯ КАК ЕДИНЫЙ JSON, а не построчно. Первый прогон 12.08 использовал
    read_jsonl — тот разбирает JSONL, а flow_watchlist.json это один документ.
    Множество «своих» получалось пустым, и отчёт объявил новыми ВСЕХ кандидатов,
    включая CkLT4ADy, который в списке с самого начала. Ровно тот класс тихой
    ошибки, ради которого здесь считается знаменатель.
    """
    import json as _json
    свои: set[str] = set()
    try:
        with open(config.OUTPUT_DIR / "flow_watchlist.json", encoding="utf-8") as f:
            d = _json.load(f)
    except Exception:  # noqa: BLE001
        return свои
    записи = d if isinstance(d, list) else (d.get("actors") or d.get("wallets") or
                                            list(d.values()) if isinstance(d, dict) else [])
    for w in записи:
        if isinstance(w, str):
            свои.add(w)
        elif isinstance(w, dict):
            for поле in ("wallet", "address", "actor_id", "actor"):
                if w.get(поле):
                    свои.add(w[поле])
    return свои


КЭШ_ФАЙЛ = "discover_cache.json"


def _кэш() -> dict[str, list[str]]:
    """Уже разобранные токены: mint → ранние покупатели.

    Публичный узел режет запросы, и разбор одного победителя стоит ~25 вызовов.
    Прогон по 84 токенам вставал на часы, а при обрыве терялось ВСЁ. Кэш делает
    разбор возобновляемым: гоняем порциями, каждая продолжает предыдущую.
    """
    import json as _json
    try:
        with open(config.OUTPUT_DIR / КЭШ_ФАЙЛ, encoding="utf-8") as f:
            d = _json.load(f)
        return {k: v for k, v in d.items() if isinstance(v, list)}
    except Exception:  # noqa: BLE001
        return {}


def _записать_кэш(кэш: dict) -> None:
    import json as _json
    try:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = config.OUTPUT_DIR / (КЭШ_ФАЙЛ + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(кэш, f, ensure_ascii=False)
        tmp.replace(config.OUTPUT_DIR / КЭШ_ФАЙЛ)
    except Exception as e:  # noqa: BLE001
        print(f"[поиск] кэш не сохранён: {type(e).__name__}")


def собрать(мин_рост: float = 10.0, токенов: int = 40, ранних: int = 25,
            за_прогон: int | None = None) -> dict:
    """`за_прогон` — сколько НОВЫХ токенов разбирать за один запуск (остальные из кэша)."""
    поб = победители(мин_рост)[:токенов]
    if not поб:
        return {"мало_данных": True, "победителей": 0}
    свои = _свои_кошельки()

    кэш = _кэш()
    новых = 0
    for рост, m in поб:
        if m in кэш:
            continue
        if за_прогон is not None and новых >= за_прогон:
            break
        кэш[m] = ранние_покупатели(m, ранних)
        новых += 1
        _записать_кэш(кэш)          # после КАЖДОГО токена: обрыв не теряет работу

    счёт: Counter = Counter()
    места: dict[str, list[int]] = defaultdict(list)
    разобрано = 0
    for рост, m in поб:
        кош = кэш.get(m)
        if not кош:
            continue
        разобрано += 1
        for i, w in enumerate(кош):
            счёт[w] += 1
            места[w].append(i + 1)
    кандидаты = []
    for w, c in счёт.items():
        if c < 2:
            continue
        кандидаты.append({"кошелёк": w, "победителей": c, "доля": c / max(разобрано, 1),
                          "медиана_места": sorted(места[w])[len(места[w]) // 2],
                          "в_списке": w in свои})
    кандидаты.sort(key=lambda x: (-x["победителей"], x["медиана_места"]))
    осталось = sum(1 for _, m in поб if m not in кэш)
    d = {"мало_данных": False, "победителей": len(поб), "разобрано": разобрано,
         "осталось_разобрать": осталось,
         "мин_рост": мин_рост, "кандидаты": кандидаты,
         "новых": [k for k in кандидаты if not k["в_списке"]]}
    _сохранить(d)
    return d


def _сохранить(d: dict) -> None:
    """Результат кладём на диск ЦЕЛИКОМ, с полными адресами.

    Отчёт печатает адреса в сокращении, и первый прогон 12.08 оставил после себя
    только восьмисимвольные префиксы — чтобы добавить кандидата в watchlist,
    пришлось бы гонять разбор заново. Разбор стоит десятки минут RPC, терять его
    результат из-за формата вывода бессмысленно.
    """
    import json as _json
    try:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = config.OUTPUT_DIR / "discover_candidates.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump({"посчитано": time.time(), **d}, f, ensure_ascii=False, indent=1)
        tmp.replace(config.OUTPUT_DIR / "discover_candidates.json")
    except Exception as e:  # noqa: BLE001
        print(f"[поиск] не удалось сохранить результат: {type(e).__name__}")


def отчёт(d: dict, показать: int = 15) -> str:
    if d.get("мало_данных"):
        return "ОБРАТНЫЙ ПОИСК: победителей в данных не нашлось."
    строки = [
        f"ОБРАТНЫЙ ПОИСК ПО ПОБЕДИТЕЛЯМ · рост >= {d['мин_рост']:.0f}x · "
        f"разобрано {d['разобрано']} из {d['победителей']} токенов",
        f"кошельков, встретившихся минимум в двух победителях: {len(d['кандидаты'])}, "
        f"из них НЕ в watchlist: {len(d['новых'])}"
        + (f" · осталось разобрать: {d['осталось_разобрать']}"
           if d.get("осталось_разобрать") else " · выборка разобрана полностью"),
        "",
    ]
    if d["новых"]:
        строки.append(f'{"кошелёк":>10s} {"победителей":>12s} {"доля":>6s} {"медиана места":>14s}')
        for k in d["новых"][:показать]:
            строки.append(f'{k["кошелёк"][:8]:>10s} {k["победителей"]:12d} '
                          f'{k["доля"]:5.0%} {k["медиана_места"]:14d}')
        строки.append("")
        строки.append("полные адреса — в output/discover_candidates.json")
    else:
        строки.append("новых кошельков не найдено — все ранние покупатели уже в списке")
    строки += [
        "",
        "ЭТО НЕ РЕЙТИНГ, А СПИСОК ДЛЯ ПРОВЕРКИ. Кандидаты найдены на тех же данных, по "
        "которым отбирались победители — ошибка выжившего здесь максимальна. Кошелёк "
        "попадает в watchlist только на испытательный срок, а решение принимается по "
        "форварду в src/actor_scorecard.py (сумма без топ-3 и отрезки, а не история).",
    ]
    return "\n".join(строки)


def main() -> None:
    ap = argparse.ArgumentParser(description="Ранние покупатели наших победителей")
    ap.add_argument("--рост", type=float, default=10.0)
    ap.add_argument("--токенов", type=int, default=40)
    ap.add_argument("--ранних", type=int, default=25)
    ap.add_argument("--за-прогон", type=int, default=None,
                    help="сколько НОВЫХ токенов разобрать за запуск (кэш продолжается)")
    ap.add_argument("--telegram", action="store_true")
    a = ap.parse_args()
    d = собрать(getattr(a, "рост"), getattr(a, "токенов"), getattr(a, "ранних"),
                getattr(a, "за_прогон"))
    txt = отчёт(d)
    print(txt)
    if a.telegram:
        from . import delivery
        delivery.send_telegram("🔎 " + txt)


if __name__ == "__main__":
    main()
