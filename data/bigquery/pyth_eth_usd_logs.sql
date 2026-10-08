-- Pyth ETH/USD PriceFeedUpdate logs on Ethereum (Google BigQuery, public dataset). Cross-check only: the experiments
-- read the RPC download of data/download_pyth_onchain_events.py; this export was used to confirm it is complete.
-- The study's export covers 2025-07-01 to 2026-07-31 (33,348 events); `data` holds publishTime, price and conf.
DECLARE start_ts TIMESTAMP DEFAULT TIMESTAMP '2025-07-01 00:00:00 UTC';
DECLARE end_ts   TIMESTAMP DEFAULT TIMESTAMP '2026-08-01 00:00:00 UTC';   -- exclusive

SELECT transaction_hash, block_number, block_timestamp, log_index, data
FROM `bigquery-public-data.crypto_ethereum.logs`
WHERE block_timestamp >= start_ts AND block_timestamp < end_ts
  AND address = '0x4305fb66699c3b2702d4d05cf36551390a4c69c6'                                     -- Pyth
  -- keccak256("PriceFeedUpdate(bytes32,uint64,int64,uint64)")
  AND topics[SAFE_OFFSET(0)] = '0xd06a6b7f4918494b3719217d1802786c1f5112a6c1d88fe2cfec00b4584f6aec'
  AND topics[SAFE_OFFSET(1)] = '0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace'   -- ETH/USD feed
ORDER BY block_number, log_index;
