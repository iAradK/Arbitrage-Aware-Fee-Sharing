# Data readiness report

Audit date: 2026-09-24. Everything below comes from `audit/audit_data_readiness.py` (raw numbers in `audit/audit_results.json`) or from directly listing and opening files. Nothing under `data/` was modified, and no network calls were made. No fetch command was run.

## 0. Layout found (differs from the brief)

The brief expects `binance/`, `pyth/`, `gas/`, `lido/` directly under `data/`. They are actually under **`data/data/`**. The swap logs are in `data/swaps_data/`. The data folder holds no pool metadata file, no `ether_gas_4y.csv`, no Coinbase or 1-second klines, and no `pyth_ETH_USD_1m.csv.gz`.

- Swaps: one combined file per pool, `data/swaps_data/swaps_<pool>.csv`, plus `<pool>/parts/*.csv` with `.state.json` files. All 25 parts per pool have `done: true`, and the part row sums equal the combined-file rows for all 4 pools.
- Swap columns: `id, timestamp, logIndex, amount0, amount1, amountUSD, sqrtPriceX96, tick, sender, origin, tx_id, tx_timestamp, tx_blockNumber, tx_gasUsed, tx_gasPrice`. These come from a subgraph (`fetch_swaps.py` queries `transaction { gasUsed gasPrice }`).
  - There is **no post-swap liquidity column**.
  - There is **no fee-charged column**.
  - There is **no pool-id column**; the pool is identified by file name.
  - `id == tx_id + "-" + logIndex` in 100% of rows.
- Timestamps in the swap files are Unix **seconds**. Binance, Pyth and Lido timestamps are ISO strings with a UTC offset.
- Binance daily zips for 2026-09-01..22 sit in `binance/raw/` (34 files per symbol). These are outside the study window.

## 1. Verdict per experiment

| # | Experiment | Verdict | One-line reason |
|---|---|---|---|
| E1 | §5.1.4 CPMM calibration | **READY WITH CAVEATS** | All 4 pools cover 12 months with a complete reference join. `gasUsed` is 0 in every row, and there is no liquidity or fee column. |
| E2 | §5.2.1 RQ1 replay | **READY WITH CAVEATS** | Test swaps (months 11-12) and the reference join are complete. Depth and cost parameters from E1 do not exist yet, and `gasUsed` is unusable for cost parameters. |
| E3 | §5.2.2 RQ2 Pareto | **READY WITH CAVEATS** | No external data needed. Same dependency on E1/E2 output. |
| E4 | §5.3 RQ3 oracle delay and eta | **READY WITH CAVEATS** | The Binance minute series is complete, so lagging works. The optional real-Pyth check is incomplete: events stop on 2026-08-17 and there is nothing after the 2026-08-26 upgrade. |
| E5 | §5.4.1 RQ4 size-dependent costs | **BLOCKED** | `tx_gasUsed` is 0 in 100% of rows in all four swap files. `fee_history.csv` covers only about 1 month (from about 2026-07-31), so 0% of training-period swap blocks are covered. |
| E6 | §5.4.2 fragmentation and reversal | **READY WITH CAVEATS** | Superseded by section 9 (corrected candidate definition). eth_usdc_005 and usdc_usdt_0001 have enough; eth_wbtc_030 only at the loose 1x-fee threshold (4 at 2x); eth_wsteth_001 has 346 with an in-sample offset. The section 4 counts used the post-swap definition and are inflated. |
| E7 | §5.5 RQ5 Solidity, gas, S_min | **BLOCKED** | `ParticipationAwareHook.sol` is not in the working tree (only a compiled artifact remains), and `forge` is not installed or on PATH. There is no full-year per-block gas series. |

Numbering inconsistency: your table calls E1 "§5.1.4" and E2 "§5.2.1", while the brief says the table also calls E2 "§5.1.3". The repo README maps sections differently again (§5.1 synthetic frontier, §5.2.1 fixed-candidate replay, §5.2.2 LP outcomes, §5.3 staleness, §5.4 Solidity parity and fragmentation). Pick one scheme before writing the paper.

## 2. Inventory

Study window is 2025-09-01 to 2026-08-31.

