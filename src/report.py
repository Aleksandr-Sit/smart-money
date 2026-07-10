"""Генерация output/report.md — методология, смещения, honest denominator, holdout.

Работает на кэшированных DuckDB-таблицах (universe, early_pnl). Dune не нужен.
CLI:  .venv\\Scripts\\python.exe -m src.report [--win-mult 5]
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime

from . import config, db


def _holdout(con, win_mult: float, seed: int = 42) -> dict:
    """Cross-sectional holdout: отобрать кошельки по половине токенов A, проверить на B."""
    rows = con.execute("SELECT mint, wallet, multiple FROM early_pnl").fetchall()
    mints = sorted({m for m, _, _ in rows})
    rng = random.Random(seed)
    rng.shuffle(mints)
    half = len(mints) // 2
    setA, setB = set(mints[:half]), set(mints[half:])

    ea, wa, eb, wb = {}, {}, {}, {}
    for mint, wallet, mult in rows:
        win = 1 if (mult is not None and mult >= win_mult) else 0
        if mint in setA:
            ea[wallet] = ea.get(wallet, 0) + 1
            wa[wallet] = wa.get(wallet, 0) + win
        else:
            eb[wallet] = eb.get(wallet, 0) + 1
            wb[wallet] = wb.get(wallet, 0) + win

    selected = {w for w in ea if ea[w] >= 2 and wa.get(w, 0) >= 1}
    base_e = sum(eb.values())
    base_w = sum(wb.values())
    sel_e = sum(eb.get(w, 0) for w in selected)
    sel_w = sum(wb.get(w, 0) for w in selected)
    return {
        "tokens_A": len(setA), "tokens_B": len(setB),
        "selected_on_A": len(selected),
        "baseline_hit_B": (base_w / base_e) if base_e else 0.0,
        "selected_hit_B": (sel_w / sel_e) if sel_e else 0.0,
        "selected_early_B": sel_e,
    }


def _temporal_split(con, win_mult: float, split: str = "2026-06-15") -> dict:
    """Temporal split: отбор кошельков на токенах, запущенных ДО split, валидация на запущенных ПОСЛЕ.

    Тест на alpha decay во времени (в отличие от cross-sectional holdout).
    """
    rows = con.execute(
        """SELECT e.mint, e.wallet, e.multiple, u.first_seen
           FROM early_pnl e JOIN universe u ON u.mint = e.mint"""
    ).fetchall()
    T = datetime.strptime(split, "%Y-%m-%d")
    ea, wa, eb, wb = {}, {}, {}, {}
    ta = set()
    tb = set()
    for mint, wallet, mult, first_seen in rows:
        win = 1 if (mult is not None and mult >= win_mult) else 0
        if first_seen < T:                       # train (раннее)
            ta.add(mint)
            ea[wallet] = ea.get(wallet, 0) + 1
            wa[wallet] = wa.get(wallet, 0) + win
        else:                                    # validation (позднее)
            tb.add(mint)
            eb[wallet] = eb.get(wallet, 0) + 1
            wb[wallet] = wb.get(wallet, 0) + win
    selected = {w for w in ea if ea[w] >= 2 and wa.get(w, 0) >= 1}
    base_e, base_w = sum(eb.values()), sum(wb.values())
    sel_e = sum(eb.get(w, 0) for w in selected)
    sel_w = sum(wb.get(w, 0) for w in selected)
    return {
        "split": split, "train_tokens": len(ta), "val_tokens": len(tb),
        "selected_on_train": len(selected), "selected_early_val": sel_e,
        "baseline_hit_val": (base_w / base_e) if base_e else 0.0,
        "selected_hit_val": (sel_w / sel_e) if sel_e else 0.0,
    }


def _temporal_split_entities(con, win_mult: float, split: str = "2026-06-15",
                             breadth_max: int = 300) -> dict:
    """Temporal split НА УРОВНЕ АКТОРОВ + ИСКЛЮЧЕНИЕ SPRAY-БОТОВ (breadth_max).

    Самая честная проверка: кошельки кластера схлопнуты, (токен,актор) раз, и из ОТБОРА
    исключены акторы с широтой следа > breadth_max (spray-боты «предсказывают» тривиально)."""
    from collections import defaultdict
    try:
        emap = {w: (cid or w) for w, cid, _ in
                con.execute("SELECT wallet, cluster_id, cluster_size FROM clusters").fetchall()}
    except Exception:  # noqa: BLE001
        emap = {}
    try:
        fp = {w: (tt or 0) for w, tt in
              con.execute("SELECT wallet, total_tokens FROM wallet_footprint").fetchall()}
    except Exception:  # noqa: BLE001
        fp = {}
    ent_breadth: dict = defaultdict(int)   # макс широта по членам актора
    for w, ent in emap.items():
        ent_breadth[ent] = max(ent_breadth[ent], fp.get(w, 0))

    rows = con.execute(
        """SELECT e.mint, e.wallet, e.multiple, u.first_seen
           FROM early_pnl e JOIN universe u ON u.mint = e.mint"""
    ).fetchall()
    T = datetime.strptime(split, "%Y-%m-%d")
    aw, bw = defaultdict(int), defaultdict(int)   # (entity,mint) -> win (макс по членам)
    for mint, wallet, mult, fs in rows:
        ent = emap.get(wallet, wallet)
        win = 1 if (mult is not None and mult >= win_mult) else 0
        d = aw if fs < T else bw
        d[(ent, mint)] = max(d[(ent, mint)], win)
    ea, wa, eb, wb = defaultdict(int), defaultdict(int), defaultdict(int), defaultdict(int)
    for (ent, _), win in aw.items():
        ea[ent] += 1; wa[ent] += win
    for (ent, _), win in bw.items():
        eb[ent] += 1; wb[ent] += win
    # отбор: активные победители на train, НЕ spray-боты
    selected = {e for e in ea if ea[e] >= 2 and wa[e] >= 1
                and ent_breadth.get(e, fp.get(e, 0)) <= breadth_max}
    base_e, base_w = sum(eb.values()), sum(wb.values())
    sel_e = sum(eb.get(e, 0) for e in selected)
    sel_w = sum(wb.get(e, 0) for e in selected)
    return {
        "selected_entities": len(selected), "selected_early_val": sel_e,
        "baseline_hit_val": (base_w / base_e) if base_e else 0.0,
        "selected_hit_val": (sel_w / sel_e) if sel_e else 0.0,
    }


def _cluster_summary(con) -> dict:
    try:
        total = con.execute("SELECT count(*) FROM clusters").fetchone()[0]
    except Exception:  # noqa: BLE001
        return {}
    if not total:
        return {}
    in_cl = con.execute("SELECT count(*) FROM clusters WHERE cluster_id IS NOT NULL").fetchone()[0]
    n_cl = con.execute("SELECT count(DISTINCT cluster_id) FROM clusters WHERE cluster_id IS NOT NULL").fetchone()[0]
    entities = con.execute("SELECT count(DISTINCT coalesce(cluster_id, wallet)) FROM clusters").fetchone()[0]
    top = con.execute(
        "SELECT cluster_id, max(cluster_size) FROM clusters WHERE cluster_id IS NOT NULL "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 5").fetchall()
    return {"total": total, "in_cluster": in_cl, "n_clusters": n_cl, "entities": entities, "top": top}


def build(win_mult: float, split: str = "2026-06-15", breadth_max: int = 300) -> None:
    con = db.connect()

    uni = con.execute(
        "SELECT class, count(*), min(peak_mc_p99), max(peak_mc_p99), min(first_seen), max(first_seen) "
        "FROM universe GROUP BY class ORDER BY class").fetchall()
    den = con.execute(
        "SELECT class, count(*), count(DISTINCT wallet), count(DISTINCT mint) "
        "FROM early_pnl GROUP BY class ORDER BY class").fetchall()
    n_wallets = con.execute("SELECT count(DISTINCT wallet) FROM early_pnl").fetchone()[0]
    n_rows = con.execute("SELECT count(*) FROM early_pnl").fetchone()[0]
    ho = _holdout(con, win_mult)
    tho = _temporal_split(con, win_mult, split)
    tent = _temporal_split_entities(con, win_mult, split, breadth_max)
    cs = _cluster_summary(con)
    con.close()

    cluster_md = ""
    if cs:
        top_str = ", ".join(f"{cid}={sz}" for cid, sz in cs["top"])
        cluster_md = f"""
