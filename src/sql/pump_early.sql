-- Ранние покупатели ОДНОГО pump.fun токена в узком окне запуска (агрегация в Dune).
-- Узкое литеральное окно [{{ws}}, {{we}}] = мало партиций block_time = дёшево по кредитам.
-- Возвращает компактные per-wallet агрегаты (не сырьё) → обход лимита выгрузки free-тарифа.
-- Плейсхолдеры: {{mint}}, {{ws}}/{{we}} 'YYYY-MM-DD', {{min_bought}} (напр. 50).
WITH tr AS (
    SELECT
        trader_id AS wallet,
        block_time, block_slot,
        CASE WHEN token_bought_mint_address = '{{mint}}' THEN 'buy' ELSE 'sell' END AS side,
        CASE WHEN token_bought_mint_address = '{{mint}}'
             THEN token_bought_amount ELSE token_sold_amount END AS qty,
        amount_usd AS usd,
        CASE WHEN token_bought_mint_address = '{{mint}}'
             THEN amount_usd / nullif(token_bought_amount, 0)
             ELSE amount_usd / nullif(token_sold_amount, 0) END AS price
    FROM dex_solana.trades
    WHERE block_time >= TIMESTAMP '{{ws}}' AND block_time < TIMESTAMP '{{we}}'
      AND ( token_bought_mint_address = '{{mint}}'
         OR token_sold_mint_address  = '{{mint}}' )
),
fb AS (
    SELECT wallet,
           min(block_time)                AS first_buy_time,
           min_by(price, block_time)      AS first_price,
           min_by(block_slot, block_time) AS first_slot
    FROM tr WHERE side = 'buy' AND usd > 0 AND qty > 0 GROUP BY 1
),
ag AS (
    SELECT wallet,
        count(*) FILTER (WHERE side = 'buy')                 AS n_buys,
        count(*) FILTER (WHERE side = 'sell')                AS n_sells,
        coalesce(sum(qty) FILTER (WHERE side = 'buy'),  0)   AS bought_qty,
        coalesce(sum(usd) FILTER (WHERE side = 'buy'),  0)   AS bought_usd,
        coalesce(sum(qty) FILTER (WHERE side = 'sell'), 0)   AS sold_qty,
        coalesce(sum(usd) FILTER (WHERE side = 'sell'), 0)   AS sold_usd
    FROM tr GROUP BY 1
)
SELECT '{{mint}}' AS mint, ag.wallet, ag.n_buys, ag.n_sells,
       ag.bought_qty, ag.bought_usd, ag.sold_qty, ag.sold_usd,
       fb.first_buy_time, fb.first_price, fb.first_slot
FROM ag JOIN fb ON fb.wallet = ag.wallet
WHERE ag.bought_usd >= {{min_bought}}
ORDER BY fb.first_slot