| Item | Req. | File found | Rows | Date range | Status | Evidence |
|---|---|---|---|---|---|---|
| Binance ETHUSDC 1m | req | `data/data/binance/ETHUSDC_1m.csv.gz` | 557,280 | 2025-09-01 00:00 to 2026-09-22 23:59 | OK | 525,600 of 525,600 expected minutes in study; 0 missing (0 of 89,280 in months 11-12); 0 duplicate timestamps; 0 non-positive prices; 108 zero-volume minutes; close range 1511.69-4764.8. |
| Binance ETHBTC 1m | req | `.../ETHBTC_1m.csv.gz` | 557,280 | same | OK | 0 missing, 0 duplicates. 63,271 zero-volume minutes (about 11%). On those minutes `open == close` in 100% of rows (last price carried forward). |
| Binance USDCUSDT 1m | req | `.../USDCUSDT_1m.csv.gz` | 557,280 | same | OK / WARN | 0 missing, 0 duplicates, 0 zero-volume. **Only 289 distinct close values** (173 in months 11-12). Top closes: 1.0 (29,201 minutes), 0.9999 (28,745), 0.9998 (26,869). Range 0.9947-1.0023. Resolution is 1 bp, which equals the pool fee. |
| Binance ETHUSDT / BTCUSDT (optional) | opt | present | 557,280 each | same | OK | 0 missing. |
| Longest Binance gaps | | | | | OK | No gaps at all in the study window. The "5 longest gaps" are all 1 minute, i.e. the normal step. |
| Pyth on-chain events | req | `data/data/pyth/onchain_ETH_USD_events.csv.gz` | 31,962 | 2025-09-01 05:15 to **2026-08-17 16:58** | **WARN** | See below. |
| Pyth Benchmarks 1m (optional) | opt | absent | | | MISSING (fine) | |
| Fee history | req | `data/data/gas/fee_history.csv` | 222,412 | blocks 25,656,293 to 25,878,704 (about 2026-07-31 23:41 to 2026-09-01) | **WARN (severe)** | No holes, no duplicates, sorted. Expected about 2.6M blocks from 23,264,566; only 8.5% present. Base fee 0.024 / median 0.086 / max 10.99 gwei. Zero-tip share: 0.73% (p50), 17.2% (p10). |
| Lido wstETH rate | req | `data/data/lido/wsteth_rate_events.csv` | 370 | 2025-08-27 12:23 to 2026-08-31 12:22 | OK | First row is at or before 2025-09-01 and last row is at or after 2026-08-30. Median gap 1 day, max 1 d 4 h 52 m. 0 decreases. `eth_per_wsteth` runs 1.2121-1.2430. `wsteth_per_eth` is the exact inverse (max error 2e-16). |
| Legacy daily gas | opt | `contracts/data/ether_gas_4y.csv` (not in `data/`) | 1,037 | 2023-04-01 to 2026-01-31 | OK (coarse) | Columns: `date, avg_gas_price_gwei, median_gas_price_gwei, p95_gas_price_gwei, tx_count`. Ends before the study window. |
| Swaps eth_usdc_005 | req | `swaps_data/swaps_eth_usdc_005.csv` | 749,115 | 2025-09-01 00:01 to 2026-08-31 23:54 | WARN | 0 duplicates on (tx_id, logIndex). `gasUsed` = 0 in 100% of rows. `gasPrice` never 0 (median 0.59 gwei). Blocks 23,264,574 to 25,878,676. |
| Swaps usdc_usdt_0001 | req | `swaps_usdc_usdt_0001.csv` | 619,397 | 2025-09-01 00:00 to 2026-08-31 23:55 | WARN | Same pattern. Blocks 23,264,567 to 25,878,684. |
| Swaps eth_wbtc_030 | req | `swaps_eth_wbtc_030.csv` | 44,476 | 2025-09-01 00:04 to 2026-08-31 14:41 | WARN | Same pattern. Largest swap gap 1 d 21 h 47 m (after 2026-08-17 01:01). |
| Swaps eth_wsteth_001 | req | `swaps_eth_wsteth_001.csv` | 45,766 | 2025-09-01 00:26 to 2026-08-31 23:23 | WARN | Same pattern. Largest swap gap 17 h 45 m (2026-04-14). |
| Pool metadata / config | strongly wanted | none in `data/` | | | MISSING | Pool IDs, currency order, fee and decimals exist only in the brief. |
| Post-swap liquidity, fee charged | strongly wanted | not in swap files | | | MISSING | Neither column exists. |
| Coinbase / Binance 1s (optional) | opt | absent | | | MISSING (fine) | |
| Historical ETH/USD for E7 | | `contracts/data/eth_usd_5y.csv` (not in `data/`) | | daily, 2021-04-16 to 2026-04-16 | WARN | Ends before the study window. Binance ETHUSDC 1m covers the full year and can replace it. |