## 4b. Кластеризация (Модуль E) — честность «широты»

Эвристика: кошельки, регулярно покупающие ОДНИ токены в ОДНИХ/соседних слотах → вероятно один
актор на многих кошельках. Из **{cs['total']}** кандидатов watchlist **{cs['in_cluster']}** попали в
**{cs['n_clusters']}** кластеров → **{cs['entities']} уникальных сущностей** (а не {cs['total']}).
Крупнейшие кластеры (размер): {top_str}.

> Без этого watchlist переоценивал бы широту: напр. «15 сверхранних гениев» на деле — несколько
> акторов, покупающих одни токены в одних слотах. `cluster_id`/`cluster_size` есть в watchlist.
> Кластеризация по slot co-occurrence (funding/CEX-эвристики — отдельный шаг, нужны доп. данные).
"""

    def uline(r):
        return (f"| {r[0]} | {r[1]} | ${r[2]:,.0f} | ${r[3]:,.0f} | {r[4]:%Y-%m-%d} | {r[5]:%Y-%m-%d} |")

    def dline(r):
        return f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |"

    md = f"""# Контур 1 — отчёт discovery (pilot)

_Сгенерировано: {datetime.now():%Y-%m-%d %H:%M} · win_mult = {win_mult}x_

> **Это не финсовет.** Копи-трейд мем-токенов — высокорисковая адверсариальная игра.
> Отчёт — о **методологии** и её честности, а не о готовых сигналах.

