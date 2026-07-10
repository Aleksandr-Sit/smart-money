-- Модуль C (агрегация в Dune): per-(токен, кошелёк) сводка ТОЛЬКО по ранним покупателям.
-- Ранний = первый вход при АБСОЛЮТНОЙ цене <= {{early_price}} (= EARLY_ABS_MC / 1e9).
-- Симметрично для pumped и dead (нет look-ahead: пик не нужен) → честный знаменатель.
-- Возвращает компактный результат (обход лимита выгрузки free-тарифа): агрегаты, не сырьё.
-- Плейсхолдеры: {{mints}} — 'm1','m2',...; {{since}} — 'YYYY-MM-DD';
--               {{early_price}} — порог цены раннего входа; {{min_bought}} — напр. 10.
WITH tr AS (
    SELECT
        CASE WHEN token_bought_mint_address IN ({{mints}})
             THEN token_bought_mint_address ELSE token_sold_mint_address END AS mint,
        trader_id AS wallet,
        block_time,
        block_slot,
        CASE WHEN token_bought_mint_address IN ({{mints}}) THEN 'buy' ELSE 'sell' END AS side,
        CASE WHEN token_bought_mint_address IN ({{mints}})
             THEN token_bought_amount ELSE token_sold_amount END AS qty,
        amount_usd AS usd,
        CASE WHEN token_bought_mint_address IN ({{mints}})
             THEN amount_usd / nullif(token_bought_amount, 0)
             ELSE amount_usd / nullif(token_sold_amount, 0) END AS price
    FROM dex_solana.trades
    WHERE block_time >= TIMESTAMP '{{since}}'
      AND ( token_bought_mint_address IN ({{mints}})
         OR token_sold_mint_address  IN ({{mints}}) )
),
fb AS (   -- первый вход кошелька по токену
    SELECT mint, wallet,
           min_by(price, block_time)      AS first_buy_price,
           min(block_time)                AS first_buy_time,
           min_by(block_slot, block_time) AS first_buy_slot
    FROM tr
    WHERE side = 'buy' AND usd > 0 AND qty > 0
    GROUP BY 1, 2
),
ag AS (   -- полные агрегаты кошелька по токену (весь round-trip)
    SELECT mint, wallet,
        count(*) FILTER (WHERE side = 'buy')                 AS n_buys,
        count(*) FILTER (WHERE side = 'sell')                AS n_sells,
        coalesce(sum(qty) FILTER (WHERE side = 'buy'),  0)   AS bought_qty,
        coalesce(sum(usd) FILTER (WHERE side = 'buy'),  0)   AS bought_usd,
        coalesce(sum(qty) FILTER (WHERE side = 'sell'), 0)   AS sold_qty,
        coalesce(sum(usd) FILTER (WHERE side = 'sell'), 0)   AS sold_usd,
        max(block_time)  FILTER (WHERE side = 'sell')        AS last_sell_time
    FROM tr
    GROUP BY 1, 2
)
SELECT ag.mint, ag.wallet, ag.n_buys, ag.n_sells,
       ag.bought_qty, ag.bought_usd, ag.sold_qty, ag.sold_usd,
       fb.first_buy_time, fb.first_buy_price, fb.first_buy_slot, ag.last_sell_time
FROM ag
JOIN fb ON fb.mint = ag.mint AND fb.wallet = ag.wallet
WHERE ag.bought_usd >= {{min_bought}}
  AND fb.first_buy_price <= {{early_price}}
