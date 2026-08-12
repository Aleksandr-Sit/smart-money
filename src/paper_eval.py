"""Оценка PAPER — форвард-валидация флоу-сигнала по РЕАЛИЗОВАННЫМ round-trip (вход→выход).

Главное: paper_closed.jsonl (закрытые позиции с realized_pnl + причина выхода) → win-rate,
медиана/среднее доходности, разбивка по причинам. Плюс открытые позиции (нереализованные).

Run:  .venv\\Scripts\\python.exe -m src.paper_eval
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict

from . import analysis, config, market


def _load(name: str) -> list[dict]:
    p = config.OUTPUT_DIR / name
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def main() -> None:
    # ЧЕРЕЗ analysis.load_closed, а не своим чтением (правка 11.08). Прямое чтение
    # `realized_pnl` обходило две защиты сразу: отсечку аномалий (в журнале лежат 18
    # исторических записей с невозможной ценой выхода, одна на +12 832 014 201) и вычет
    # комиссии. Первая раздувала среднее до сотен миллионов процентов, вторая завышала
    # каждую сделку на EXIT_FEE. Поле `pnl` уже net и уже с учётом источника итога.
    closed = analysis.load_closed()
    сырых = sum(1 for r in _load("paper_closed.jsonl") if r.get("realized_pnl") is not None)
    print(f"=== ЗАКРЫТЫЕ round-trip (реализованные): {len(closed)} ===")
    if сырых != len(closed):
        print(f"    отсеяно аномальных/без даты входа: {сырых - len(closed)} из {сырых}")
    if closed:
        rets = [c["pnl"] for c in closed]
        wins = sum(1 for x in rets if x > 0)
        # по полю `по_деньгам`, а НЕ по метке `pnl_source`: метка до правки 11.08
        # ставилась каждому бумажному выходу, и счёт по ней давал 1644 «денежных»
        # сделки при 188 действительно живых
        деньги = sum(1 for c in closed if c.get("по_деньгам"))
        print(f"win-rate: {wins/len(closed):.2f} | median: {statistics.median(rets):+.1%} | "
              f"mean: {statistics.mean(rets):+.1%} | сумма: {sum(rets):+.1%}")
        print(f"итог по деньгам: {деньги} сделок, по модели: {len(closed)-деньги}")
        by = defaultdict(list)
        for c in closed:
            by[c.get("reason", "?")].append(c["pnl"])
        print("по причине выхода:")
        for reason, rr in sorted(by.items(), key=lambda kv: -len(kv[1])):
            w = sum(1 for x in rr if x > 0)
            print(f"  {reason:<12} n={len(rr):<3} win {w/len(rr):.2f} median {statistics.median(rr):+.1%}")

    # открытые позиции (нереализованные, по текущей цене)
    try:
        openp = json.loads((config.OUTPUT_DIR / "open_positions.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        openp = {}
    print(f"\n=== ОТКРЫТЫЕ позиции: {len(openp)} ===")
    unreal = []
    for token, p in list(openp.items())[:40]:
        info = market.token_info(token)
        cur = info.get("price_usd")
        ep = p.get("entry_price")
        ur = (cur / ep - 1) if (cur and ep) else None
        if ur is not None:
            unreal.append(ur)
        print(f"  {token[:14]:<14} entry_mc={p.get('entry_mc')} "
              f"актор_вышло={len(p.get('exited_actors', []))}/{len(p.get('entry_actors', []))} "
              f"unreal={f'{ur:+.0%}' if ur is not None else 'n/a'}")
    if unreal:
        print(f"нереализованная медиана: {statistics.median(unreal):+.1%}")


if __name__ == "__main__":
    main()
