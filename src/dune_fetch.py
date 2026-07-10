"""Забор данных из Dune с ретраями + кэш в DuckDB (чтобы не жечь кредиты на повторах).

Ключ DUNE_API_KEY читается из .env в рантайме и НИКОГДА не логируется.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from dune_client.client import DuneClient
from requests.exceptions import ChunkedEncodingError
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import Timeout
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from . import db
from .config import secret


def _is_transient(e: BaseException) -> bool:
    """Ретраить только сетевые сбои, НЕ query-ошибки и НЕ 402/4xx (они детерминированы)."""
    return isinstance(e, (ReqConnectionError, Timeout, ChunkedEncodingError))

SQL_DIR = Path(__file__).resolve().parent / "sql"
# base58 без 0 O I l
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _client() -> DuneClient:
    return DuneClient(secret("DUNE_API_KEY"), request_timeout=120)


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception(_is_transient),
    reraise=True,
)
def run_sql(sql: str) -> list[dict]:
    """Выполнить произвольный SQL через Dune run_sql. Ретраи на сетевые сбои."""
    res = _client().run_sql(query_sql=sql, is_private=True)
    return res.get_rows()


def _parse_ts(s: str) -> datetime | None:
    """Dune отдаёт '2026-07-01 11:59:59.000 UTC' → naive datetime (UTC)."""
    if not s:
        return None
    s = s.replace(" UTC", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _normalize(rows: list[dict], mint: str) -> list[tuple]:
    """Строки Dune → нормализованные кортежи под таблицу trades (контракт README)."""
    out: list[tuple] = []
    for r in rows:
        if r.get("bought_mint") == mint:
            side = "buy"
            base = float(r.get("bought_amount") or 0.0)
        elif r.get("sold_mint") == mint:
            side = "sell"
            base = float(r.get("sold_amount") or 0.0)
        else:
            continue
        usd = float(r.get("amount_usd") or 0.0)
        price = usd / base if base > 0 and usd > 0 else None
        mc = price * db.FIXED_SUPPLY if price is not None else None
        out.append(
            (
                mint,
                int(r["slot"]),
                _parse_ts(r.get("block_time")),
                r.get("tx_id"),
                r.get("wallet"),
                r.get("project"),
                side,
                base,
                usd,
                price,
                mc,
            )
        )
    return out


def _normalize_batch(rows: list[dict], mint_set: set[str]) -> list[tuple]:
    """Строки batch-запроса → кортежи trades. token_mint = та сторона, что в наборе."""
    out: list[tuple] = []
    for r in rows:
        bm, sm = r.get("bought_mint"), r.get("sold_mint")
        if bm in mint_set:
            mint, side, base = bm, "buy", float(r.get("bought_amount") or 0.0)
        elif sm in mint_set:
            mint, side, base = sm, "sell", float(r.get("sold_amount") or 0.0)
        else:
            continue
        usd = float(r.get("amount_usd") or 0.0)
        price = usd / base if base > 0 and usd > 0 else None
        mc = price * db.FIXED_SUPPLY if price is not None else None
        out.append((mint, int(r["slot"]), _parse_ts(r.get("block_time")), r.get("tx_id"),
                    r.get("wallet"), r.get("project"), side, base, usd, price, mc))
    return out


def fetch_universe_trades(since: str, force: bool = False) -> int:
    """Забрать сделки ВСЕХ токенов из таблицы universe одним batch-запросом (экономия кредитов).

    Уже закэшированные токены пропускаются (если не force).
    """
    con = db.connect()
    db.ensure_schema(con)
    mints = [r[0] for r in con.execute("SELECT mint FROM universe").fetchall()]
    if not mints:
        con.close()
        raise RuntimeError("universe пуст — сначала запусти src.universe")
    for m in mints:
        if not MINT_RE.match(m):
            con.close()
            raise ValueError(f"Некорректный mint в universe: {m!r}")

    if force:
        todo = mints
    else:
        cached = {r[0] for r in con.execute(
            "SELECT DISTINCT token_mint FROM trades WHERE token_mint IN "
            "(SELECT mint FROM universe)").fetchall()}
        todo = [m for m in mints if m not in cached]
    if not todo:
        print(f"[cache] все {len(mints)} токенов universe уже в trades (skip Dune)")
        con.close()
        return 0

    mint_list = ",".join(f"'{m}'" for m in todo)
    sql = (SQL_DIR / "token_trades_batch.sql").read_text(encoding="utf-8")
    sql = sql.replace("{{since}}", since).replace("{{mints}}", mint_list)
    print(f"[dune] batch fetch {len(todo)} tokens since {since} ...")
    rows = run_sql(sql)
    tuples = _normalize_batch(rows, set(todo))

    con.execute(f"DELETE FROM trades WHERE token_mint IN ({mint_list})")
    if tuples:
        con.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuples)
    con.close()
    print(f"[dune] got {len(rows)} rows -> {len(tuples)} trades across {len(todo)} tokens cached")
    return len(tuples)


def fetch_token_trades(mint: str, since: str, force: bool = False) -> int:
    """Забрать все сделки токена и закэшировать в DuckDB. Вернуть число строк.

    Если данные уже в кэше и force=False — Dune не дёргаем (экономия кредитов).
    """
    if not MINT_RE.match(mint):
        raise ValueError(f"Некорректный mint: {mint!r}")

    con = db.connect()
    db.ensure_schema(con)

    cached = con.execute(
        "SELECT count(*) FROM trades WHERE token_mint = ?", [mint]
    ).fetchone()[0]
    if cached and not force:
        print(f"[cache] {mint}: {cached} trades already in DuckDB (skip Dune)")
        con.close()
        return cached

    sql = (SQL_DIR / "token_trades.sql").read_text(encoding="utf-8")
    sql = sql.replace("{{mint}}", mint).replace("{{since}}", since)
    print(f"[dune] fetching trades for {mint} since {since} ...")
    rows = run_sql(sql)
    tuples = _normalize(rows, mint)

    con.execute("DELETE FROM trades WHERE token_mint = ?", [mint])
    if tuples:
        con.executemany(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuples
        )
    con.close()
    print(f"[dune] got {len(rows)} rows -> {len(tuples)} token trades cached")
    return len(tuples)
