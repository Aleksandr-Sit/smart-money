-- Batch-забор: все сделки НАБОРА токенов одним запросом (экономия кредитов Dune).
-- Универсальный mint определяется на стороне кода (bought или sold ∈ набор).
-- Плейсхолдеры: {{since}} — 'YYYY-MM-DD', {{mints}} — 'm1','m2',... (валидируются в коде).
SELECT
    block_slot                  AS slot,
    block_time,
    tx_id,
    trader_id                   AS wallet,
    project,
    token_bought_mint_address   AS bought_mint,
    token_sold_mint_address     AS sold_mint,
    token_bought_amount         AS bought_amount,
    token_sold_amount           AS sold_amount,
    amount_usd
FROM dex_solana.trades
WHERE block_time >= TIMESTAMP '{{since}}'
  AND ( token_bought_mint_address IN ({{mints}})
     OR token_sold_mint_address  IN ({{mints}}) )
ORDER BY block_slot, block_time
