# Paper reruns with the final rule, validation months (2026-10-08)

Branch `final-integration`. Every output is under `results/final_valid/` (untracked), written with `RESULTS_RUN=final_valid`. Stored results are never overwritten. The comparison with the stored validation results is `experiments/compare_final_valid.py`, which writes `results/final_valid/comparison/*.csv`, including `file_list.csv` (every file with the commit its manifest records). Test months: not run.

## Setup

| Commit | Step |
|---|---|
| 5752782 | merge `claude/pnet-contract` at cab9c14 (P-net hook, observed-swap eps calibration); NOTES commit dd14eaa not merged |
| e63693e | `RESULTS_RUN` output root (`reporting.out_dir`, `gas_cold_table.py`) |
| c1b6197 | `config/gas_block_scope.json` = P-net hook gas, measured on e63693e (contracts identical to cab9c14) |
| 7413862 | `eps_observed_calibration.py --variants e2`: every E2 pool variant |
| 28a2827 | E2 final rule (`experiments/configs/e2_final.yml`) |
| 0b8653e | `e2_recoverable_margin.py --tag` |
| 8a699a0 | one-sided proportional buffer (E8), training-month eps (E5) |
| 9e48b68 | E8 final rule (`e8_final.yml`) |
| a520822 | E4, E5, E6, E7 and B.10 on the final rule (`e*_final.yml`), `BlockScopedHookFragmentGasTest` |
| 014a7ed | E5 fix: the daily eps is in ppb and must be divided by 1e9 (the first E5 run on a520822 had no charges; rerun) |

**Gas** (cold, `forge test --isolate`, contracts = cab9c14):

| Case | Gas |
|---|---|
| first swap of a block, surplus changes (uncharged correction) | 38,520 (was 38,696) |
| charged swap | 52,161 |
| charged swap, vault holds none of the token | 69,261 |
| second transaction, same block | 19,832 |
| second swap in a transaction | 8,637 |
| extra transaction, new block | 159,556 / 173,197 |

`config/gas_block_scope.json` holds these figures. ĝ = 150,000 + 52,161 = 202,161, the charged figure (DECISIONS I3). The Foundry test configuration `_finalParams` still deploys ĝ = 180,214 (`FINAL_GAS_UNITS`); that is a test constant, not used by the reruns.

**eps.** The daily observed-swap calibration: at 00:00 UTC, the rolling 7-day p95 of |P̂/P − 1| over observed price-correcting swaps, rounded up to whole ppb (`eps_observed_daily_valid.csv`; clean run on 28a2827). The P-net branch calibrated only the three Experiment C pool variants; the same method is extended here to every E2 variant.

| Pool variant | Median daily eps |
|---|---|
| ETH/USDC raw | 15.0 bp |
| ETH/WBTC raw / corr24h / corr6h / corr7d | 13.2 / 13.5 / 13.5 / 13.4 bp |
| ETH/wstETH, all four variants | 0 every day |

ETH/wstETH's reference (the Lido rate lagged one minute) equals the benchmark at 99.94% of its swaps, so its buffered rules equal the retained margin.

Extra eps files:
- **One-sided (E8):** `eps_observed_signed_daily_valid.csv`, the p95 of sign(d0)·(P̂/P − 1), floored at 0. Median 5.4 bp ETH/USDC, 3.4 bp ETH/WBTC.
- **Training months (E5):** `eps_observed_daily_train.csv`. Median 23.2 bp ETH/USDC, 18.7 bp ETH/WBTC raw.
- The two-sided file stayed byte-identical when the signed and training outputs were added.

**Final buffered rule.** Each correction is one transaction in its own block. The hook's surplus is the bracket [Ŝ − dmult · eps · |net token1 change|]⁺, which is the contract's ⌈ε π̂ |X₁|⌉ with token0 = ETH, expressed in the replay's numeraire (the pool's token1). Then δ = 0 and F = min(λ·bracket, (1 − γ)[bracket − K̂]⁺); λ applies to the buffered surplus, as in the contract.

Q4 and Q6 put the buffer on the ETH leg instead. The two differ by the trade's fee and price impact only.

