"""Модуль A — realized P&L по одному токену (средневзвешенная себестоимость).

Детерминированный расчёт по кэшированным сделкам из DuckDB. Логика:
  BUY  → увеличиваем позицию (qty, cost).
  SELL → фиксируем прибыль по средней цене, уменьшаем позицию.
  Продажа больше, чем куплено (трансфер/дуст) → базис 0, флаг sold_without_basis_qty.

CLI:
  .venv\\Scripts\\python.exe -m src.pnl <mint> [--since YYYY-MM-DD] [--force] [--top 30]
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import db, dune_fetch


@dataclass
class WalletState:
    n_buys: int = 0
    n_sells: int = 0
    bought_qty: float = 0.0
    bought_usd: float = 0.0
    sold_qty: float = 0.0
    sold_usd: float = 0.0
    realized: float = 0.0
    pos_qty: float = 0.0          # текущая позиция
    pos_cost: float = 0.0         # себестоимость текущей позиции
    sold_without_basis: float = 0.0
    unbacked_proceeds: float = 0.0  # выручка с продаж без DEX-базиса (инсайдер/трансфер)
    first_buy_slot: int | None = None
    first_buy_time: datetime | None = None
    first_buy_price: float | None = None
    last_sell_time: datetime | None = None


def _peak_price(con, mint: str) -> float | None:
    row = con.execute(
        "SELECT max(price_usd) FROM trades WHERE token_mint = ? AND price_usd IS NOT NULL",
        [mint],
    ).fetchone()
    return row[0] if row else None


def compute(mint: str) -> dict:
    """Посчитать per-wallet P&L и сводку по токену. Пишет в wallet_pnl + возвращает summary."""
    con = db.connect()
    db.ensure_schema(con)

    peak = _peak_price(con, mint)
    rows = con.execute(
        """
        SELECT wallet, side, base_amount, quote_amount_usd, price_usd, slot, block_time
        FROM trades WHERE token_mint = ?
        ORDER BY slot, block_time
        """,
        [mint],
    ).fetchall()

    states: dict[str, WalletState] = {}
    for wallet, side, qty, usd, price, slot, btime in rows:
        st = states.setdefault(wallet, WalletState())
        qty = qty or 0.0
        usd = usd or 0.0
        if side == "buy":
            st.n_buys += 1
            st.bought_qty += qty
            st.bought_usd += usd
            st.pos_qty += qty
            st.pos_cost += usd
            if st.first_buy_slot is None:
                st.first_buy_slot = slot
                st.first_buy_time = btime
                st.first_buy_price = price
        else:  # sell
            st.n_sells += 1
            st.sold_qty += qty
            st.sold_usd += usd
            st.last_sell_time = btime
            if qty <= 0:
                continue
            if st.pos_qty <= 0:                       # продажа без базиса (не в realized!)
                st.sold_without_basis += qty
                st.unbacked_proceeds += usd
                continue
            avg = st.pos_cost / st.pos_qty
            matched = min(qty, st.pos_qty)
            basis = avg * matched
            proceeds_matched = usd * (matched / qty)
            st.realized += proceeds_matched - basis   # только сматченный round-trip
            st.pos_qty -= matched
            st.pos_cost -= basis
            leftover = qty - matched
            if leftover > 0:                          # продали больше, чем держали
                st.sold_without_basis += leftover
                st.unbacked_proceeds += usd * (leftover / qty)

    # запись результатов
    con.execute("DELETE FROM wallet_pnl WHERE token_mint = ?", [mint])
    out_rows = []
    for wallet, st in states.items():
        roi = (st.realized / st.bought_usd) if st.bought_usd > 0 else None
        hold = None
        if st.first_buy_time and st.last_sell_time:
            hold = (st.last_sell_time - st.first_buy_time).total_seconds()
        entry_frac = (
            st.first_buy_price / peak if (st.first_buy_price and peak) else None
        )
        first_mc = (
            st.first_buy_price * db.FIXED_SUPPLY if st.first_buy_price else None
        )
        out_rows.append(
            (
                mint, wallet, st.n_buys, st.n_sells, st.bought_qty, st.bought_usd,
                st.sold_qty, st.sold_usd, st.realized, roi, st.pos_qty,
                st.first_buy_slot, st.first_buy_time, st.first_buy_price, first_mc,
                st.last_sell_time, hold, entry_frac, st.sold_without_basis,
                st.unbacked_proceeds,
            )
        )
    if out_rows:
        con.executemany(
            "INSERT INTO wallet_pnl VALUES (" + ",".join(["?"] * 20) + ")", out_rows
        )

    summary = con.execute(
        """
        SELECT
            count(*)                                                       AS wallets,
            count(*) FILTER (WHERE n_buys > 0)                             AS buyers,
            count(*) FILTER (WHERE bought_usd > 0 AND realized_pnl_usd > 0) AS winners,
            count(*) FILTER (WHERE n_buys = 0 AND sold_without_basis_qty > 0) AS unbacked_sellers,
            sum(realized_pnl_usd) FILTER (WHERE bought_usd > 0)            AS total_realized,
            sum(bought_usd)                                                AS total_bought,
            sum(unbacked_proceeds_usd)                                     AS total_unbacked
        FROM wallet_pnl WHERE token_mint = ?
        """,
        [mint],
    ).fetchone()
    con.close()

    return {
        "mint": mint,
        "peak_price_usd": peak,
        "peak_mc_usd": (peak * db.FIXED_SUPPLY) if peak else None,
        "wallets": summary[0],
        "buyers": summary[1],
        "winners_roundtrip": summary[2],
        "unbacked_sellers (insider/transfer)": summary[3],
        "total_realized_usd (buyers only)": summary[4],
        "total_bought_usd": summary[5],
        "total_unbacked_usd (excluded)": summary[6],
    }


def _print_top(mint: str, top: int) -> None:
    """Топ genuine round-trip трейдеров (есть DEX-покупки). Инсайдеры/трансферы отсеяны."""
    con = db.connect()
    rows = con.execute(
        """
        SELECT wallet, n_buys, n_sells, round(bought_usd,0) inv,
               round(realized_pnl_usd,0) pnl, round(roi,2) roi,
               round(entry_frac,4) ef, round(hold_seconds/3600,1) hold_h,
               round(unbacked_proceeds_usd,0) unbk
        FROM wallet_pnl WHERE token_mint = ? AND bought_usd > 0
        ORDER BY realized_pnl_usd DESC NULLS LAST
        LIMIT ?
        """,
        [mint, top],
    ).fetchall()
    con.close()
    print(f"\nTOP round-trip traders (bought on DEX):")
    print(f"{'wallet':<46} {'buys':>4} {'sell':>4} {'inv$':>10} {'pnl$':>12} "
          f"{'roi':>7} {'entryF':>7} {'holdH':>6} {'unbk$':>9}")
    for w, nb, ns, inv, pnl, roi, ef, hh, unbk in rows:
        print(f"{w:<46} {nb:>4} {ns:>4} {str(inv):>10} {str(pnl):>12} "
              f"{str(roi):>7} {str(ef):>7} {str(hh):>6} {str(unbk):>9}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Модуль A: realized P&L по токену")
    ap.add_argument("mint", help="base58 mint токена")
    ap.add_argument("--since", default=None,
                    help="YYYY-MM-DD, нижняя граница выборки (по умолч. 120 дней назад)")
    ap.add_argument("--force", action="store_true", help="перезабрать из Dune, игнор кэша")
    ap.add_argument("--top", type=int, default=30, help="сколько топ-кошельков показать")
    args = ap.parse_args()

    since = args.since or (datetime.utcnow() - timedelta(days=120)).strftime("%Y-%m-%d")
    dune_fetch.fetch_token_trades(args.mint, since, force=args.force)
    summary = compute(args.mint)

    print("\n=== TOKEN SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k:<20} {v}")
    _print_top(args.mint, args.top)


if __name__ == "__main__":
    main()
