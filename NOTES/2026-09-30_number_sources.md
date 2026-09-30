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
