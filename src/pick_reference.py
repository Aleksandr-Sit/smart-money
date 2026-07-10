"""Подобрать реальные эталонные токены для сверки Модуля A.

Берём недавно грэдуейтнутые pump.fun токены (есть сделки на pumpswap) с высоким
USD-объёмом за последние дни. Ключ читается из .env, не логируется.
Run:  .venv\\Scripts\\python.exe -m src.pick_reference
"""
from __future__ import annotations

from . import dune_fetch

STABLES = (
    "So11111111111111111111111111111111111111112",  # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
)

SQL = f"""
SELECT token_bought_mint_address        AS mint,
       count(*)                         AS buys,
       count(distinct trader_id)        AS traders,
       round(sum(amount_usd))           AS usd_vol,
       min(block_time)                  AS first_seen,
       max(block_time)                  AS last_seen
FROM dex_solana.trades
WHERE block_time > now() - interval '3' day
  AND project = 'pumpswap'
  AND token_bought_mint_address NOT IN ('{STABLES[0]}','{STABLES[1]}','{STABLES[2]}')
GROUP BY 1
HAVING count(*) BETWEEN 2000 AND 40000   -- реальный, но не гигант (дешевле тянуть в Модуле A)
ORDER BY usd_vol DESC
LIMIT 15
"""


def main() -> None:
    rows = dune_fetch.run_sql(SQL)
    print(f"Найдено кандидатов: {len(rows)}\n")
    print(f"{'mint':<46} {'buys':>7} {'traders':>8} {'usd_vol':>14}  first_seen")
    for r in rows:
        print(f"{r['mint']:<46} {r['buys']:>7} {r['traders']:>8} "
              f"{str(r['usd_vol']):>14}  {r['first_seen']}")


if __name__ == "__main__":
    main()