Replays keep K̂ = K (gas at the block's actual price, ĝ = the charged gas). τ̂ = 3 gwei enters where K̂ is estimated: E4 and B.10.

## Deviations and notes

- **E2 delay grid:** ETH/USDC only, as requested. E8 runs its delay grid on all three pools. Its as-frozen check compares the ETH/USDC rows, which are bit-identical, and reports the other pools as having no E2 counterpart.
- **E4** keeps its own γ = 0.05.
  - "Rolling" is the daily observed-swap eps; "valid (in-sample)" is one value fitted on all validation correcting swaps.
  - The full buffer is the proportional eps plus an additive eps_K = |η|·K̂₀.
  - Pyth uses the same daily eps; the admin would calibrate on Binance-lag errors, not on Pyth's.
- **E5** runs on the training months, with the training months' daily eps. Its K̂ modes keep the actual gas price.
- **E6:**
  - The fragment gas is the final hook's: 38,520 first, 8,637 per further swap in the transaction.
  - New block-scoped checks through `ScopedHookReference(block, tx_clip, rel, net)` in the contract's orientation.
  - The old in-transaction accounting checks are unchanged (all 0 failures).
- **B.10, split cost:** under block scope a same-block split saves nothing (one κ per block). The rerun computes the cross-block split instead (`e6_cross_tx_split.py --final`, validation months). The stored figure was a same-block cross-transaction split on the test months, so there is no validation counterpart.
- **B.5:**
  - The ex-post margin is rerun on the validation tag.
  - The fee/TVL ratio is not: subgraph pool-day data exist only for June–August (`data/data/subgraph`), and the validation months would need a new download.
- **Not rerun:**
  - LVR and priority-fee analyses (unchanged, as requested);
  - E8 depth exposure (still hard-codes `e2.yml`);
  - the E2 bootstrap for the B.9 / B.5 test tables.
- **Interruption:** a connection loss interrupted E6 and E7's fragment step. Both were rerun on the same commit (a520822) from scratch; the first partial outputs were overwritten.

## Checks

- E2 smoke test: with eps = 0 the buffered rules equal the retained margin exactly; with eps > 0 they never charge more.
- E8: every as-frozen row is bit-identical to the final E2 rows (72 ideal; 8 per delayed tag, ETH/USDC).
- E6, all fragment counts and pools:
  - The reversal path across transactions always pays exactly F(final block surplus).
  - No charge is negative.
  - Splitting across transactions in one block never pays less than unsplit by more than rounding (at most 21 wei); on average it pays 1.1% to 11% more, because each transaction's bracket is clipped separately.
  - Splitting within one transaction never pays less, and pays more for 40–65 of 1,042 ETH/USDC opportunities, because the watermark keeps an intermediate peak.
- Contracts: all Foundry suites pass on the merged tree (P-net NOTES §3); the new fragment gas test passes, 13 / 13.

## Rerun with eps per reference (results/final_valid_eps)

| Commit | Step |
|---|---|
| 23a77b0 | test configuration ĝ = 202,161 (`FINAL_GAS_UNITS`); every block-scope suite passes in its supported mode (logs in `results/e7_block_scope/logs_23a77b0`); conformance 0/0/0 mismatches in the three modes; the 2,000-run fuzz passes isolated and not isolated; cold gas unchanged |
| 46f0cc2, b6ae752 | `experiments/eps_reference_calibration.py` (run on b6ae752) |
| e24a07a | every final-rule experiment uses the eps of its own reference; all reruns on this commit |
| 646c168 | comparison: per-reference eps, the d = 0 check, E4 fitted-once per d, Pyth rows |

**Per-reference eps.**
- **Error:** |P / P̂ − 1| at observed price-correcting swaps (the stored file used |P̂ / P − 1|; the two differ at order ε²). Rolling 7-day p95, daily at 00:00 UTC, strictly earlier data, rounded up to whole ppb.
- **One-sided:** max(p95 of sign(d0)·(1 − P/P̂), 0).
- **Checks:**
  - lag 0 gives 0 every day, two-sided and one-sided;
  - lag 1 under the old definition reproduces all 549 pool-days of `eps_observed_daily_valid.csv`.

Median daily eps for ETH/USDC (fitted once in brackets):

| Reference | Median daily eps (fitted once) |
|---|---|
| lag 1 | 15.0 bp (15.6) |
| lag 5 | 32.8 bp (35.5) |
| lag 10 | 45.8 bp (51.0) |
| lag 30 | 81.8 bp (88.1) |
| lag 60 | 111.3 bp (125.4) |
| Pyth, no check | 73.3 bp |
| Pyth, 60 s | 10.9 bp |
| Pyth, 300 s | 27.5 bp |
| Pyth, 1 h | 59.8 bp |

- **ETH/WBTC:** 13.2–13.5 bp at lag 1, 30–32 bp at lag 5, about 85 bp at lag 60.
- **ETH/wstETH:** 0 at every lag.
- **Coverage on observed correcting swaps:** 94–96% for every reference.
- **Coverage on replay candidates:**

| Cadence | Coverage (lags 1–60) |
|---|---|
| k = 15 | 94.5–96.8% |
| k = 5 | 90.8–93.9% |
| k = 1 | 80.5–91.4% |

  The k = 1 candidates follow large one-minute moves.
- The one-sided eps is 0 for ETH/WBTC at lags ≥ 5. Correcting swaps follow recent moves, so a lagged reference underestimates their surplus.

**Which reference each experiment uses:**

| Experiment | Reference |
|---|---|
| E2, E8 | lag d of each run |
| E4 | lag d; Pyth per staleness bound and without one; fitted-once per reference |
| E5, E6, the cross-block split | the exact reference (eps = 0) |
| B.10 threshold | lag 5 (`delta_from_e4_lag_min`) |

**d = 0 check.** At d = 0 every buffered row equals the retained margin's in every metric: 18 rule/variant combinations, each with 48 rows (all regimes, λ and γ), in all three pools.

Comparison tables: `results/final_valid_eps/comparison/*.csv`; `file_list.csv` gives each file's commit (eps_reference: b6ae752; everything else: e24a07a).
