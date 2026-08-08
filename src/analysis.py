"""Чтение журналов для анализа с отсечкой аномалий источника.

ЗАЧЕМ (аудит 08.08). В paper_closed.jsonl лежат четыре июльские записи с ценой
выхода до $153 000 за токен — следствие пылевых продаж, которые тогда не
фильтровались. Они не удалены намеренно: журнал сырых событий переписывать нельзя,
иначе теряется история. Но при чтении их обязательно отсекать.

Во что обходится их игнорирование, на живых данных 08.08 (7236 закрытий):
    среднее со всеми аномалиями   +177 335 806%
    среднее без них                      +19.7%
    медиана (не зависит)                  +0.2%
Любой отчёт по среднему без фильтра — мусор. Медиана устойчива, но по ней одной
хвостовую стратегию оценивать нельзя: у нас четверть прибыли делают 0.7% сделок.

Порог MAX_ABS_PNL = 20 (то есть +2000%) выбран по данным: настоящий максимум
среди корректных сделок — +593%, а все четыре аномалии выше +2200%. Между ними
разрыв почти в четыре раза, поэтому порог не пограничный.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import config

MAX_ABS_PNL = 20.0        # |PnL| выше = аномалия источника, а не сделка


def read_jsonl(name: str, directory: Path | None = None) -> list[dict]:
    """Прочитать JSONL, пропуская битые строки (обрыв записи при рестарте)."""
    path = (directory or config.OUTPUT_DIR) / name
    out = []
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    return out


def is_sane_pnl(value) -> bool:
    """PnL правдоподобен? Отсекает аномалии источника в обе стороны."""
    return isinstance(value, (int, float)) and abs(value) <= MAX_ABS_PNL


def load_closed(directory: Path | None = None, with_fee: bool = True,
                since: float | None = None, until: float | None = None) -> list[dict]:
    """Закрытые позиции с отсечкой аномалий и приведённым PnL.

    with_fee=True вычитает EXIT_FEE — в realized_pnl комиссии НЕТ, монитор считает
    net отдельно. Забыть об этом — значит завысить результат на 6% на каждой сделке.
    since/until — границы по entry_ts, чтобы исключать окна с известным дефектом кода.
    → список записей с добавленными полями pnl (net) и exit_ts.
    """
    from . import strategy
    fee = strategy.RISK["EXIT_FEE"] if with_fee else 0.0
    out = []
    for r in read_jsonl("paper_closed.jsonl", directory):
        if r.get("type") != "exit" and "realized_pnl" not in r:
            continue
        if not is_sane_pnl(r.get("realized_pnl")):
            continue
        ets = r.get("entry_ts")
        if ets is None:
            continue
        if since is not None and ets < since:
            continue
        if until is not None and ets >= until:
            continue
        try:
            exit_ts = datetime.fromisoformat(r["ts"]).timestamp()
        except Exception:  # noqa: BLE001
            continue
        out.append({**r, "pnl": r["realized_pnl"] - fee, "exit_ts": exit_ts})
    out.sort(key=lambda x: x["exit_ts"])
    return out


def anomaly_report(directory: Path | None = None) -> dict:
    """Сколько записей отсекается и как это меняет среднее. Для проверки самой отсечки."""
    import statistics
    raw = [r["realized_pnl"] for r in read_jsonl("paper_closed.jsonl", directory)
           if isinstance(r.get("realized_pnl"), (int, float))]
    clean = [x for x in raw if abs(x) <= MAX_ABS_PNL]
    dropped = [x for x in raw if abs(x) > MAX_ABS_PNL]
    return {
        "всего": len(raw), "отсечено": len(dropped),
        "среднее_со_всеми": statistics.mean(raw) if raw else 0.0,
        "среднее_без_аномалий": statistics.mean(clean) if clean else 0.0,
        "медиана": statistics.median(raw) if raw else 0.0,
        "максимум_корректной": max(clean) if clean else 0.0,
        "минимум_отсечённой": min((abs(x) for x in dropped), default=None),
    }


if __name__ == "__main__":
    rep = anomaly_report()
    print("ОТСЕЧКА АНОМАЛИЙ В paper_closed.jsonl")
    print(f"  записей всего: {rep['всего']}, отсечено: {rep['отсечено']}")
    print(f"  среднее со всеми:     {rep['среднее_со_всеми']:+,.1%}")
    print(f"  среднее без аномалий: {rep['среднее_без_аномалий']:+.1%}")
    print(f"  медиана:              {rep['медиана']:+.1%}")
    print(f"  максимум корректной сделки: {rep['максимум_корректной']:+.0%}")
    if rep["минимум_отсечённой"]:
        print(f"  минимум отсечённой:         {rep['минимум_отсечённой']:+.0%}")
        print(f"  разрыв между ними: {rep['минимум_отсечённой']/max(rep['максимум_корректной'],1e-9):.1f}x "
              f"— порог не пограничный")
    n = len(load_closed())
    print(f"\n  load_closed() отдаёт {n} записей с вычтенной комиссией")
