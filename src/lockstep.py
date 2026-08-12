"""Связки кошельков: сколько НЕЗАВИСИМЫХ участников стоит за сигналом.

ЗАЧЕМ. Замер 11.08 на 9247 реализованных сделках: два кошелька `XyPCF8Da…` и
`7p4AkPb9…` оказались одним участником — пересечение множеств токенов 995 из 996.
В 993 из 1007 его сигналов КРОМЕ него нет никого, то есть порог `CONFLUENCE_N=2`
он закрывает в одиночку. Подтверждения независимыми покупателями там нет вовсе.

Масштаб: три таких связки (2, 2 и 3 кошелька) дают 12.3% всех сигналов. Если
требовать двух НЕЗАВИСИМЫХ участников, поток даёт −$732 за 29.8 суток (0 из 4
сегментов в плюсе) против отчётных +$2956. То есть дедупликация не зарабатывает —
она перестаёт врать о результате.

ПОЧЕМУ НЕ `funder.py`/`cluster.py`. Оба опираются на DuckDB-кэш `early_pnl` из
закрытого Контура 1 и на Dune; живых акторов там нет. Здесь связка выводится из
СОБСТВЕННЫХ журналов, стоит $0 и не зависит от внешних источников.

ДВА ПРИЗНАКА, оба нужны:
  1. Совпадение множеств токенов (Jaccard) — сильный, но медленный: требует истории.
     Считается по `signals.log`, глубина 30 суток.
  2. Совместная покупка одного токена в пределах секунды — быстрый: связка видна
     на десятке сделок. Считается по `actor_buys.jsonl` (там ts с долями секунды).
Признаки объединяются: срабатывание ЛЮБОГО связывает кошельки, дальше Union-Find.

Run:  python -m src.lockstep [--сохранить] [--jaccard 0.8] [--окно 1.0]
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from itertools import combinations

from . import analysis, config

JACCARD = 0.8          # доля общих токенов, выше которой кошельки считаются одним лицом
МИН_ТОКЕНОВ = 40       # меньше — статистика пересечения не значима
ОКНО_С = 1.0           # совместная покупка в пределах секунды = один отправитель
МИН_СОВМЕСТНЫХ = 10    # меньше — совпадение может быть случайным
ДОЛЯ_СОВМЕСТНЫХ = 0.8  # какая часть покупок кошелька приходится на совместные
ФАЙЛ = "lockstep.json"


class _UF:
    """Union-Find: связки сливаются в группы транзитивно (A~B, B~C → A,B,C)."""

    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def найти(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def союз(self, a: str, b: str) -> None:
        ra, rb = self.найти(a), self.найти(b)
        if ra != rb:
            self.p[ra] = rb


def _по_токенам(сигналы: list[dict], jaccard: float, мин: int) -> list[tuple]:
    """Признак 1: кошельки, ходящие по одним и тем же токенам."""
    токены: dict[str, set[str]] = defaultdict(set)
    for r in сигналы:
        s = r.get("signal") or {}
        t = s.get("token_mint")
        if not t:
            continue
        for a in dict.fromkeys(s.get("actors") or []):
            токены[a].add(t)
    годные = {a: s for a, s in токены.items() if len(s) >= мин}
    связки = []
    for a, b in combinations(sorted(годные), 2):
        A, B = годные[a], годные[b]
        j = len(A & B) / len(A | B)
        if j >= jaccard:
            связки.append((a, b, "токены", round(j, 3), len(A & B)))
    return связки


def _по_времени(покупки: list[dict], окно: float, мин: int, доля: float) -> list[tuple]:
    """Признак 2: кошельки, покупающие один токен в пределах секунды.

    Именно так выглядит участник, рассылающий покупку с нескольких адресов одним
    пакетом: у найденной пары медиана задержки набора конфлюенса ровно 0.0 секунды.
    """
    по_токену: dict[str, list[tuple[float, str]]] = defaultdict(list)
    токены: dict[str, set[str]] = defaultdict(set)
    for r in покупки:
        t, a, ts = r.get("token_mint"), r.get("actor") or r.get("wallet"), r.get("ts")
        if not t or not a or ts is None:
            continue
        по_токену[t].append((float(ts), a))
        токены[a].add(t)
    # считаем ТОКЕНЫ, а не пары событий: один кошелёк докупает один токен несколько
    # раз, и подсчёт пар давал долю 1.803 — величину, которая не может быть долей
    вместе: dict[tuple[str, str], set[str]] = defaultdict(set)
    for tok, v in по_токену.items():
        v.sort()
        for i, (t1, a1) in enumerate(v):
            for t2, a2 in v[i + 1:]:
                if t2 - t1 > окно:
                    break
                if a1 != a2:
                    вместе[tuple(sorted((a1, a2)))].add(tok)
    связки = []
    for (a, b), общие in вместе.items():
        c = len(общие)
        if c < мин:
            continue
        д = c / min(len(токены[a]), len(токены[b]))
        if д >= доля:
            связки.append((a, b, "время", round(д, 3), c))
    return связки


def построить(jaccard: float = JACCARD, окно: float = ОКНО_С,
              мин_токенов: int = МИН_ТОКЕНОВ) -> dict:
    """→ {'группы': [[кошелёк,...]], 'связки': [...], 'посчитано': ts, ...}"""
    # в signals.log пишутся И записи выходов (deliver_exit) — считать их сигналами
    # нельзя, иначе отчёт завышает объём выборки почти вдвое
    сигналы = [r for r in analysis.read_jsonl("signals.log") if r.get("signal")]
    покупки = analysis.read_jsonl("actor_buys.jsonl")
    связки = (_по_токенам(сигналы, jaccard, мин_токенов)
              + _по_времени(покупки, окно, МИН_СОВМЕСТНЫХ, ДОЛЯ_СОВМЕСТНЫХ))
    uf = _UF()
    for a, b, *_ in связки:
        uf.союз(a, b)
    гр: dict[str, set[str]] = defaultdict(set)
    for a in list(uf.p):
        гр[uf.найти(a)].add(a)
    группы = sorted((sorted(v) for v in гр.values() if len(v) > 1), key=len, reverse=True)
    return {"посчитано": time.time(), "сигналов": len(сигналы), "покупок": len(покупки),
            "группы": группы, "связки": [list(s) for s in связки]}


def загрузить(путь=None) -> dict[str, str]:
    """→ {кошелёк: id группы}. Пустой словарь, если файла нет — тогда каждый сам за себя."""
    p = путь or (config.OUTPUT_DIR / ФАЙЛ)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:  # noqa: BLE001 — отсутствие файла не должно ронять монитор
        return {}
    из = {}
    for гр in d.get("группы") or []:
        if not гр:
            continue
        for w in гр:
            из[w] = гр[0]
    return из


def независимых(акторы, карта: dict[str, str] | None = None) -> int:
    """Сколько РАЗНЫХ участников стоит за списком кошельков.

    Пустая карта → каждый кошелёк сам по себе, поведение как до правки.
    """
    карта = загрузить() if карта is None else карта
    return len({карта.get(a, a) for a in акторы})


def сохранить(d: dict) -> None:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.OUTPUT_DIR / (ФАЙЛ + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    tmp.replace(config.OUTPUT_DIR / ФАЙЛ)


def отчёт(d: dict) -> str:
    строки = [f"СВЯЗКИ КОШЕЛЬКОВ · сигналов {d['сигналов']}, покупок {d['покупок']}",
              f"групп: {len(d['группы'])}"]
    for гр in d["группы"]:
        строки.append(f"  из {len(гр)}: " + ", ".join(w[:8] for w in гр))
    по_признаку: dict[str, int] = defaultdict(int)
    for с in d["связки"]:
        по_признаку[с[2]] += 1
    строки.append("связок по признакам: " + ", ".join(
        f"{k} {v}" for k, v in sorted(по_признаку.items())) or "нет")
    if not d["группы"]:
        строки.append("ни одной связки — либо их нет, либо данных мало")
    return "\n".join(строки)


def main() -> None:
    ap = argparse.ArgumentParser(description="Связки кошельков одного участника")
    ap.add_argument("--jaccard", type=float, default=JACCARD)
    ap.add_argument("--окно", type=float, default=ОКНО_С)
    ap.add_argument("--мин-токенов", type=int, default=МИН_ТОКЕНОВ)
    ap.add_argument("--сохранить", action="store_true", help=f"записать output/{ФАЙЛ}")
    a = ap.parse_args()
    d = построить(a.jaccard, getattr(a, "окно"), getattr(a, "мин_токенов"))
    print(отчёт(d))
    if a.сохранить:
        сохранить(d)
        print(f"записано в output/{ФАЙЛ}")


if __name__ == "__main__":
    main()
