# Sources of numbers added or changed in main-sigmetrics.tex (2026-09-30)

## Section 5.4.2, splitting across transactions (plan item 1.4, gamma = 0.05, delta = 0)

| Number in text | Source |
|---|---|
| 46.2% (independent rule, 2 parts, ETH/USDC) | `results/e6/tables/e6_checks_test.csv`, `indep_over_cumulative_equal`, raw, n = 2: 0.46206 |
| 0.07 USD of gas in K_hat_tx | `results/e7/tables/e7_smin_gas_conditions.csv`, ETH/USDC P50 `C_gas_actual_usd` = 0.0651 (180,032 gas at the median actual block gas price; E6 prices K at the block's actual gas price) |
| R = 0.81 USD | median regime, ETH/USDC raw (`cache/e2_test_.log`, App. B.1) = 0.8057 |
| 0.83 USD maximal saving | (1 - 0.05) x (0.0651 + 0.8057) = 0.828 |
| 185k gas for an extra transaction | 21,000 (intrinsic) + 133,966 (cold swap without hook) + 30,032 (hook), `results/e7/hook_gas_isolated.csv` rows n = 1; the Foundry measurement uses `gasleft()` around the router call and excludes the 21,000 |
| 0.07 USD extra-transaction cost | 0.0651 x 184,998 / 180,032 = 0.0669 |
| "about twelve times" | 0.828 / 0.0669 = 12.4 |
| ETH/WBTC R = 5.05 USD | median regime with the 24-hour offset (App. B.1), matching the offset variant of Fig. 9a |

## Section 5.5, charging threshold with a constant tip (decision 6)

| Number | Source |
|---|---|
| tau_hat = 0.02 gwei | `results/e7/smin_formula.txt` (20,000,000 wei, median `tip_p50` of the training months' fee-history blocks) |
| 14.10 / 16.18 USD | `e7_smin_gas_conditions.csv`, ETH/USDC `S_min_usd` at P50 / P99 |
| 0.86 / 2.94 USD | same table, `S_min_delta0_usd` at P50 / P99 |
| 0.7% (5.2% without buffer) | `e7_smin_share_test.csv`, ETH/USDC 0.006557 / 0.052284 |
| 0.1% ETH/WBTC, 0.3% ETH/wstETH | same table, 0.000929 / 0.002653 |
| 0.5 USD (hook gas at P99) | `e7_smin_gas_conditions.csv`, `hook_gas_actual_usd` P99 = 0.484 |

## Section 5.4.2, revised 2026-09-30 after the reviewer's rewrite (supersedes the 1.4 table above)

Convention (user decision): K_hat_tx is priced as in Eq. (9), 180,032 gas at the median base fee plus tau_hat;
the extra transaction is priced at the actual median gas price that the arbitrager pays.

| Number in text | Unrounded | Source |
|---|---|---|
| 0.06 USD of gas in K_hat_tx | 0.0579 | `e7_smin_gas_conditions.csv`, ETH/USDC P50 `C_gas_usd` (base fee + tau_hat) |
| R = 0.81 USD | 0.8057 | same table, `R_protocol_usd` |
| upper bound 0.82 USD | 0.95 x 0.8636 = 0.8204 | |
| 185k gas, 0.07 USD | 184,998 gas; 0.0669 | `hook_gas_isolated.csv`; `C_gas_actual_usd` P50 x 184,998 / 180,032 |
| about twelve times | 12.26 | 0.8204 / 0.0669 |
| 2c = 1.73 USD | 1.7272 | |
| lower bound 0.65 USD | 0.75 x 0.8636 = 0.6477 | |
| about ten times | 9.68 | 0.6477 / 0.0669 |
| 63%, 1,177 | 0.6262 (737 of 1,177) | `results/e6/tables/e6_cross_tx_split_test.csv`, `share_a_ge_2c` |
| median 0.58 USD, P95 0.75 USD | 0.5751, 0.7526 | same table, `net_median_usd`, `net_p95_usd` |

The reviewer's 1.76 and 0.66 came from rounding before multiplying (0.07 + 0.81 = 0.88).

## Hook gas of the current contract (settlement, commit 8ca840a), added 2026-09-30

| Number in text | Unrounded | Source |
|---|---|---|
| 30.2k single swap, 13.8k extra fragment, 5.7k fail-open | 30,214; 13,823; 5,684.5 | `results/e7/gas_contract_8ca840a/gas_profile.json` |
| 238k for sixteen fragments | 237,559 | `results/e7/gas_contract_8ca840a/tables/e7_gas_isolated.csv` |
| settlement adds 31.6k, charged swap 61.8k | 31,593; 61,807 | `results/e7/gas_contract_8ca840a/hook_gas_settled.csv` |
| about 0.02 USD for a charged swap | 61,807 / 180,032 x 0.0651 = 0.0224 | median actual gas price, `e7_smin_gas_conditions.csv` |
| replays understate by about 0.01 USD | (61,807 - 30,032) / 180,032 x 0.0651 = 0.0115 | same |
| 185k extra transaction | 21,000 + 133,966 + 30,214 = 185,180 | isolated test of the current contract |
| split pays for 90%, median 0.57 USD, P95 0.75 USD | 0.9040, 0.5744, 0.7510 | `results/e6/tables/e6_cross_tx_split_test.csv` (current-contract gas, settlement when both parts are charged) |

## Experiment A: strategic splitting (transaction scope) and spillover (block scope), added 2026-10-06

Script `experiments/eA_strategic_split.py`; all files under `results/eA/` (manifest.json: git 2f994c9+dirty, input/output hashes).
E2 replays re-executed in memory to recover per-correction transfers; totals equal the stored E2 summaries (`eA_checks.csv`).

| Number | Unrounded | Source |
|---|---|---|
| swap gas of an extra transaction 133,966; extra tx 185,180 gas; settlement 31,593 | 133966; 185180; 31593 | `results/e7/gas_contract_8ca840a/hook_gas_isolated.csv` (n=1, hook=0), `hook_gas_settled.csv`; `results/eA/manifest.json` parameters |
| surviving share at l*, ideal (retained, k=1, d=0), ETH/USDC: 3.24% | 0.032424 | `results/eA/eA_part1_summary.csv`, setting ideal_retained_k1_d0, eth_usdc_005, `surviving_share` |
| recovered under strategic splitting, ideal: 853 of 26,323 USD | 853.48; 26322.50 | same row, `strategic_usd`, `original_usd` |
| surviving share at l*, buffered (delta=eps_S, k=15, d=1), ETH/USDC: 0.04% | 0.000390 | same file, buffered_1eps_k15_d1, eth_usdc_005 |
| recovered under strategic splitting, buffered: 7.5 of 19,139 USD | 7.4558; 19139.24 | same row, `strategic_usd`, `original_usd` |
| surviving share at l=2: 75.0% ideal, 84.2% buffered | 0.749899; 0.841612 | same rows, `surviving_share_l2` |
| splitting pays: 93.5% (ideal), 82.1% of charged / 16.1% of all (buffered) | 0.934921; 0.821346; 0.160617 | same rows, `share_split_pays_of_charged`, `share_split_pays` |
| net saving (charged) median / P95: 1.01 / 8.95 USD ideal; 4.11 / 181 USD buffered | 1.0101, 8.9483; 4.1145, 180.96 | same rows, `net_saving_median_usd_charged`, `net_saving_p95_usd_charged` |
| l* median / P95 / max: 2 / 10 / 525 ideal; 2 / 15.5 / 165 buffered | | same rows, `l_star_*` |
| E6 sample ETH/USDC delta=0: surviving 0.09% (l*), 99.2% (l=2), n=730 | 0.000918; 0.992066 | same file, e6_sample_delta0, eth_usdc_005 |
| E6 sample ETH/USDC: one 69k USD correction carries 87.5% of F | 68997.7; 0.875 | computed from `eA_part1_corrections.csv.gz` (top 1% share of F_a_usd) |
| E6 sample ETH/WBTC delta=0: surviving 4.1% raw (n=531), 1.7% offset (n=888) | 0.040777; 0.017124 | same file, e6_sample_delta0, eth_wbtc_030 raw / corr24h |
| block scope ETH/USDC: 20,872 price-correcting swaps, 9.3% preceded by a positive-surplus swap | 0.092900 | `results/eA/eA_part2_summary.csv`, eth_usdc_005, `share_preceded_pos` |
| overcharged: 0.97% (delta=0), 0.13% (delta=eps_S) | 0.009726; 0.001294 | same file, `share_overcharged` |
| overcharge median / P95 / max: 0.44 / 1.34 / 1.72 USD (delta=0); 3.54 / 11.1 / 15.9 USD (delta=eps_S) | 0.4424, 1.3427, 1.7156; 3.5387, 11.105, 15.937 | same file, `overcharge_*_usd_pos`, `overcharge_max_usd` |
| overcharge > own margin (a-kappa)^+: 0.87% / 0.13% | 0.008672; 0.001294 | same file, `share_violation_spec` |
| Proposition 3 violations: 0 (all pools, both settings); max overcharge / ((1-gamma) kappa) = 1.000 | 0; 1.0 | same file, `n_prop3_violation`, `max_overcharge_over_bound` |
| 92% of overcharged ETH/USDC swaps (delta=0) follow another origin's swap | 186 / 203 = 0.916 | `results/eA/eA_part2_by_sender.csv`, eth_usdc_005 delta0, `n_overcharged` |
| partial corrections ETH/USDC completed by another origin: 21.1% same block, 41.3% within 1 block, 51.5% within 2 | 0.210701; 0.41276; 0.515033 | `results/eA/eA_part3_summary.csv`, eth_usdc_005 |

### Experiment A follow-up (2026-10-06), `experiments/eA_followup.py`

| Number | Unrounded | Source |
|---|---|---|
| block gas limit 60M in the test months, cap 323-324 transactions | 60,000,000 median, 59,824,338 min; 324 / 323 | `results/eA/eA_fu_capacity_summary.csv`, `gas_limit_*`, `cap_block_*` |
| surviving share with the block cap, ETH/USDC: 4.30% ideal (1,132 USD), 0.04% buffered | 0.042992; 1131.66; 0.000390 | same file, `surviving_share_block`, `strategic_usd_block` |
| l <= 50: 18.3% ideal, 11.7% buffered; l <= 10: 37.4% ideal, 41.1% buffered | 0.183159, 0.116508; 0.373548, 0.410935 | same file, `surviving_share_l<=50`, `surviving_share_l<=10` |
| cap binds for 2 ideal ETH/USDC corrections carrying 3.6% of the transfer | 2; 0.036486 | same file, `n_cap_binds_block`, `transfer_share_cap_binds_block` |
| l* max 525 uncapped, 324 capped (ideal ETH/USDC) | | same file, `l_star_max_uncapped`, `l_star_max_block` |
| E6 sample ETH/USDC with block cap: 86.0% survives; 15 corrections, 91.2% of the transfer, exceed the cap | 0.860417; 0.911779 | same file, e6_sample delta0 |
| 45.1% = mean of per-correction ratios (sum-weighted: 99.4%) | 0.4510147; 0.994325 | `results/eA/eA_fu_aggregation.csv` (reproduces `results/e6/tables/e6_checks_test.csv` indep_over_cumulative_equal n=2) |
| optimal l=2, mean of ratios: 42.0% (E6 sample), 23.8% (E2 ideal), 24.0% (E2 buffered) | 0.419741; 0.238015; 0.239738 | `eA_fu_aggregation.csv`, method optimal l = 2 |
| block scope ETH/USDC, participation violation: 0.97% (delta=0), 0.13% (eps_S) of PC swaps | 0.009678; 0.001294 | `results/eA/eA_fu_part2_violation.csv`, `share_violation` |
| turned into a violation (estimated-feasible before): 0.20% (42 swaps), 0.03% (6) | 0.002012; 0.000287 | same file, `share_turned_of_pc` |

### Experiment A follow-up 2 (2026-10-06): block-scoped hook, `experiments/eA_followup2.py`

- **Output.** `results/eA/eA_fu2_block_scope.csv` (sha256 5ed3e746…79bc4). Manifest: `results/eA/manifest_followup2.json`.
- **Commit.** git bfa921d+dirty. The dirty files under common/experiments/tests:
  - modified: `common/data_io.py`, `common/fixedpoint.py`, `common/pools.py`, `experiments/build_aligned_dataset.py`, `experiments/configs/e{1,2,4,5,6}.yml`, `experiments/e2_sequential_replay.py`, `experiments/e6_cross_tx_split.py`, `experiments/fees_tvl_check.py`
  - untracked: `experiments/e8_*`, `experiments/configs/e8.yml`, `experiments/e6_block_scope_check.py`, `experiments/gas_cold_table.py`, `experiments/make_data_manifest.py`
- **Why the dirty tree does not matter.** The only dirty file on the computation path is `common/fixedpoint.py`. Its diff adds token1 settlement (`settle_token0=False`) to `ScopedHookReference`. The script uses the default token0 path, where `collected = r`, as in the committed code.
- **Labels.** Every share is labelled in the CSV's `aggregation` column. SW = sum-weighted (sum of charges / sum of F(a)). MR = mean of per-correction ratios (over F(a) > 0).

| Number | Unrounded | Source |
|---|---|---|
| within one block, every split l <= block capacity (323-324): surviving 100%, SW and MR, ideal and buffered, ETH/USDC and ETH/WBTC raw/offset | 1.0 (all 24 part-1 rows) | `eA_fu2_block_scope.csv`, part "1 within one block", `share` |
| deviation of the block total from F(a): 0 WAD units (1e-18 USD), min and max over all splits | 0; 0 | same rows, `deviation_wei_min`, `deviation_wei_max`, `n_splits_nonzero_deviation` = 0 |
| 4,139,001 splits, 669,617,937 pieces evaluated | 4139001; 669617937 | same rows, `n_splits_evaluated`, `n_pieces_evaluated` (summed over the 6 rule x pool groups) |
| l* with gas = 1 for every correction | max 1 | same rows, `l_star_with_gas_max` |
| scope="tx" reproduces Experiment A's charge(l*): 0 mismatches | 0 | same rows, `tx_scope_check_mismatches` |
| pool rounding not modelled; Solidity test 0 / 3 / 7 wei for 2 / 4 / 8 tx | | NOTES/2026-10-06_block_scoped_hook.md section 4 |
| cross-block, l <= 5 blocks, ETH/USDC ideal: SW 50.3% (13,249 USD), MR 12.4% | 0.503348; 13249.37; 0.124317 | `eA_fu2_block_scope.csv`, part "2 cross-block bracket", ideal_retained_k1_d0, eth_usdc_005, l_cap "l<=5 blocks" |
| cross-block, l <= 75 blocks, ETH/USDC ideal: SW 14.7% (3,881 USD), MR 6.9% | 0.147440; 3880.99; 0.068841 | same, l_cap "l<=75 blocks" |
| cross-block, l <= 5 blocks, ETH/USDC buffered: SW 60.5% (11,583 USD), MR 27.5% | 0.605186; 11582.81; 0.274587 | same, buffered_1eps_k15_d1 |
| cross-block, l <= 75 blocks, ETH/USDC buffered: SW 6.5% (1,247 USD), MR 18.1% | 0.065171; 1247.33; 0.180916 | same |
| ETH/USDC cap binds, ideal: 824 (62.9% of transfer) at 5 blocks, 19 (19.1%) at 75 | 824, 0.629148; 19, 0.190902 | same rows, `n_cap_binds`, `transfer_share_cap_binds` |
| ETH/USDC cap binds, buffered: 70 (79.8%) at 5 blocks, 3 (19.7%) at 75 | 70, 0.798461; 3, 0.197348 | same |
| ETH/WBTC raw ideal: SW 51.3% / 1.77%, MR 12.4% / 4.2% (5 / 75 blocks) | 0.512941 / 0.017706; 0.123859 / 0.041687 | same file, ideal_retained_k1_d0, eth_wbtc_030 raw |
| ETH/WBTC offset ideal: SW 51.9% / 1.75%, MR 13.6% / 4.7% | 0.518783 / 0.017527; 0.136333 / 0.047329 | same, corr24h |
| ETH/WBTC raw buffered: SW 57.7% / 0.002%, MR 19.4% / 5.6% | 0.577339 / 1.99295e-05; 0.193571 / 0.055556 | same, buffered_1eps_k15_d1, raw |
| ETH/WBTC offset buffered: SW 61.1% / 0.001%, MR 19.7% / 5.4% | 0.610955 / 1.39659e-05; 0.197155 / 0.054348 | same, corr24h |
| ETH/WBTC cap binds at 5 / 75 blocks: ideal raw 182 (68.9%) / 4 (8.1%), offset 165 (70.0%) / 3 (7.0%); buffered raw 24 (78.3%) / 0, offset 21 (83.5%) / 0 | 0.688949, 0.080523; 0.699896, 0.069963; 0.782733; 0.834599 | same rows, `n_cap_binds`, `transfer_share_cap_binds` |
| ETH/wstETH (both caps equal, max l* <= 3): ideal SW 2.4% / 2.3%, MR 11.1% / 34.4% (raw / offset); buffered SW 1.6% / 3.9%, MR 7.5% / 28.1% | 0.023825 / 0.022837; 0.111111 / 0.344004; 0.015695 / 0.039206; 0.074547 / 0.28125 | same file, eth_wsteth_001 |
| l <= 10 and l <= 50 shares of `eA_fu_capacity_summary.csv` reproduced exactly by the cross-block code | | assertion in `eA_followup2.py` |
