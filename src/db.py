"""DuckDB — локальное аналитическое хранилище (L2). Кэш трейдов + результаты P&L.

Схема следует внутреннему контракту сделки из README:
  token_mint, wallet, side, block_time, slot, base_amount, quote_amount_usd,
  price_usd, token_mc_usd (= price_usd * FIXED_SUPPLY для pump.fun).
"""
from __future__ import annotations

import duckdb

from .config import DB_PATH

# pump.fun: фиксированная эмиссия 1e9 токенов → MC = price_usd * SUPPLY.
# Для не-pump.fun токенов это приближение (помечается в отчёте).
FIXED_SUPPLY = 1_000_000_000.0


def connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH))


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    token_mint        VARCHAR,
    slot              BIGINT,
    block_time        TIMESTAMP,
    tx_id             VARCHAR,
    wallet            VARCHAR,
    project           VARCHAR,
    side              VARCHAR,   -- 'buy' | 'sell' относительно token_mint
    base_amount       DOUBLE,    -- кол-во токена M в сделке
    quote_amount_usd  DOUBLE,    -- объём в USD
    price_usd         DOUBLE,    -- quote_amount_usd / base_amount (NULL если неизвестно)
    token_mc_usd      DOUBLE     -- price_usd * FIXED_SUPPLY (NULL если price неизвестен)
);
CREATE TABLE IF NOT EXISTS wallet_pnl (
    token_mint             VARCHAR,
    wallet                 VARCHAR,
    n_buys                 INTEGER,
    n_sells                INTEGER,
    bought_qty             DOUBLE,
    bought_usd             DOUBLE,
    sold_qty               DOUBLE,
    sold_usd               DOUBLE,
    realized_pnl_usd       DOUBLE,
    roi                    DOUBLE,     -- realized_pnl / bought_usd
    remaining_qty          DOUBLE,
    first_buy_slot         BIGINT,
    first_buy_time         TIMESTAMP,
    first_buy_price_usd    DOUBLE,
    first_buy_mc_usd       DOUBLE,     -- MC токена на момент первого входа кошелька
    last_sell_time         TIMESTAMP,
    hold_seconds           DOUBLE,
    entry_frac             DOUBLE,     -- first_buy_price / peak_price (меньше = раньше)
    sold_without_basis_qty DOUBLE,     -- продано больше, чем куплено (трансферы/дуст/инсайдер)
    unbacked_proceeds_usd  DOUBLE      -- выручка с безбазисных продаж (НЕ входит в realized)
);
CREATE TABLE IF NOT EXISTS universe_cohort (
    mint         VARCHAR,
    first_seen   TIMESTAMP,
    last_seen    TIMESTAMP,
    n_trades     BIGINT,
    n_traders    BIGINT,
    usd_vol      DOUBLE,
    peak_mc_max  DOUBLE,
    peak_mc_p99  DOUBLE
);
CREATE TABLE IF NOT EXISTS universe (
    mint         VARCHAR,
    first_seen   TIMESTAMP,
    last_seen    TIMESTAMP,
    n_trades     BIGINT,
    n_traders    BIGINT,
    usd_vol      DOUBLE,
    peak_mc_max  DOUBLE,     -- пик MC по max(price) (чувствителен к дуст-выбросам)
    peak_mc_p99  DOUBLE,     -- пик MC по p99(price) (робастный) — используем для классификации
    class        VARCHAR     -- 'pumped' | 'dead'
);
"""


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA)