### Pyth detail

- 31,962 events, versus the roughly 7,000-8,000 you expected (about 90 per day over the 351 days covered, rather than 20). Either the source includes more updaters than assumed or the expectation is off.
- Per-month event counts are very uneven:

| Month | Events |
|---|---|
| 2025-09 | 522 |
| 2025-10 | 372 |
| 2025-11 | 536 |
| 2025-12 | 572 |
| 2026-01 | 890 |
| 2026-02 | 3,857 |
| 2026-03 | 9,144 |
| 2026-04 | 5,644 |
| 2026-05 | 1,598 |
| 2026-06 | 8,239 |
| 2026-07 | 558 |
| **2026-08** | **30** |

- Every month has events. Month 12 (2026-08) has 30 events and none after 2026-08-17.
- `staleness_s` (median / P90 / P99):

| Month | Median | P90 | P99 |
|---|---|---|---|
| 2025-09 | 21.5 | 48 | 141 |
| 2025-11 | 12 | 36 | 343 |
| 2026-03 | 8 | 20 | 57 |
| 2026-05 | 12 | 32 | 937 |
| 2026-07 | 11 | 27 | 87 |
| 2026-08 | 26 | 40 | 44 |

  The other months are in `audit_results.json`.
- Largest gaps between updates: 3 d 05:47 (ending 2026-08-05), 2 d 01:21 (ending 2026-08-09), 1 d 20:25 (ending 2026-08-07), 1 d 13:10 (ending 2026-08-01), 1 d 02:20 (ending 2026-07-31).
- Before / after 2026-08-26: **all 31,962 events are before the upgrade, 0 are after.** Pre-upgrade staleness is median 9 s, P90 23 s, P99 81 s, max 3,100 s. The post-upgrade regime cannot be reported.
- The August sparsity (gaps of days) looks like an incomplete download or an outage, not real Pyth behaviour. I cannot tell which from the file alone. Note that eth_wbtc_030 also has a 1 d 21 h swap gap starting 2026-08-17.

## 3. Swap coverage matrix (swaps per pool-month)

Splits: train = 2025-09 to 2026-04, val = 2026-05 and 2026-06, test = 2026-07 and 2026-08.

| Month | Split | eth_usdc_005 | usdc_usdt_0001 | eth_wbtc_030 | eth_wsteth_001 |
|---|---|---|---|---|---|
| 2025-09 | train | 80,000 | 71,211 | 3,378 | 3,259 |
| 2025-10 | train | 86,418 | 87,952 | 5,600 | 4,747 |
| 2025-11 | train | 90,475 | 78,386 | 6,562 | 3,774 |
| 2025-12 | train | 73,641 | 51,449 | 3,910 | 4,270 |
| 2026-01 | train | 54,917 | 48,976 | 3,468 | 2,884 |
| 2026-02 | train | 75,524 | 56,218 | 5,448 | 6,062 |
| 2026-03 | train | 59,059 | 44,583 | 3,991 | 3,222 |
| 2026-04 | train | 54,530 | 44,925 | 2,148 | 4,097 |
| 2026-05 | val | 43,557 | 38,409 | 1,423 | 2,671 |
| 2026-06 | val | 48,392 | 35,604 | 3,625 | 4,973 |
| 2026-07 | test | 40,884 | 26,131 | 2,634 | 3,652 |
| 2026-08 | test | 41,718 | 35,553 | 2,289 | 2,155 |
| **Total** | | 749,115 | 619,397 | 44,476 | 45,766 |

No month is empty. The thinnest cells are eth_wbtc_030 2026-05 (1,423 swaps) and eth_wsteth_001 2026-08 (2,155). The eth_usdc_005 count for 2025-09 is exactly 80,000, made of two parts (40,808 + 39,192). It is probably coincidence, since neither part is near the 1,000-row page size, but I cannot confirm it from the file.

