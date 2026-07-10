"""Полный он-чейн след кошелька (объективная проверка «реальный трейдер vs узкий бот»).

Считает по dex_solana.trades: всего сделок, уник. токенов куплено, период активности, объём.
Run:  .venv\\Scripts\\python.exe -m src.wallet_footprint <wallet1> <wallet2> ...
"""
from __future__ import annotations

import sys

from . import dune_fetch

WSOL = "So11111111111111111111111111111111111111112"


def main() -> None:
    wallets = sys.argv[1:]
    if not wallets:
        sys.exit("usage: -m src.wallet_footprint <wallet> [<wallet> ...]")
    wl = ",".join("'" + w + "'" for w in wallets)
    sql = f"""
    SELECT trader_id,
        count(*)                                                             AS n_trades,
        count(DISTINCT CASE WHEN token_bought_mint_address <> '{WSOL}'
             THEN token_bought_mint_address END)                            AS n_tokens_bought,
        min(block_time)                                                      AS first_trade,
        max(block_time)                                                      AS last_trade,
        round(sum(amount_usd))                                               AS vol_usd
    FROM dex_solana.trades
    WHERE trader_id IN ({wl})
      AND block_time > now() - interval '80' day
    GROUP BY 1
    ORDER BY n_trades DESC
    """
    print(f"{'wallet':<46} {'trades':>8} {'tokens':>7} {'vol_usd':>14}  active")
    for r in dune_fetch.run_sql(sql):
        print(f"{r['trader_id']:<46} {r['n_trades']:>8} {r['n_tokens_bought']:>7} "
              f"{str(r['vol_usd']):>14}  {str(r['first_trade'])[:10]}..{str(r['last_trade'])[:10]}")


if __name__ == "__main__":
    main()
