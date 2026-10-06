# BigQuery cross-check of the swap, gas and Pyth data (2026-10-01)

Purpose: extend the study window to 2025-07-01 and validate the subgraph data against the public Ethereum
dataset on Google BigQuery (`bigquery-public-data.crypto_ethereum`). Raw exports are in `data/data/bq/`
(gitignored). The decoded swap-event table is `data/data/bq/swap_logs_decoded.parquet`
(one row per v4 `Swap` log of the three pools, 2025-07-01 to 2026-07-31, with the transaction receipt fields).

## Files

| Export | Query | Rows |
|---|---|---|
| `bq-results-20261001-153524-*.csv` (copied to `data/data/gas/`) | `blocks`: number, timestamp, base fee, 2025-07-01 to 2025-08-31 | 443,892 blocks, contiguous |
| `bq-results-20261001-153804-*.csv` | `logs` (PoolManager `Swap`, topic `0x40e9cecb...`) joined to `transactions` and `blocks`, 2025-07-01 to 2026-07-31 | 1,025,725 swap logs |
| `bq-results-20261001-154655-*.csv` (copied to `data/data/pyth/bq_pyth_eth_usd_logs_2025-07_2026-07.csv`) | `logs` (Pyth `PriceFeedUpdate`, ETH/USD feed), 2025-07-01 to 2026-07-31 | 33,348 events |

## New data downloaded with the repository scripts (credentials in the gitignored `data/.secrets.env`)

| Source | Script | Result |
|---|---|---|
| Swaps, July and August 2025, three pools | `data/fetch_swaps.py --start 2025-07-01 --end 2025-09-01 --pools ...` | ETH/USDC 215,067, ETH/WBTC 11,603, ETH/wstETH 5,859 swaps; merged files now start 2025-07-01 |
| Tips, July and August 2025 | `data/download_fee_history.py` -> `data/data/gas/fee_history_2025_07_08.csv` | 443,892 blocks; `load_fee_history` concatenates all `fee_history*.csv` |
| Lido rate events | `data/download_wsteth_rate.py --start 2025-07-01 --end 2026-09-01` | 432 events (62 new); the 370 old rows are identical; old file in `archive/data_superseded/` |
| Binance 1m, five symbols | `data/download_binance_klines.py --start 2025-07-01` | 0 missing minutes from 2025-07-01 |

`common/data_io.load_blocks` now concatenates every `bq-results-*.csv` and asserts that overlapping blocks agree.
USDC/USDT was not extended (excluded from the paper, DECISIONS W8).

## Findings

1. **The subgraph swap data is complete and exact.** Monthly swap counts from BigQuery equal the subgraph counts
   in every pool and month from 2025-07 to 2026-07, except one extra event in ETH/USDC in 2026-03 (59,060 vs 59,059).
   Joining on (transaction hash, log index), `sqrtPriceX96` and the transaction gas price are identical for 100% of
   rows. The amounts have opposite signs (the event reports the pool's balance delta, the subgraph the user's).
   The exactly 80,000 ETH/USDC swaps of September 2025 are genuine (continuous hourly counts, BigQuery agrees).

2. **Pyth export is complete.** Monthly `PriceFeedUpdate` counts from BigQuery equal the RPC export for every
   month from 2025-09 to 2026-07 (July 2026: 558). The July thinning is real, not a download gap. BigQuery adds
   659 events in 2025-07 and 757 in 2025-08.

3. **A protocol fee was switched on 2026-07-27 at about 09:15 UTC in all three pools.** The `fee` field of the
   Swap event (the fee the swapper pays) changed from 500 to 625 pips in ETH/USDC, 3000 to 3499 in ETH/WBTC and
   100 to 125 in ETH/wstETH. With v4's `swapFee = protocolFee + lpFee - protocolFee * lpFee / 1e6`, this is a protocol
   fee of 125, 500 and 25 pips (0.0125%, 0.05% and 0.0025%). The LP fee is unchanged. From that day the arbitrager pays
   1.25x (ETH/USDC, ETH/wstETH) and 1.17x (ETH/WBTC) the fee that the simulations assume. (Corrected 2026-10-01; an
   earlier version of this note said 1.25x for all three pools.) Affected: 0.6% to 1.0%
   of all swaps (the last five days of July 2026, and all of August 2026 in the current paper's test months).
   The per-swap fee is available in the decoded table for a time-varying swap fee.

4. **Gas used by swap transactions (receipts).** 52% of ETH/USDC swaps (57% ETH/WBTC, 45% ETH/wstETH) are the
   only v4 swap in their transaction ("single-leg"). Gas used, single-leg, P10/P25/P50/P75/P90:
   ETH/USDC 136k/145k/156k/181k/402k; ETH/WBTC 127k/134k/139k/147k/277k; ETH/wstETH 146k/243k/392k/566k/826k.
   Multi-leg medians: 680k, 1.80M, 497k. The 150,000-gas assumption sits at the single-leg median of ETH/USDC
   and ETH/WBTC, and the E5 sweep (100k to 300k) covers their P10 to P90.

5. **Tips actually paid** (effective gas price minus base fee), single-leg swaps: median 0.01 gwei in ETH/USDC
   with 38% paying a zero tip (45% in ETH/WBTC, 9% in ETH/wstETH). Zero-tip inclusion points to builder payments
   outside the gas price (coinbase transfers), which the `traces` table would quantify (not exported).

6. **E1 depth calibration validated against the measured in-range liquidity.** The Swap event's `liquidity`
   (converted to the token units of E1 by sqrt(10^dec0 * 10^dec1)) matches E1's trailing one-hour depth of the
   same minute with a median log error of 0.0000; within 10% for 91% (ETH/USDC), 92% (ETH/WBTC) and 95%
   (ETH/wstETH) of swaps, within 2% for 80%, 82% and 95%. The error is largest in the validation and test months
   and in the largest size quartile. The measured liquidity changes between consecutive swaps for 13%, 11% and 3%
   of swaps (range crossings or LP actions).

## Open

- Model the swapper's fee per swap (finding 3) or disclose it. Only `fees_tvl_check.py` uses the LP fee income.
- Resolved: the measured ETH/wstETH liquidity does collapse, from about 9,070 to about 400 (weeks of 29 March to 12 April
  2026), and recovers in the second half of April. Monthly per-swap medians hid it. The E1 depth series shows the same.
- Coinbase transfers (`traces`) for the inclusion payments were not exported.
