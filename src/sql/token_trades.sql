-- Модуль A: все сделки одного токена по всем DEX-площадкам Solana
-- (включая бондинг-кривую pump.fun через project='pumpdotfun' и пост-грэдуэйшн 'pumpswap').
-- Плейсхолдеры: {{mint}} — base58 mint (валидируется в коде), {{since}} — 'YYYY-MM-DD'.
SELECT
    block_slot                      AS slot,
    block_time,
    tx_id,
    trader_id                       AS wallet,
    project,
    token_bought_mint_address       AS bought_mint,
    token_sold_mint_address         AS sold_mint,
    token_bought_amount             AS bought_amount,
    token_sold_amount               AS sold_amount,
    amount_usd
FROM dex_solana.trades
WHERE block_time >= TIMESTAMP '{{since}}'
  AND ( token_bought_mint_address = '{{mint}}'
     OR token_sold_mint_address  = '{{mint}}' )
ORDER BY block_slot, block_time
