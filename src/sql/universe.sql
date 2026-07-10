-- Модуль B: когорта токенов pump.fun по окну ЗАПУСКА.
-- Берём токены, чей первый в истории трейд попал в [launch_start, launch_end];
-- scan_start сдвинут раньше, чтобы токены, торговавшиеся ДО окна, отсеклись по first_seen.
-- Пик MC считаем и по max, и по p99 цены (p99 — робастно к дуст-выбросам).
-- mint = сторона-мемкоин (не SOL/USDC/USDT); price_usd = amount_usd / кол-во мемкоина.
-- Плейсхолдеры: {{scan_start}}, {{scan_end}}, {{launch_start}}, {{launch_end}}, {{min_trades}}.
WITH base AS (
    SELECT
        block_time,
        amount_usd,
        trader_id,
        CASE WHEN token_bought_mint_address NOT IN (
                 'So11111111111111111111111111111111111111112',
                 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
                 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB')
             THEN token_bought_mint_address ELSE token_sold_mint_address END AS mint,
        CASE WHEN token_bought_mint_address NOT IN (
                 'So11111111111111111111111111111111111111112',
                 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
                 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB')
             THEN token_bought_amount ELSE token_sold_amount END AS mc_amount
    FROM dex_solana.trades
    WHERE project IN ('pumpdotfun', 'pumpswap')
      AND block_time BETWEEN TIMESTAMP '{{scan_start}}' AND TIMESTAMP '{{scan_end}}'
      AND amount_usd > 10
),
priced AS (
    SELECT mint, block_time, amount_usd, trader_id,
           amount_usd / mc_amount AS price_usd
    FROM base
    WHERE mc_amount > 0
)
SELECT
    mint,
    min(block_time)                            AS first_seen,
    max(block_time)                            AS last_seen,
    count(*)                                   AS n_trades,
    count(DISTINCT trader_id)                  AS n_traders,
    round(sum(amount_usd))                     AS usd_vol,
    round(max(price_usd) * 1e9)                AS peak_mc_max,
    round(approx_percentile(price_usd, 0.99) * 1e9) AS peak_mc_p99
FROM priced
GROUP BY mint
HAVING min(block_time) BETWEEN TIMESTAMP '{{launch_start}}' AND TIMESTAMP '{{launch_end}}'
   AND count(*) >= {{min_trades}}
ORDER BY peak_mc_p99 DESC
