# Experiment A: strategic splitting under transaction scope, spillover under block scope

Script: `experiments/eA_strategic_split.py` (post-processing). Provenance: `manifest.json` (git 2f994c9+dirty, input and output hashes, parameters).
Test months 2026-06-01 to 2026-07-26. F is the integer reference (`common/fixedpoint.transfer_wad`, USD in WAD, round down).

## Parameters and assumptions

- **Gas.** Extra transaction = 21,000 + **133,966** (swap) + 30,214 (hook) = 185,180 gas. Settlement = 31,593 gas, paid only by the one piece with a positive charge.
  - Swap gas: `results/e7/gas_contract_8ca840a/hook_gas_isolated.csv`, row n=1, hook=0. This is a cold swap without the hook in the Foundry test pool.
  - Hook gas: the n=1, hook=1 row minus the swap gas.
  - Settlement: `hook_gas_settled.csv`, charged_hook minus uncharged_hook.
- **Prices.** Gas is priced at the block's actual gas price (E2: the minute's gas price) times the ETH price. Amounts are in USD, i.e. the numeraire (quote) token, which is token0 in the hook's convention, times `usd_per_num`. One base unit is one smallest unit of that token.
- **E6 sample (Part 1a).**
  - λ=0.75, γ=0.05 (e6.yml).
  - κ = 180,214 gas × (block base fee + τ̂ = 0.05 gwei) + median R + δ.
  - a = S at the swap's own reference.
  - δ = 0, or δ = rolling ε_S(d=1) from the k=1 baseline replay at the swap's minute.
- **E2 replays (Part 1b).**
  - λ=0.75, γ=0.02 (E2 headline). κ uses the run's own K̂: 180,214 gas at the minute's gas price, plus R, plus δ.
  - The settings are retained margin (k=1, d=0) and buffered δ=ε_S (k=15, d=1).
  - **Each correction keeps its executed size. The path is not re-simulated under splitting.**
- **Deviation from the brief.** The replays never saved per-correction data. The frozen `simulate()` was therefore re-executed in memory on the baseline plus the one configuration. Nothing in results/e2 was written.
  - The recomputed totals equal the stored ones to the cent: 26,322.50 and 19,139.24 USD (`eA_checks.csv`).
  - a is recovered exactly as F⁻¹(r) = max(r/λ, κ + r/(1−γ)).
- **Price-correcting swap.** The E6 definition: deviation before the swap > fee, and the swap moves the price toward the reference.
- **Block scope (Part 2).** The reference is locked at the first swap of the block. A_prev includes every earlier swap in the block on that pool.
- **Sender data.** The raw swap CSVs contain `sender` and `origin`; all test swaps joined. Sender splits use `origin` (tx.origin).

## Part 1: optimal split under the transaction scope (`eA_part1_summary.csv`, per correction: `eA_part1_corrections.csv.gz`)

| Setting | Pool | n | Original USD | Strategic USD | Surviving l* | Surviving l=2 | Split pays (of charged) | Net saving med / P95 (charged) | l* med / P95 / max (charged) |
|---|---|---|---|---|---|---|---|---|---|
| E6 sample, δ=0 | ETH/USDC | 730 | 106,066 | 97 | 0.09% | 99.2% | 92.6% | 1.69 / 73.3 | 3 / 79 / 48,413 |
| E6 sample, δ=ε_S | ETH/USDC | 730 (147 charged) | 103,638 | 8 | 0.01% | 98.5% | 98.0% | 21.5 / 889 | 3 / 53 / 3,531 |
| E6 sample, δ=0 | ETH/WBTC raw | 531 | 3,353 | 137 | 4.1% | 59.7% | 97.2% | 2.10 / 12.6 | 2 / 4 / 204 |
| E6 sample, δ=ε_S | ETH/WBTC raw | 531 (7) | 1,228 | 111 | 9.0% | 84.1% | 85.7% | 16.3 / 595 | 2 / 17 / 22 |
| E6 sample, δ=0 | ETH/WBTC offset | 888 | 7,138 | 122 | 1.7% | 60.1% | 97.7% | 3.56 / 21.3 | 2 / 7 / 257 |
| E6 sample, δ=ε_S | ETH/WBTC offset | 888 (8) | 1,259 | 0 | 0.0% | 89.1% | 100% | 8.46 / 725 | 2 / 17 / 24 |
| Ideal, retained k=1 d=0 | ETH/USDC | 7,053 | **26,323** | **853** | **3.24%** | **75.0%** | 93.5% | 1.01 / 8.95 | 2 / 10 / 525 |
| Buffered δ=ε_S k=15 d=1 | ETH/USDC | 2,204 (431) | **19,139** | **7.5** | **0.04%** | **84.2%** | 82.1% | 4.11 / 181 | 2 / 15.5 / 165 |
| Ideal | ETH/WBTC raw / offset | 1,247 / 1,039 | 16,246 / 15,102 | 40 / 49 | 0.25% / 0.33% | 76.6% / 76.6% | 96.0% / 95.4% | 3.70 / 4.58 (med) | max 99 / 97 |
| Buffered | ETH/WBTC raw / offset | 639 / 531 | 21,687 / 19,284 | 0.43 / 0.27 | 0.002% / 0.001% | 83.4% / 83.3% | 94.4% / 94.6% | 50.5 / 43.1 (med) | max 47 / 40 |
| Ideal / buffered | ETH/wstETH raw | 27 | 7.72 / 7.90 | 0.18 / 0.12 | 2.4% / 1.6% | 0% / 0.07% | 88.9% / 92.6% | 0.26 / 0.24 (med) | max 2 |

