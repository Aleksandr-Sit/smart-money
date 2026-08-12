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


def склеить(точки: list[tuple]) -> list[tuple]:
    """Привести траекторию к ОДНОЙ шкале на стыке источников цены.

    ЗАЧЕМ (замер 11.08 на 1 756 276 выборках). Трекер читает цену с бондинг-кривой,
    а после выпуска токена на DEX переключается на DexScreener. На стыке цена прыгает,
    хотя рынок не двинулся:

        curve→curve  n=1 180 538  медиана 1.0000   <- контроль
        dex→dex      n=  553 041  медиана 1.0000   <- контроль
        curve→dex    n=    1 599  медиана 1.1464   максимум 59.9

    Контроль внутри каждого источника — ровно единица, значит 14.6% на стыке это
    шкала, а не движение цены. Бьёт по лучшим сделкам: грэдуируют как раз победители,
    и шесть из десяти крупнейших результатов схлопывались с +1000…+3500% до нуля.

    Склейка домножает последующий отрезок так, чтобы первая выборка нового источника
    совпала с последней выборкой прежнего. Переход `signal`→`curve` НЕ склеивается:
    якорь `signal` — это цена актора, а расхождение с ней есть настоящий разрыв
    исполнения (медиана 0.9452), и гасить его склейкой значило бы прятать проблему.

    точки — [(ts, price, src), ...] по возрастанию ts. → тот же список с новой ценой.
    """
    из: list[tuple] = []
    k = 1.0
    пред = None
    for ts, цена, src in точки:
        if (пред is not None and {src, пред[2]} == {"curve", "dex"}
                and пред[1] > 0 and цена > 0):
            k *= пред[1] / цена
        из.append((ts, цена * k, src))
        пред = (ts, цена, src)
    return из


def траектории(нужны: set[str] | None = None, directory: Path | None = None,
               splice: bool = True) -> dict[str, list[tuple]]:
    """Траектории из price_history.jsonl → {mint: [(ts, price, src), ...]}.

    splice=True (по умолчанию) приводит каждую траекторию к одной шкале. Отключать
    только для замера самого артефакта — во всех остальных случаях сырые ряды дают
    завышенный результат на грэдуировавших токенах.
    """
    трек: dict[str, list[tuple]] = {}
    path = (directory or config.OUTPUT_DIR) / "price_history.jsonl"
    if not path.exists():
        return трек
    with open(path, encoding="utf-8") as f:
        for line in f:
            if '"price_usd"' not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            m, p = r.get("mint"), r.get("price_usd")
            if not m or not p or p <= 0:
                continue
            if нужны is not None and m not in нужны:
                continue
            трек.setdefault(m, []).append((float(r["ts"]), float(p), r.get("src")))
    for m, v in трек.items():
        v.sort()
        if splice:
            трек[m] = склеить(v)
    return трек


def load_closed(directory: Path | None = None, with_fee: bool = True,
                since: float | None = None, until: float | None = None) -> list[dict]:
    """Закрытые позиции с отсечкой аномалий и приведённым PnL.

    with_fee=True вычитает EXIT_FEE — в realized_pnl комиссии НЕТ, монитор считает
    net отдельно. Забыть об этом — значит завысить результат на 6% на каждой сделке.
    since/until — границы по entry_ts, чтобы исключать окна с известным дефектом кода.
    → список записей с добавленными полями pnl (net) и exit_ts.

    ИСКЛЮЧЕНИЕ (найдено 11.08): у записей с `pnl_source == "деньги"` комиссии УЖЕ внутри
    полученной суммы — она посчитана по дельтам транзакций. Вычитать EXIT_FEE ещё раз
    значит занижать живые сделки на 12%, то есть ровно на ту величину, вокруг которой
    идёт весь спор о доходности. Комиссия вычитается только из модельного результата.
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
        # Нужны ОБА признака. Метка `pnl_source` до правки 11.08 ставилась неверно:
        # монитор передавал модельный итог в параметр «фактические деньги», и все
        # бумажные выходы с 10.08 помечены «деньгами». Доверять одной метке — значит
        # не вычесть комиссию из целого пласта бумажной истории. Реальные деньги
        # бывают только в живом режиме, поэтому решает пара «mode + источник».
        по_деньгам = r.get("pnl_source") == "деньги" and r.get("mode") == "live"
        out.append({**r, "pnl": r["realized_pnl"] - (0.0 if по_деньгам else fee),
                    "exit_ts": exit_ts, "по_деньгам": по_деньгам})
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
