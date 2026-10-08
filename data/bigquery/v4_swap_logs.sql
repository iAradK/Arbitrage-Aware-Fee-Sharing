-- Uniswap v4 Swap logs of the three studied pools with their transaction receipts (Google BigQuery, public dataset).
-- Save the result as CSV under data/data/bq/ and decode it with:  python data/decode_bq_swaps.py <file>.csv
-- The study's export covers 2025-07-01 to 2026-07-31 (1,025,725 logs).
--
-- n_swap_logs counts every PoolManager Swap log in the transaction, on any pool; it is used to tell single-swap
-- transactions from multi-leg routes. Row order is whatever BigQuery returns; nothing downstream depends on it.
DECLARE start_ts TIMESTAMP DEFAULT TIMESTAMP '2025-07-01 00:00:00 UTC';
DECLARE end_ts   TIMESTAMP DEFAULT TIMESTAMP '2026-08-01 00:00:00 UTC';   -- exclusive

WITH swaps AS (
  SELECT transaction_hash, block_number, log_index, topics[SAFE_OFFSET(1)] AS pool_id, data
  FROM `bigquery-public-data.crypto_ethereum.logs`
  WHERE block_timestamp >= start_ts AND block_timestamp < end_ts
    AND address = '0x000000000004444c5dc75cb358380d2e3de08a90'                                   -- PoolManager
    -- keccak256("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
    AND topics[SAFE_OFFSET(0)] = '0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'
),
per_tx AS (
  SELECT transaction_hash, COUNT(*) AS n_swap_logs FROM swaps GROUP BY transaction_hash
)
SELECT s.transaction_hash, s.block_number, s.log_index, s.pool_id, s.data, p.n_swap_logs,
       t.from_address, t.to_address, t.receipt_gas_used, t.receipt_effective_gas_price, t.max_priority_fee_per_gas,
       b.base_fee_per_gas, b.miner
FROM swaps s
JOIN per_tx p USING (transaction_hash)
JOIN `bigquery-public-data.crypto_ethereum.transactions` t
  ON t.hash = s.transaction_hash AND t.block_timestamp >= start_ts AND t.block_timestamp < end_ts
JOIN `bigquery-public-data.crypto_ethereum.blocks` b
  ON b.number = s.block_number AND b.timestamp >= start_ts AND b.timestamp < end_ts
WHERE s.pool_id IN (
  '0x21c67e77068de97969ba93d4aab21826d33ca12bb9f565d8496e8fda8a82ca27',   -- ETH/USDC 0.05%
  '0x54c72c46df32f2cc455e84e41e191b26ed73a29452cdd3d82f511097af9f427e',   -- ETH/WBTC 0.30%
  '0x1d5b2949ece8754c2d736991c62c5162bd144f497b2212182401b9bae77e2d76'    -- ETH/wstETH 0.01%
);