Monetary columns are in USD. "Charged" means F(a) > 0. Across all corrections, including uncharged ones, splitting pays for 93.5% (ideal) and 16.1% (buffered) of ETH/USDC corrections. The l=2 surviving share uses one piece of κ − 1 unit. It agrees with the exact best two-transaction split (breakpoint search) to within 0.003 percentage points.

### Reading Part 1

- **Full split.** A strategic arbitrager that splits optimally pays almost nothing. Under the ideal reference, 3.2% of the 26,323 USD survives in ETH/USDC (853 USD). Under the buffered rule, 0.04% of 19,139 USD survives (7.5 USD).
  - Each extra transaction costs about 0.05 to 0.17 USD and avoids up to (1−γ)κ: about 1.4 USD with δ=0, and about 17 USD with δ≈15.6 USD.
  - **A larger buffer therefore makes splitting more attractive.**
- **Two transactions.** A split into two transactions already removes 25% (ideal) or 16% (buffered) of the ETH/USDC transfer.
- **E6 ETH/USDC concentration.** One CPMM-replayed correction of 69,000 USD carries 87.5% of F in the sample, which drives the 99.2% l=2 survival. Excluding the top 1% of corrections, the surviving share is 0.74% at l* and 93.7% at l=2.
- **Bound check.** Every correction's gross saving obeys Proposition prop:cross-tx, F(a) − charge(l) ≤ min{lλ, (l−1)(1−γ)}κ, with 0 violations. The maximum ratio is 1.000, so the bound is attained.

## Part 2: spillover under the planned block scope (`eA_part2_summary.csv`, `eA_part2_by_sender.csv`, swaps: `eA_part2_swaps.csv.gz`)

| Pool | PC swaps | Preceded by positive surplus | Overcharged δ=0 / δ=ε_S | Overcharge med / P95 / max, USD (δ=0) | Same (δ=ε_S) | > own margin (a−κ)⁺, δ=0 / δ=ε_S | Prop. 3 violations |
|---|---|---|---|---|---|---|---|
| ETH/USDC | 20,872 | 9.29% | **0.97%** / 0.13% | **0.44** / 1.34 / 1.72 | 3.54 / 11.1 / 15.9 | 0.87% / 0.13% | **0** |
| ETH/WBTC raw | 2,249 | 2.40% | 1.02% / 0.04% | 1.51 / 4.31 / 4.51 | 19.2 (n=1) | 0.84% / 0.04% | 0 |
| ETH/WBTC offset | 2,017 | 1.34% | 0.84% / 0.05% | 2.19 / 5.67 / 5.88 | 42.8 (n=1) | 0.69% / 0.05% | 0 |
| ETH/wstETH raw | 3,284 | 3.41% | 0.37% / 0.24% | 0.15 / 0.93 / 1.15 | 0.16 / 1.15 / 1.35 | 0.27% / 0.15% | 0 |
| ETH/wstETH offset | 769 | 5.20% | 0.65% / 0.26% | 0.40 / 0.95 / 1.08 | 0.80 / 1.31 / 1.37 | 0.39% / 0.13% | 0 |