## 1. Universe (Модуль B)

Когорта pump.fun токенов по окну **запуска** (первый в истории трейд в окне). В universe
входят и запампившие, и **мёртвые** — иначе знаменатель `hit_rate` мерил бы только победителей.

| class | токенов | min peak MC (p99) | max peak MC (p99) | launch с | launch по |
|---|---|---|---|---|---|
{chr(10).join(uline(r) for r in uni)}

**Пик MC = `p99(price) × 1e9`**, не `max` — `max` ловит дуст-выбросы (единичный $0-трейд за
0.003 токена даёт фейковые сотни $M). «pumped» дополнительно гейтится по объёму (`usd_vol ≥ $50k`),
иначе токены с не-1e9 supply дают артефакт $14.7B.

## 2. Честный знаменатель (Модуль C)

`hit_rate = n_win / n_early`, где **n_early — ВСЕ ранние входы кошелька по universe** (pumped И dead),
а n_win — сколько из них дали `≥ {win_mult}x` реализованной прибыли.

| class токена | ранних входов | кошельков | токенов |
|---|---|---|---|
{chr(10).join(dline(r) for r in den)}

Всего ранних входов: **{n_rows}**, уникальных кошельков: **{n_wallets}**.
Большинство ранних входов — на токенах, которые **сдохли**. Это и есть корректный знаменатель.

## 3. Определения (без look-ahead)

- **Ранний вход** = первая покупка при **абсолютной MC ≤ $50k** (≈ до грэдуэйшн pump.fun ~$69k).
  Раньше бралось «≤5% от пика» — это (а) **асимметрично** (у пампера окно $100k, у мёртвого $3k →
  мёртвые не попадали в знаменатель) и (б) **look-ahead** (пик — будущая информация). Абсолютный
  порог знаем в момент входа и он одинаков для всех токенов.
- **realized P&L** ≈ `(avg_sell − avg_buy) × min(bought_qty, sold_qty)` — равно точному avg-cost на
  закрытых позициях (проверено, ошибка ~1e-9) и **исключает безбазисные продажи** инсайдеров
  (продал больше, чем купил на DEX = трансфер/дев-аллокация, не копируется).

## 4. Флаги копируемости (Модуль D)

- **snipe** — вход в пределах {2} слотов от launch-слота токена (деплой-блок) → не успеть повторить.
- **unbacked** — продажи без DEX-базиса (инсайдер/трансфер).
- **too_fast / bot_like** — hold < 300с или аномально много сделок на токен (вошинг/бот).
- `copyability = НЕ bot_like И frac_snipe<0.34 И frac_unbacked<0.34`.
{cluster_md}
## 5. Скоринг