Test-split counts: eth_usdc_005 82,602; usdc_usdt_0001 61,684; eth_wbtc_030 4,923; eth_wsteth_001 5,807. E2 trains and evaluates on those.

## 4. Level 2 checks (orientation, joins, E6)

Join rule: each swap is matched to the last **completed** minute bar. A bar opened at T is only available at T+60 s, so nothing from the bar containing the swap is used. Tolerance is 5 minutes for Binance and 3 days for the Lido rate. Reference age is median 35 s and P99 59 s for Binance pools, and median 11.0 h and P99 23.8 h for wstETH (daily step series).

Orientation and decimals. Samples are one week in month 10 (2026-06-15 to 06-22) and one week in month 12 (2026-08-10 to 08-17). Deviation is |log(pool price / reference)| with the dec0/dec1 values given in the brief.

| Pool | Median \|dev\| m10 / m12 | In fee tiers | Signed median m10 / m12 | Inverted-reference median \|dev\| | Verdict |
|---|---|---|---|---|---|
| eth_usdc_005 | 3.48 bp / 3.32 bp | 0.70x / 0.66x | -0.2 bp / -0.4 bp | 14.9 | Orientation and decimals correct |
| usdc_usdt_0001 | 0.19 bp / 0.16 bp | 0.19x / 0.16x | 0.0 bp / -0.02 bp | 0.14% | Correct |
| eth_wbtc_030 | 17.6 bp / 31.8 bp | 0.59x / 1.06x | +13.0 bp / -31.8 bp | 7.2 | Correct orientation; see WBTC note |
| eth_wsteth_001 | 2.04 bp / 1.25 bp | 2.0x / 1.25x | +2.0 bp / +1.25 bp | 0.43 (about 2·log price) | Correct (pool price is wstETH per ETH, compared with `wsteth_per_eth`) |

- In no case is the price inverted or off by a power of 10. As an independent check, `|amount1/amount0|` divided by the price implied by sqrtPriceX96 has median 1.0000-1.0034 in every pool and sample. This confirms currency0 is ETH (or USDC for the stable pool) and that the decimals in the brief are right.
- WBTC note: the eth_wbtc_030 median deviation moves from +13 bp (June) to -32 bp (August) and is one-signed within each week. This is consistent with WBTC not equalling BTC (a depeg or basis) or with a stale pool. It is a reference-quality issue for that pool, not an orientation issue.
- The wstETH signed deviation is positive in both weeks (the pool is above the contract rate), which is consistent with the stETH basis you flagged.

Join coverage (fraction of swaps that find a reference / fraction of swap blocks that find a `fee_history` row). Blocks are counted per swap row.

| Pool | Reference: train / val / test | Fee history: train / val / test |
|---|---|---|
| eth_usdc_005 | 100% / 100% / 100% | **0% / 0% / 50.5%** |
| usdc_usdt_0001 | 99.9998% / 100% / 100% | **0% / 0% / 57.6%** |
| eth_wbtc_030 | 100% / 100% / 100% | **0% / 0% / 46.5%** |
| eth_wsteth_001 | 100% / 100% / 100% | **0% / 0% / 37.1%** |

The fee-history join covers only about half of the test swaps and none of the earlier ones. It covers the second half of month 11 and all of month 12 only.

E6 feasibility (months 11-12). A candidate is a swap whose post-swap price deviates from the reference by more than the pool's fee tier. The surplus proxy is |dev| × `amountUSD`, and quartiles are computed over the candidates.

| Pool | Test swaps | Candidates (> fee) | Share | Per surplus quartile | > 2× fee | 2026-07 / 2026-08 |
|---|---|---|---|---|---|---|
| eth_usdc_005 | 82,602 | 25,360 | 30.7% | 6,340 each | 8,322 | 11,521 / 13,839 |
| usdc_usdt_0001 | 61,684 | 1,007 | 1.6% | 252 / 252 / 251 / 252 | 710 | 405 / 602 |
| eth_wbtc_030 | 4,923 | 1,623 | 33.0% | 406 / 406 / 405 / 406 | 15 | 694 / 929 |
| eth_wsteth_001 | 5,807 | 4,095 | 70.5% | 1,024 / 1,024 / 1,023 / 1,024 | 709 | 2,248 / 1,847 |

