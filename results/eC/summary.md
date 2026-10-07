# Experiment C: block scope versus transaction scope on observed same-block swaps (validation months)

Script: `experiments/eC_same_block_replay.py`. Provenance: `manifest_valid.json` (git e51c8c4+dirty, input and output hashes).

- **Period.** Validation months 2026-04-01 to 2026-05-31. No test-month run: `--split test` is refused without `--confirm-frozen`.
- **Outputs.**
  - `eC_block_stats_valid.csv`: same-block frequencies.
  - `eC_summary_valid.csv`: metrics per pool, variant, setting and scope.
  - `eC_swaps_valid.csv.gz`: per-swap charges.
  - `eC_checks_valid.csv`: ε_S check.

## Setup

- **Swaps.** Observed swaps of ETH/USDC (raw) and ETH/WBTC (corr24h = E8 primary; raw as sensitivity), grouped by block and ordered by log index. Each block's swaps are replayed in order through `common/fixedpoint.ScopedHookReference`, the integer mirror of the hook, including token1 settlement (commit 8998e73 or later). Three scopes:
  - **block:** the deployed hook;
  - **tx:** the transaction-scoped contract;
  - **standalone:** every swap is its own scope, i.e. the charge the swap would pay alone.
- **Hook parameters.**
  - Reference: the benchmark lagged by one minute (`p_ref_lag_1`) times the variant's trailing offset, locked at the scope's first valid swap.
  - λ = 0.75, γ = 0.02 (E2 headline).
  - κ = R + δ + 180,214 gas × (base fee + τ̂), valued at the locked reference (the hook's formula). R is E2's median regime.
  - δ = 0, or δ = the rolling ε_S at d = 1 from the k = 1 baseline replay of the validation months. Its median equals the stored E2 summary exactly: 16.80 USD (ETH/USDC), 73.99 / 58.95 USD (ETH/WBTC corr24h / raw).
- **Settlement token.** The token the swapper receives. This is an exact-input assumption: the data do not record which side was specified.
- **Invalid oracle.** As implemented: the swap is charged 0 and not accumulated, and the next swap of the block retries.
- **Arbitrage (E8 rule).** The swap starts outside the fee band of the lag-0 benchmark (|deviation| ≥ gap_multiple × fee) and ends closer to it.
- **Standalone margin (Definition 3).** M = S − K, with S at the lag-0 benchmark and K = 180,214 gas × the block's gas price + R.

## 1. Same-block frequency (`eC_block_stats_valid.csv`)

| Pool | Swaps | Blocks with a swap | ≥ 2 swaps | ≥ 2 transactions | Swaps in blocks with ≥ 2 swaps / ≥ 2 tx | Tx with ≥ 2 swaps | Max swaps in a block |
|---|---|---|---|---|---|---|---|
| ETH/USDC | 98,087 | 83,823 | **13.4%** | **13.1%** | 26.0% / 25.5% | 0.44% | 55 |
| ETH/WBTC | 3,571 | 3,467 | 2.7% | 2.6% | 5.6% / 5.2% | 0.20% | 4 |

Arbitrage swaps: 20.7% of ETH/USDC swaps; 38.2% of ETH/WBTC swaps (corr24h), or 56.6% at the raw benchmark.

## 2. The four metrics, block versus transaction scope

Transfer = the sum of charges collected under the scope, in USD. The metrics are kept separate. Definitions:
- **(a) tipped violations:** M ≥ 0 and r > M.
- **(b) spillover charges:** r > 0 although the standalone charge is 0.
- **(c1) reversal leak:** an increase of F(A) that stays below the watermark after a counter-trade.
- **(c2) netting:** surplus that refills a negative signed cumulative surplus π·Δ left by an earlier counter-trade in the scope; its charge-unit value is the shortfall against the standalone charge.
- **(d) non-arbitrage charges:** charges paid by swaps that are not arbitrage.

**ETH/USDC.** 1,038 swaps (of 98,087) are feasible standalone.

| Setting | Scope | Transfer USD | (a) tipped (of 1,038) | (b) spillover: n, share of charged, USD | (c1) reversal leak: USD, share of transfer | (c2) netting: swaps, surplus USD, shortfall USD, share of standalone transfer | (d) non-arb: n, USD, share of transfer |
|---|---|---|---|---|---|---|---|
| δ=0 | block | 8,770 | 356 (34.3%) | 364, 18.4%, 145 | 20.5, 0.23% | 2,210, 56,765, **42,001, 83.1%** | 886, 3,120, 35.6% |
| δ=0 | tx | 50,515 | 567 (54.6%) | 33, 1.5%, 17 | 0 | 15, 67, 48, 0.09% | 756, 3,171, 6.3% |
| δ=0 | standalone | 50,515 | 567 (54.6%) | 0 | 0 | 0 | 747, 3,160, 6.3% |
| δ=ε_S | block | 3,112 | 49 (4.7%) | 56, 32.9%, 240 | 11.9, 0.38% | 2,210, 56,765, **39,502, 93.5%** | 56, 873, 28.1% |
| δ=ε_S | tx | 42,344 | 43 (4.1%) | 16, 7.8%, 68 | 0 | 15, 67, 1.5, 0.00% | 40, 772, 1.8% |
| δ=ε_S | standalone | 42,250 | 36 (3.5%) | 0 | 0 | 0 | 39, 766, 1.8% |

**ETH/WBTC corr24h.** 835 swaps are feasible standalone.

| Setting | Scope | Transfer USD | (a) | (b) | (c1) | (c2) | (d) |
|---|---|---|---|---|---|---|---|
| δ=0 | block | 6,483 | 123 (14.7%) | 12, 1.8%, 19.5 | 0 | 1 swap, 1,440, 1,080, 14.3% | 4, 9.6 |
| δ=0 | tx | 7,531 | 120 (14.4%) | 0 | 0 | 0 | 3, 9.5 |
| δ=ε_S | block | 1,231 | 3 (0.36%) | 0 | 0 | 1 swap, 1,440, 1,080, 46.7% | 0 |
| δ=ε_S | tx | 2,311 | 3 (0.36%) | 0 | 0 | 0 | 0 |

ETH/WBTC raw gives the same picture (`eC_summary_valid.csv`).
- δ=0: transfer 13,750 (block) vs 14,924 (tx); (a) 244 vs 234; (b) 24 vs 0; (c2) 3 swaps with a shortfall of 1,274 (8.5% of standalone).
- δ=ε_S: (a) 6 vs 4; (b) 2 vs 0; (c2) shortfall 1,223.

## 3. Failed-open oracle

0 swaps had an invalid oracle in either pool, so 0 swaps were charged after an earlier swap of the same block had failed open.

The lagged benchmark exists at every validation-month swap. Beyond that, every swap of a block shares one timestamp, hence one oracle state. In these data a retry within the block could therefore never succeed where the first swap failed, and the block hook's retry rule and the transaction contract's disable-for-the-transaction rule coincide.

## Reading

- **(c2) netting is the dominant block-scope effect.** In ETH/USDC the block scope collects 8,770 USD against 50,515 under the transaction scope (δ=0), and 3,112 against 42,344 with δ=ε_S.
  - **Mechanism.** A large non-arbitrage trade with negative surplus at the reference is followed in the same block by a back-run from another transaction. The back-run's surplus first refills the negative cumulative surplus and pays nothing.
  - **Counter-trades.** 99.7% of netting swaps follow a counter-trade from a different transaction. 99.7% (δ=0) and 99.98% (δ=ε_S) of the shortfall falls on arbitrage swaps.
  - **Concentration.** The shortfall is concentrated. One back-run (block 24915388, 2026-04-19) carries 80% of it (δ=0) and 85% (δ=ε_S): a 294 ETH sale moved the pool price by 13.6%, and the back-run's standalone charge is 33,638 USD. Without that block, the shortfall is 8,363 USD of a 16,877 USD standalone transfer, 49.6% (δ=0), and 5,864 of 8,612 USD, 68.1% (δ=ε_S) (`eC_netting_variants_valid.csv`). *Corrected 2026-10-07: an earlier version gave 16.6% and 13.9%, which divided the shortfall without that swap by the standalone transfer with it.*
- **(c1) the reversal leak proper is small.** 20.5 USD (δ=0) and 11.9 USD (δ=ε_S) in ETH/USDC; 0 in ETH/WBTC.
- **(b) spillover is a block-scope effect.** In ETH/USDC: 18.4% of charged swaps (δ=0) and 32.9% (δ=ε_S), against 1.5% and 7.8% under the transaction scope. In USD it is small: 145 and 240 USD.
- **(a) tipped violations.** With δ=0, most tipped violations come from the lagged reference itself: 567 of 1,038 feasible swaps standalone, the same under the transaction scope. The block scope has fewer (356) only because netting removes charges. With δ=ε_S: 49 (block) vs 43 (tx) vs 36 (standalone).
- **(d) non-arbitrage charges** are roughly unchanged in USD (3,120 vs 3,171 with δ=0). Their share of the transfer rises under the block scope (35.6% vs 6.3%) because the arbitrage transfer shrinks.
- **ETH/WBTC.** The pool rarely has same-block activity (2.7% of blocks), so the scopes differ by one to three back-runs.

## Caveats

- **Settlement side.** Token1 settlement assumes exact-input swaps.
- **Rounding of observed amounts.** Observed amounts are floats from the aligned data, rounded to base units (relative error about 1e-16).
- **κ's gas term** uses base fee + τ̂ as the hook does. K in the margin uses the block's gas price, as E2 does.
- **Dirty working tree.** The uncommitted files mirror the main checkout:
  - `common/pools.py` holds the April–May validation split, which exists only uncommitted.
  - Also `common/data_io.py`, `experiments/e2_sequential_replay.py`, the configs and the Binance klines. See the manifest's input hashes.