Overcharge quantiles are over overcharged swaps; over all price-correcting swaps the median and P95 are 0.

- **Proposition 3.** overcharge ≤ (1−γ)κ holds for every swap, with a tolerance of 2 WAD units for integer rounding. The maximum ratio is 1.000 (attained).
- **Stricter violation criterion.** Total block charge > (a−κ)⁺, i.e. overcharge > (a−κ)⁺ − F(a): 0.97% / 0.13% in ETH/USDC.
- **By sender (ETH/USDC, δ=0).** Of the 1,939 price-correcting swaps preceded by a positive-surplus swap:
  - 1,849 have an earlier swap from a different origin in the block; 10.1% of them are overcharged (186 swaps, 95.8 USD in total).
  - 90 have only same-origin predecessors; 18.9% of them are overcharged (17 swaps, 13.0 USD).
  - So 92% of overcharged ETH/USDC swaps pay for another sender's surplus.
- **Undercharge.** A block-scoped charge can also be smaller than F(a_j), for example when earlier swaps in the block had negative surplus: 372 ETH/USDC swaps (δ=0).

## Part 3: who completes partial corrections (`eA_part3_summary.csv`)

A partial correction is a price-correcting swap that leaves the pool deviated by more than the fee. The table gives the share of partial corrections whose next price-correcting swap on the pool comes from a different tx.origin.

| Pool | Partial | Same block | Within 1 block (gap ≤ 1) | Within 2 blocks |
|---|---|---|---|---|
| ETH/USDC | 8,448 | 21.1% | 41.3% | 51.5% |
| ETH/WBTC raw / offset | 1,939 / 1,843 | 2.7% / 1.5% | 22.3% / 21.0% | 33.8% / 31.6% |
| ETH/wstETH raw / offset | 3,141 / 646 | 2.9% / 4.5% | 6.7% / 11.0% | 10.3% / 13.9% |

The same origin completes the correction within 1 block in only 1.2% to 3.3% of cases.

## Five-line summary

1. **Surviving share at the optimal split (ETH/USDC):** 3.24% ideal (853 of 26,323 USD) and 0.04% buffered (7.5 of 19,139 USD).
2. **Surviving share at l=2:** 75.0% ideal and 84.2% buffered.
3. **Spillover frequency under block scope (ETH/USDC):** 9.3% of price-correcting swaps follow a positive-surplus swap in their block. 0.97% are overcharged with δ=0 and 0.13% with δ=ε_S; 0.87% / 0.13% exceed their own margin.
4. **Median overcharge (ETH/USDC, overcharged swaps):** 0.44 USD with δ=0 and 3.54 USD with δ=ε_S.
5. **Proposition 3:** 0 violations in all pools and settings. The bound is attained (max ratio 1.000).

---

# Follow-up (2026-10-06): `experiments/eA_followup.py`

Post-processing of the files above. Nothing was re-simulated. Provenance: `manifest_followup.json`, plus a `provenance` block added to `manifest.json`.

## 1. Block capacity (`eA_fu_capacity_summary.csv`; per correction: `eA_fu_capacity_corrections.csv.gz`)

The cap is l ≤ floor(gas_limit_b / 185,180), with gas_limit_b from the BigQuery blocks export (`data/data/gas/bq-results-*.csv`). An E6 correction uses its own block. An E2 correction uses the last block strictly before its grid minute, the same block whose gas price the replay uses. The gas limit is 60M in every test-month block (minimum 59.8M), so the cap is 323 to 324 transactions.

**ETH/USDC:**

| Setting | Uncapped | Block cap | l ≤ 50 | l ≤ 10 | l = 2 | Cap binds: n (transfer share) |
|---|---|---|---|---|---|---|
| Ideal, retained k=1 d=0 | 3.24% | **4.30%** (1,132 USD) | 18.3% | 37.4% | 75.0% | 2 (3.6%) |
| Buffered δ=ε_S k=15 d=1 | 0.04% | **0.04%** (7.5 USD) | 11.7% | 41.1% | 84.2% | 0 (0%) |
| E6 sample δ=0 | 0.09% | 86.0% | 93.5% | 96.9% | 99.2% | 15 (91.2%) |
| E6 sample δ=ε_S | 0.01% | 67.4% | 84.9% | 93.4% | 98.5% | 5 (86.5%) |

