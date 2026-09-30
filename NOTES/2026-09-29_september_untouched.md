# September 2026 data: untouched? (checked 2026-09-29)

Purpose: decide whether a September 2026 confirmation run (plan item 2.3) is a clean
out-of-sample test. The study window is 2025-09-01 to 2026-09-01 (exclusive), set in
`common/pools.py` (`STUDY_END`).

## What was checked

1. Every raw data file under `data/`: the last timestamp or block and the number of rows at or
   after 2026-09-01 00:00 UTC.
2. The derived caches (`cache/aligned/*.parquet`, `cache/block_gas.parquet`).
3. Every code path that reads the raw files, for clipping to the study window.
4. The audit, results and decision logs (`audit/`, `results/`) for any September statistic.
5. The git history of `data/` and `audit/`.

The check script is `sept_check.py` (kept in the session scratchpad; its output is summarized
below).

## Findings

| Source | On disk | September rows |
|---|---|---|
| Uniswap v4 swaps, 4 pools (`data/swaps_data/swaps_*.csv`) | last swap 2026-08-31 (14:41 to 23:55 UTC) | 0 |
| Gas: BigQuery blocks export | last block 2026-08-31 23:59:59 | 0 |
| Gas: `fee_history_head.csv` (tips) | last block 25,656,292 (2026-07-31) | 0 |
| `cache/block_gas.parquet` | last block 25,878,704 (2026-08-31) | 0 |
| Pyth on-chain events | last event 2026-08-17 16:58:35 | 0 |
| Lido wstETH rate events | last event 2026-08-31 12:22:35 | 0 |
| Aligned per-pool caches | last row 2026-08-31 | 0 |
| **Binance 1m, all 5 symbols** (`data/data/binance/*_1m.csv.gz`, plus daily zips in `raw/`) | **last minute 2026-09-22 23:59** | **31,680 per symbol (Sep 1 to 22)** |

The Binance September minutes were downloaded on 2026-09-24 and committed in `477bc3a`
("updated experiemnts data gathering scripts"). They were read in two places:

- `common/data_io.load_klines`, which every experiment uses, clips to `< STUDY_END`. No
  experiment, table or figure uses a September price.
- `audit/offset_and_candidates.py` reads the files without clipping but joins them backward
  (`merge_asof`) onto swaps that end on 2026-08-31. No September price enters its output.
- `audit/audit_data_readiness.py` (Phase 0 audit, 2026-09-24) computed whole-file quality
  counts that include the September rows: total rows (557,280), duplicate, NaN and non-positive
  rows, zero-volume minutes (108, of which 5 in September), the close range (1,511.69 to 4,764.80,
  which September does not affect, as September closes lie between 2,359.30 and 2,804.82) and,
  for USDC/USDT, the number of distinct closes. `audit/data_readiness_report.md` notes that the
  September zips exist and are outside the window.

No deviation, offset, opportunity, surplus, mechanism or gas statistic was computed on
September data. No September pool state exists anywhere on disk.

## Conclusion

September 2026 is untouched for analysis. The only contact is the Phase 0 data-quality counts
on Binance prices above, which contain no information about pool deviations or mechanism
outcomes. This should be disclosed in one sentence if 2.3 is run.

A September confirmation run (2.3) is possible but needs new data first:
swaps for the four pools, block base fees and tips (fee history), Lido rate events and
Binance minutes for Sep 23 to 30 (or a shorter confirmation window ending Sep 22). The Pyth
scenario cannot be extended, since the on-chain Pyth download already stops on 2026-08-17.
The frozen configurations (E2 `1e8527ba2d0c`, E4 `6026852cf005`) would be applied unchanged.