Every pool has well over 100 candidates and over 400, so the feasibility threshold is met. The candidate filter is generous, and these counts are not "arbitrage opportunities":

- For eth_wsteth_001, 70% of swaps count. Most of these are the daily-step Lido rate and the stETH basis, not arbitrage.
- For eth_wbtc_030, the count is inflated by the WBTC offset. Only 15 swaps exceed 2× the fee.
- For usdc_usdt_0001, the reference resolves only to 1 bp, which equals the fee.

Pick E6 opportunities with a stricter rule (for example deviation above 2× the fee, with a minimum size).

## 5. Non-data prerequisites

| Item | Finding |
|---|---|
| `foundry.toml` | Present at `contracts/foundry.toml` (solc 0.8.24, cancun, via_ir, optimizer 200 runs). No `hardhat.config.*` found. |
| `forge --version` | **Not runnable.** `forge` is not on PATH and `%USERPROFILE%\.foundry\bin\forge.exe` does not exist. |
| ParticipationAwareHook source | **Not in the working tree.** `contracts/src/` has only `CumulativeSurplusAccounting.sol` and `SurplusSharingAccounting.sol`. Compiled artifacts exist in `contracts/out/ParticipationAwareHook.sol/` and `.t.sol/`, and their metadata target is `src/hooks/ParticipationAwareHook.sol`. `git log --all` shows that path in commit `f5405ca` ("Updated hooks"), so the source is recoverable from history. |
| Test folder | `contracts/test/` has only `CumulativeSurplusAccounting.t.sol` and `SurplusSharingAccounting.t.sol`. There is no `ParticipationAwareHook.t.sol` source, though `contracts/out/` has a compiled `ParticipationAwareHook.t.sol` and `HookGasBenchmark.t.sol`. `lib/forge-std` and `lib/v4-core` are present. |
| Python entry points | `contracts/python/hook_replay.py`, `contracts/python/fragmentation_replay.py`; `contracts/data_prep/*.py`; `experiments/*/run_*.py`; `common/cpmm_replay_common.py`. The existing experiments read older data (Jan-Mar 2026 and 2021-era). I did not find a script that replays the new 4-pool, 12-month files. |
| Saved E1 calibration | **None for the new data.** Only `contracts/results/lp_outcomes_v4_auto_r/swap_r_calibration.csv` and `archive/contracts_results_swap_level_replay/swap_r_calibration.csv`, which come from the older Jan-Mar 2026 replay. |
| Secrets (paths only) | `data/fetch_swaps.py` matched my API-key pattern (git history says the key was removed; check the file). `contracts/lib/v4-core/.env` exists (contents not read). |

## 6. Problems ranked by impact, with fixes

1. **`tx_gasUsed` is 0 in 100% of rows in all four swap files.** This blocks E5 (cost coefficients c0, c1, c2), the gas parts of E1/E2 costs, and the E7 gas distribution. The subgraph field is not populated. Fix: fetch receipts (`eth_getTransactionReceipt`) for the swap transactions, or for a sample stratified by month and pool. **No script for this exists in `data/`**, and it needs a new one (batch `eth_getTransactionReceipt`, one row per unique `tx_id`, resumable). `tx_gasPrice` is populated and can stay.
2. **`fee_history.csv` covers only about 2026-07-31 to 2026-09-01 (222,412 of about 2.6M blocks).** Blocks before 25,656,293 are missing, so the training and validation periods have no per-block gas price. This blocks E5's per-block price and E7's g_t. Fix (do not run without your say):
   ```
   python download_fee_history.py --start 2025-09-01 --end 2026-09-01 --workers 4
   ```
   The script resumes, so it should continue from the existing file. Set `ETH_RPC_URL` in the environment first. Alternative for E5 only: use the per-swap `tx_gasPrice`.