`score = w_hit·hit_rate + w_nwin·log1p(n_win) + w_rec·recency + w_profit·log1p(agg_realized)
         − snipe − bot − 0.5·unbacked`. Отсев one-hit-wonders: боевой порог `n_early≥5, n_win≥3`.

## 6. Overfitting / holdout (cross-sectional)

Токены случайно делятся на A ({ho['tokens_A']}) и B ({ho['tokens_B']}). Кошельки отбираются по A
(`n_early_A≥2, n_win_A≥1`), затем меряется их hit_rate на **независимом** B:

- Отобрано по A: **{ho['selected_on_A']}** кошельков; их ранних входов на B: {ho['selected_early_B']}.
- **baseline** hit_rate на B (все): **{ho['baseline_hit_B']:.3f}**
- **selected** hit_rate на B (отобранные по A): **{ho['selected_hit_B']:.3f}**

> Если selected > baseline — есть признак переносимого edge.

## 6b. Temporal split (alpha decay во времени)

Токены делятся по дате запуска на **{tho['split']}**: train = запущены раньше ({tho['train_tokens']} токенов),
validation = позже ({tho['val_tokens']}). Кошельки отбираются по train, меряются на **будущих** токенах:

- Отобрано по train: **{tho['selected_on_train']}** кошельков; их ранних входов на validation: {tho['selected_early_val']}.
- **baseline** hit_rate на validation: **{tho['baseline_hit_val']:.3f}**
- **selected** hit_rate на validation: **{tho['selected_hit_val']:.3f}**

> Это честная проверка «выбрали в прошлом → работает в будущем». Если selected > baseline — edge
> переживает время. Строгая значимость требует большего числа токенов из разных периодов.

## 7. Ограничения pilot и смещения

- **Survivorship** — снят включением мёртвых токенов в знаменатель (см. §2).
- **Look-ahead** — снят абсолютным порогом раннего входа и realized-only P&L (§3).
- **Overfitting** — механизм holdout готов (§6), но требует **масштаба** для значимости.
- **Alpha decay / temporal split** — нужен universe из РАЗНЫХ периодов (train до T, validation после T).
  На pilot все токены из одного окна (июнь 27-29) → временной split вырожден. **Появится при масштабе.**
- **Малая выборка** — 36 токенов; боевой порог `n_win≥3` не набирает никто. Это ожидаемо.

## 8. Следующий шаг — масштаб

Код Контура 1 (B→C→D→scoring→report) собран и проверён на бесплатных pilot-данных. Дальше —
**один раз** расширить universe (сотни токенов, несколько окон запуска для temporal split) и
прогнать тот же отлаженный конвейер. Кредиты Dune тратятся уже на финальном коде.
"""
    (config.OUTPUT_DIR / "report.md").write_text(md, encoding="utf-8")
    print("report.md записан:", config.OUTPUT_DIR / "report.md")
    print(f"\nCross-sectional holdout: selected={ho['selected_hit_B']:.3f} vs baseline={ho['baseline_hit_B']:.3f} "
          f"({ho['selected_on_A']} кошельков, {ho['selected_early_B']} входов на B)")
    print(f"Temporal split ({tho['split']}): selected={tho['selected_hit_val']:.3f} vs baseline={tho['baseline_hit_val']:.3f} "
          f"({tho['selected_on_train']} кошельков, {tho['selected_early_val']} входов на validation)")
    print(f"Temporal split ПО АКТОРАМ + БЕЗ SPRAY-БОТОВ: selected={tent['selected_hit_val']:.3f} vs "
          f"baseline={tent['baseline_hit_val']:.3f} ({tent['selected_entities']} чистых акторов, "
          f"{tent['selected_early_val']} входов на validation)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Генерация report.md")
    ap.add_argument("--win-mult", type=float, default=5.0)
    ap.add_argument("--split", default="2026-06-15", help="дата temporal split (train<split, val>=split)")
    ap.add_argument("--breadth-max", type=int, default=300, help="исключить акторов с широтой следа > этого")
    args = ap.parse_args()
    build(args.win_mult, args.split, args.breadth_max)


if __name__ == "__main__":
    main()
