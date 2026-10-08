-- Ethereum blocks: number, timestamp, base fee, gas used and gas limit (Google BigQuery, public dataset).
-- Save the result as CSV to data/data/gas/bq-results-<anything>.csv. common.data_io.load_blocks concatenates every
-- bq-results-*.csv there and checks that overlapping blocks agree. The study used two exports:
--   2025-07-01 to 2025-09-01 and 2025-09-01 to 2026-09-01 (443,892 and 2,614,139 blocks).
DECLARE start_ts TIMESTAMP DEFAULT TIMESTAMP '2025-07-01 00:00:00 UTC';
DECLARE end_ts   TIMESTAMP DEFAULT TIMESTAMP '2026-09-01 00:00:00 UTC';   -- exclusive

SELECT number, timestamp, base_fee_per_gas, gas_used, gas_limit
FROM `bigquery-public-data.crypto_ethereum.blocks`
WHERE timestamp >= start_ts AND timestamp < end_ts
ORDER BY number;
