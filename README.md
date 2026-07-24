# smart-money — discovery недооткрытых кошельков (Контур 1)

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![DuckDB](https://img.shields.io/badge/DuckDB-analytics-FFF000?logo=duckdb&logoColor=black)
![Solana](https://img.shields.io/badge/Solana-on--chain-9945FF?logo=solana&logoColor=white)
![Dune](https://img.shields.io/badge/Dune-dex__solana.trades-F4511E)
![status](https://img.shields.io/badge/status-WIP-yellow)
![last commit](https://img.shields.io/github/last-commit/Aleksandr-Sit/smart-money)

Поиск кошельков, которые **систематически** заходят рано в мем-токены Solana и выходят
в прибыль на **многих** токенах, с приоритетом на **недооткрытые** адреса (которых ещё
нет в публичных лидербордах GMGN/Cielo). Результат Контура 1 — ранжированный `watchlist`.

Контур 2 (live monitoring + сигналы в Telegram) строится ПОСЛЕ того, как watchlist
пережил валидацию. Не раньше.

## ✨ Ключевое

- **On-chain аналитика на больших данных** — Dune (`dex_solana.trades`) → локальный DuckDB, инфраструктура **$0/мес**.
- **Метрики систематичности** — ранний вход × прибыльность на многих токенах × «недооткрытость» (нет в публичных лидербордах).
- **Первый модуль валидирован**; честный **WIP**-статус — Контур 2 строится только после валидации watchlist.

---

## Источник данных

| Источник | Роль | Тариф |
|----------|------|-------|
| **Dune** | Контур 1: universe + ранние покупатели + кросс-токенный P&L (SQL) | Free (2500 кредитов/мес) → платный по факту |
| Bitquery | Контур 2: стрим watchlist + точечные P&L | подключаем позже |

> **Flipside НЕ используем** — data-бизнес продан SonarX, платформа Flipspace отключена
> 17.06.2026. (Проверено 01.07.2026.)

Стратегия по деньгам: прототип на Dune **Free ($0)** на маленьком universe → замер расхода
кредитов на батч токенов → решение о платном тарифе (Analyst/Plus $399) только по факту.

---

## Архитектура Фазы 1 (`discover.py`)

```
1. UNIVERSE        Dune SQL: pump.fun токены с peak_MC ≥ PEAK_MC_MIN за LOOKBACK_DAYS,
                   зрелость ≥ MATURITY_DAYS.  + ВЫБОРКА МЁРТВЫХ токенов (анти-survivorship).
2. EARLY BUYERS    Dune SQL по каждому токену: вход при MC ≤ EARLY_FRAC×peak ИЛИ в первые
                   EARLY_MINUTES; реализованный P&L (buys+sells) на кошелёк.
3. AGGREGATE       duckdb/pandas локально: wallet → [токены]; n_early, n_win, hit_rate,
                   agg_profit, median_roi, avg_entry_frac, avg_hold_time, recency, losses.
4. COPYABILITY     флаги: deploy/genesis-block, same-block снайперы, боты/вошинг → отсечь/пометить.
5. SPLIT           train/validation по дате T: кошельки отобраны на данных ДО T,
                   проверены на T..now.
6. SCORE + RANK    score = f(hit_rate, log(n_win), recency, agg_profit) − penalty(bot, too_fast, too_late).
                   Отсев one-hit-wonders (n_win < MIN_WINS). →  output/watchlist.{csv,json} + report.md
```

**Внутренний контракт** — нормализованная запись сделки (заполняется Dune-SQL, схема-агностична
к именам таблиц Dune, которые сверяются в каталоге при первом запуске):

```
Trade = {
  token_mint, wallet, side (buy|sell), block_time, slot,
  base_amount, quote_amount_usd, price_usd, token_mc_usd_at_trade
}
```

---

## Честные допущения (пойдут в report.md)

- **Survivorship bias.** Universe только из «памповых» токенов делает любой кошелёк гением.
  Знаменатель `hit_rate` считается по **всем** ранним входам, включая **мёртвые** токены
  (они специально добавлены в universe). Меряем hit_rate по всем ранним входам, не по победам.
- **Look-ahead bias.** Ранжируем/сигналим только по инфо, доступной на момент входа.
  Никакого «подглядывания» в будущую цену при принятии решения.
- **Overfitting.** Пороги НЕ подкручиваются под красивый список. train/validation split по дате T.
- **Alpha decay.** Известные кошельки уже фронтранятся. Ценность — недооткрытые адреса.
- **Не финсовет.** Копи-трейд мемов — высокорисковая адверсариальная игра.

---

## Запуск

```bash
cp .env.example .env          # вписать DUNE_API_KEY
pip install -r requirements.txt
python discover.py            # → ./output/watchlist.{csv,json} + report.md
```

Все пороги — в `config/params.yaml` (не в коде).

---

## Статус

- [x] Каркас, методология, параметры
- [x] Build-vs-buy разбор (`docs/build_vs_buy.md`) — пол затрат $0/мес, мандаторных BUY нет
- [x] Dune API-ключ + сверка имён таблиц: `dex_solana.trades` (pumpdotfun+pumpswap+raydium…), `run_sql` на free-тарифе
- [x] Модуль A: per-token realized P&L (`src/pnl.py`, средневзвеш. себестоимость) — **валидирован** на 2 токенах (тождество err ~1e-9), кэш в DuckDB, безбазисные продажи выделены в `unbacked_proceeds_usd`
- [x] Модуль B: universe с мёртвыми токенами (`universe.py`) — пик по p99, гейт по объёму, кэш когорты
- [x] Модуль C: агрегация в Dune (early-buyer P&L, `aggregate.py`) — early=абс. MC≤$50k (без look-ahead), честный знаменатель
- [x] Модуль D: флаги copyability/insider/bot (`score.py`) + скоринг/ранжирование → `output/watchlist.{csv,json}`
- [x] holdout-проверка + `report.md` (`report.py`) — на pilot 2x lift, но незначимо (нужен масштаб)
- [ ] **МАСШТАБ:** сотни токенов + несколько окон запуска → temporal split, стат-значимость
- [ ] Контур 2 (monitor + Telegram) — после валидации watchlist на масштабе