3. **`ParticipationAwareHook.sol` and its test are missing from the working tree**, and `forge` is not installed. This blocks E7. Fix: restore the source with `git show f5405ca:contracts/src/hooks/ParticipationAwareHook.sol` (and check the test the same way), then install Foundry (`foundryup`) or add it to PATH.
4. **Pyth on-chain events stop on 2026-08-17 and are very sparse in July and August** (558 and 30 events, gaps up to 3 days). This weakens E4's real-Pyth realism check and gives no post-upgrade data. Fix:
   ```
   python download_pyth_onchain_events.py --start 2025-09-01 --end 2026-09-01
   ```
   Also inspect why the count (31,962) is about 4x your expectation.
5. **No E1 calibration outputs for the new dataset**, and no found replay script for the four-pool 12-month files. This blocks E2, E3 and E6 replays until E1 is run. Fix: build the E1 fit script first (not a data problem).
6. **No post-swap liquidity or fee-charged column, and no pool metadata file.** E1 can still fit depth from price impact (sqrtPriceX96 before and after), and `amountUSD` and the amounts are usable. Fix: write a small `pools.json` with pool id, currency0/1, fee, tickSpacing, hooks address and decimals. If you want liquidity per swap, fetch from the v4 Swap events (logs) instead of the subgraph.
7. **Reference quality issues (not blocking).** The wstETH reference is a daily step (median age 11 h). USDCUSDT has 289 distinct closes. The WBTC-BTC offset shows up in the eth_wbtc_030 medians.
8. **Housekeeping.** The data lives in `data/data/`, not `data/`. `ether_gas_4y.csv` and `eth_usd_5y.csv` are in `contracts/data/`, not `data/`, and both end before the study window. The repo README numbering does not match the paper's numbering.

## 7. Caveats to state in the paper

- **USDC/USDT reference resolution.** Binance quotes USDCUSDT to 4 decimals (289 distinct closes; 173 in months 11-12), so one tick is about 1 bp, the same as the 0.01% fee. I compared with the minute VWAP (`quote_volume / volume`), not only the close. Deviations below about 1 bp are not resolvable.
- **wstETH contract-rate assumption.** The reference is the Lido contract rate, which assumes stETH = 1 ETH. It ignores the market basis (about 1-2 bp here) and is a daily step, so its median age at a swap is 11 h. The observed median deviation of 1.25-2.0 bp is of the order of the fee.
- **USDT is not USD.** Binance references for ETHUSDC use USDC, ETHBTC has no USD leg, and any USDT-quoted series carries the USDT/USD basis. For USDCUSDT the reference is a relative price, not a USD price.
- **WBTC ≈ BTC.** The eth_wbtc_030 medians drift by tens of basis points, more than the difference you would expect from noise.
- **Pyth Core upgrade on 2026-08-26 inside month 12.** The on-chain file has no events after 2026-08-17, so the post-upgrade regime is not observed. Staleness before the date cannot be extrapolated across it.
- **Lookahead avoided.** Every join uses the last completed 1-minute bar (bar open + 60 s ≤ swap time), so a swap never sees the bar it falls in.
- **`gasUsed` is unavailable** in the current swap data, so any gas-cost result must say where `gasUsed` came from.
- **Subgraph data, post-swap price only.** The swap logs come from a subgraph and carry the post-swap `sqrtPriceX96` and `tick`, not the pre-swap price.

## 8. Rerun

```
.venv\Scripts\python.exe audit\audit_data_readiness.py --data-dir data
```

Run it from the repo root (it needs pandas and numpy from the project venv). It reads only `--data-dir` (plus the parent folder for the `.env` and legacy gas search), writes only `audit/audit_results.json`, and takes a few minutes. It prints no secrets. The Level 1 tables above come straight from `audit_results.json`. The non-data checks in section 5 (git history, `forge`, file listings) were done by hand.

## 9. Addendum: corrected E6 candidate definition and offset analysis

Script: `audit/offset_and_candidates.py` (raw output `audit/offset_results.json`).

**Rerun:**

```
.venv\Scripts\python.exe audit\offset_and_candidates.py --data-dir data
```

**Definition used.** A candidate swap satisfies both of these:

- The pre-swap deviation exceeds the fee tier. The pre-swap deviation is the previous swap's ending price against the reference at this swap's time (last completed 1-minute bar; Lido rate for wstETH).
- The swap moves the price toward the reference (|post| < |pre|).

The surplus proxy is |pre-deviation| × `amountUSD`. This replaces the post-swap definition in section 4, which measured reference noise and offsets. Counts are for months 11-12.