**Distribution of l\* over charged corrections:**
- Ideal: median / P95 / max is 2 / 10 / 525 uncapped and 2 / 10 / 324 with the block cap.
- Buffered: 2 / 15.5 / 165 both ways.
- E6 sample δ=0: 3 / 78.7 / 48,413 uncapped and 3 / 78.7 / 324 capped.

**Other pools:**
- The block cap never binds in ETH/WBTC or ETH/wstETH.
- With l ≤ 10, about 35% of the ideal ETH/WBTC transfer survives and about 36–37% of the buffered transfer.

The block cap matters only for the few very large corrections. In the E6 ETH/USDC sample, 15 corrections carry 91% of the transfer.

## 2. Reconciling the 45.1% (`eA_fu_aggregation.csv`)

**The 45.1% of Section 5.4.2 is the mean of per-correction ratios.** It is `indep_over_cumulative_equal` at n=2 in `results/e6/tables/e6_checks_test.csv`, computed with `np.mean` over corrections with F > 0. It is reproduced here exactly as 0.4510147: the independent rule, two equal-input CPMM fragments, E6's K (180,214 gas at the block's actual gas price, plus R), γ=0.05, δ=0. The integer reference gives the same value.

| ETH/USDC, two-way split | Sum-weighted | Mean of ratios |
|---|---|---|
| Independent rule, equal-input CPMM fragments, E6's K (the paper's 45.1%) | 99.4% | **45.1%** |
| Same, with Experiment A's κ (base fee + τ̂) | 99.4% | 46.7% |
| Two equal-surplus pieces, Experiment A's κ | 99.4% | 43.3% |
| Optimal l=2, Experiment A's κ (E6 sample) | 99.2% | 42.0% |
| Optimal l=2, E2 ideal replay | 75.0% | 23.8% |
| Optimal l=2, E2 buffered replay | 84.2% | 24.0% |

- In the E6 sample, the sum-weighted figure is dominated by one 69k USD correction. The mean of ratios describes a typical correction.
- In the E2 replays, both figures are informative. A typical charged correction keeps about 24% of its transfer at l=2. Large corrections, which carry most of the money, keep about 75% (ideal) and 84% (buffered).
- The main Part 1 tables use sum-weighted shares, which are recovered USD over original USD.

## 3. Part 2: overcharge that becomes a participation violation (`eA_fu_part2_violation.csv`)

A violation means the total block-scope charge F(a) + overcharge exceeds the own estimated margin (a−κ)⁺. "Turned into" means the swap was estimated-feasible (a ≥ κ) and is no longer.

| Pool | δ | Violation, share of PC swaps | Turned into violation, share of PC swaps | … of estimated-feasible | Already infeasible |
|---|---|---|---|---|---|
| ETH/USDC | 0 | 0.97% (202) | **0.20%** (42) | 5.5% of 760 | 160 |
| ETH/USDC | ε_S | 0.13% (27) | **0.03%** (6) | 3.9% of 155 | 21 |
| ETH/WBTC raw | 0 / ε_S | 0.93% / 0.04% | 0.22% / 0 | 0.9% / 0 | 16 / 1 |
| ETH/WBTC offset | 0 / ε_S | 0.84% / 0.05% | 0.30% / 0.05% | 0.7% / 11% (1 of 9) | 11 / 0 |
| ETH/wstETH raw | 0 / ε_S | 0.34% / 0.24% | 0.15% / 0.09% | 3.9% / 5.8% | 6 / 5 |
| ETH/wstETH offset | 0 / ε_S | 0.65% / 0.26% | 0.26% / 0.13% | 5.6% / 5.0% | 3 / 1 |

Most swaps that the block scope pushes over their margin were already below κ under the estimate (a < κ), so an estimated margin of zero is exceeded by any charge.

## 4. Manifest

`manifest.json` now has a `provenance` block, and `manifest_followup.json` repeats it with the follow-up's input and output hashes. The block records that the per-correction sizes came from an in-memory re-execution of the frozen, unmodified `simulate()`. That re-execution reproduced every stored total bit for bit: all 10 protection totals with their execution counts, and all 5 rolling ε_S medians, compared with round-trip float parsing.