**Signed post-swap deviation by month (bp).** Full monthly tables (median, p5, p95) are in `offset_results.json`.

| Pool (fee) | Monthly medians | Is the offset stable? |
|---|---|---|
| eth_usdc_005 (5 bp) | -0.6 to 0.0; std across months 0.2 bp; p5 to p95 roughly ±7 to ±21 | Yes; essentially no offset. |
| usdc_usdt_0001 (1 bp) | -0.08 to +0.25; std 0.1 bp; p5 to p95 about ±0.4 to ±1.0 | Yes; essentially no offset. |
| eth_wbtc_030 (30 bp) | 2025-09/10: 3.6, 0.8; 2025-11/12: about 20; 2026-01 to 2026-05: 35 to 41; 2026-06: 14; 2026-07: 16; 2026-08: 14. Std across months 13.5 bp | **No.** It steps between regimes (about 0, 20, 35-40, 14-16 bp). The pool price sits above the ETHBTC reference by a regime-dependent premium. |
| eth_wsteth_001 (1 bp) | 12.6, 5.1, 7.0, 1.5, 1.1, 9.8, 4.6, **32.9 (2026-04, p95 71)**, 1.5, 1.8, **1.1 (2026-07), 1.3 (2026-08)**. Std 9.1 bp | **No.** Large and volatile in training, about 1.2 bp in test (p5 to p95 about 0.4 to 3 bp). |

- Your reading is right for wstETH: the old 70% rate was mostly an offset.
- In the test months the wstETH offset is about 1.2 bp, close to the 1 bp fee, so it still matters for a "> fee" rule.
- WBTC's offset is 14 to 16 bp in the test months, about half the fee tier.

**E6 candidates in months 11-12 (new definition).** The middle three columns are pre-swap deviation > 1× fee; the last is > 2× fee.

| Pool | Raw (1× fee) | Minus training-period constant offset | Minus per-month median offset (in-sample) | Raw, > 2× fee | Old post-swap definition |
|---|---|---|---|---|---|
| eth_usdc_005 | 23,028 (5,757 per quartile) | 22,956 | 23,020 | 3,167 | 25,360 |
| usdc_usdt_0001 | 606 (about 151 per quartile) | 615 | 602 | 371 | 1,007 |
| eth_wbtc_030 | 2,153 (about 538 per quartile) | 2,217 | 2,142 | **4** | 1,623 |
| eth_wsteth_001 | 2,262 (about 565 per quartile) | **2,878** | **346 (about 86 per quartile)** | 454 | 4,095 |

The training-period constant offset makes wstETH and WBTC worse. The training median is 5.9 bp for wstETH and 19.4 bp for WBTC, but the test months sit near 1.2 bp and 14 to 16 bp. Do not subtract a constant training offset from these two pools.

Reading the table:

- **eth_usdc_005 and usdc_usdt_0001:** the offset is negligible, so the corrected and raw definitions agree. Both pools have more than 400 candidates at 1× fee, but usdc_usdt_0001 resolves the reference only to about 1 bp, so 1× fee candidates are at the noise floor. At 2× fee it has 371.
- **eth_wsteth_001:** with the per-month offset removed, 346 candidates remain (above 100, below 400). This number is in-sample, because it uses the test months' own median, so treat it as an optimistic bound. Options are to lower the E6 target for this pool or to add the stETH/ETH market price as a second reference.
- **eth_wbtc_030:** the count at 1× fee stays at about 2,100 after removing the monthly offset, because deviations already spread about ±30 bp around it (p5 to p95 in August: -36 to +36 bp), as wide as the fee tier. At 2× fee (60 bp) only 4 candidates remain, so E6 for this pool has to rely on the 1× fee set. The WBTC-BTC premium is a real reference-quality problem for this pool.

**What I did not do.** I did not estimate a lookahead-free rolling offset (for example the previous month's median). That is the natural next step if you want offset-corrected results in the paper without fitting the offset on test data.

**Effect on verdicts.** E6 stays READY WITH CAVEATS. The target of about 100 stratified opportunities per pool is met for eth_usdc_005 and usdc_usdt_0001. For eth_wbtc_030 it is met only at the loose 1× fee threshold, and for eth_wsteth_001 only with the in-sample offset (346 available).
