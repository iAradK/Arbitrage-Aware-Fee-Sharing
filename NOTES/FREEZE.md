# Freeze of the final configuration (2026-10-08)

**Frozen state: the commit that adds this file.** It contains `74167e4` (the configuration the user named) plus:
- `4b388e4`, the test-month enablement, which changes no parameter;
- the frozen hashes in `config/frozen/final/`.

`74167e4` itself could not run the test months:
- every final-rule script refused them;
- no eps existed for the test days;
- the cross-block split ran on validation only;
- the E6 next-block statistics had no code.

The user chose to add the enablement and then freeze (2026-10-08). The test months are run once, from this commit, with `--confirm-frozen`; nothing is changed afterwards. Outputs go to `results/final_test/` (`RESULTS_RUN=final_test`).

## What `--confirm-frozen` checks

`reporting.guard_final` refuses a test-month run unless the config's hash equals `config/frozen/final/<exp>.sha256` (written by `experiments/freeze_final.py --cal-root final_valid_eps`):

| exp | config | frozen hash |
|---|---|---|
| e2 | experiments/configs/e2_final.yml | 6862e53f3e73108b146985f0a7598620f7ed71ffc2299e24a6ee1c53d1b08aee |
| e4 | experiments/configs/e4_final.yml | 5da995df380940243be33a708b67969249ba36d40364bdcbacd6d3b26bd36c4e |
| e6 | experiments/configs/e6_final.yml | 220ab2b6aa40ef0cb6c0e481a54e601cc5b65c408e9e3e26ff8e91aa88744f64 |
| e7 | experiments/configs/e7_final.yml | e84991e9bfa3a198078406f9ea721fa27091b2cccf280ee36c0d3658d8e6fc3f |
| e8 | experiments/configs/e8_final.yml + e2_final.yml hash + calibrated betas | 79a7221f7f16348689933ce8067d608f8ffb52fa071c647abfd9aa2c30d53ab5 |

E5 runs on the training months only (as always); its config is `experiments/configs/e5_final.yml`.

## Frozen files (git blob / sha256 prefix)

| File | git blob | sha256 |
|---|---|---|
| experiments/configs/e2_final.yml | 01aafe2ab261 | cb313891791cfe1f |
| experiments/configs/e4_final.yml | 2fd707addb88 | 67d3f7db0c483619 |
| experiments/configs/e5_final.yml | 5bfa214375af | 888296bd488ee3b9 |
| experiments/configs/e6_final.yml | 8d1071d2b004 | 0da4656e6b10c5bd |
| experiments/configs/e7_final.yml | 1eaae330bb24 | 2d86acd4ef8b3411 |
| experiments/configs/e8_final.yml | fa3b96aa8951 | 384dcf774eeac5ef |
| config/gas_block_scope.json | e21a32b4f2d6 | 1b62f5ac3ae7183d |
| contracts/src/hooks/ParticipationAwareHook.sol | 1970925ce0f8 | 22e10ab92648f3fb |
| common/fixedpoint.py | ae72305c2146 | 76f092fb4c6a621d |
| experiments/eps_reference_calibration.py | a32eec219f38 | f916f936dab73e0b |
| results/final_valid_eps/eps_reference/eps_ref_daily_valid.csv (untracked; run on b6ae752) | | 76dc6c9a692bf3da |
| results/final_valid_eps/eps_reference/eps_ref_fitted_once_valid.csv (untracked; run on b6ae752) | | 449d6bcfe7766f32 |
| results/final_valid_eps/e8/dynfee_calibration.json (untracked; run on e24a07a) | | 1760a4d5038da37d |

## Frozen parameters

**Hook (contracts = cab9c14):**
- block scope with per-transaction clipping;
- proportional buffer ⌈ε π̂ |X₁|⌉ on each transaction's net token1 change;
- κ = K̂ once per block;
- τ̂ = 3 gwei;
- `gasPriceToken0Wad` = 1e18;
- the test configuration's ĝ = 202,161.

**Rule:**
- λ = 0.75, γ = 0.02 (headline);
- E4 and E6 keep their own γ = 0.05;
- grids as in the configs.

**Gas:**

| Case | Gas |
|---|---|
| uncharged correction (first swap of a block, surplus changes) | 38,520 |
| charged | 52,161 |
| charged, empty vault (sensitivity) | 69,261 |
| second transaction, same block | 19,832 |
| second swap in a transaction | 8,637 |
| extra transaction, new block | 159,556 / 173,197 |
| ĝ (gas_units + charged) | 150,000 + 52,161 = 202,161 |

**eps.**
- **Rule:** per reference, daily at 00:00 UTC, the rolling 7-day p95 of |P / P̂ − 1| over observed price-correcting swaps stamped strictly before, rounded up to whole ppb.
- **References:** the benchmark delayed by d (lag d), Pyth on-chain per staleness bound, the exact reference (eps = 0) for E5, E6 and the cross-block split.
- **Test months:** the same rule continued, each day from the 7 days strictly before it (`eps_reference_calibration.py --split test --confirm-frozen`).
- **E4's "fitted once on validation":** the validation values in `eps_ref_fitted_once_valid.csv`.

**E8:**
- dynamic-fee betas calibrated on the validation months: ETH/USDC 0.311328, ETH/WBTC 0.134961, ETH/wstETH 0.176563;
- the one-sided buffer is not run (`skip_families: [onesided]`).

**R:** median train regime (low / high for B.9), unchanged.

## Test-month run order

**Core:**
1. eps calibration over the test months.
2. E2: ideal (all rules, all three pools, three R regimes, the lambda sweep and the B.4 grid), the delayed grid for ETH/USDC; ex-post margin (B.5); fee/TVL ratios (`fees_tvl_check.py`, June–July subgraph data).
3. E4: the d × k grid, the eta grid, Pyth with the three staleness bounds (and without).
4. E6: equal splits, reversal paths, the within-block split across transactions; the cross-block split cost with the next-block statistics.
5. E7 / B.10: per-fragment gas (`--fragments`) and the charging threshold (`--smin`).

**Secondary:**

6. E8: dynamic fee (validation betas recomputed in the run root; must equal the frozen ones), settlement sensitivity, LVR (`e8_lvr.py`), priority fees (`e8_mev_tax_observed.py`), concentration.
7. E5 (training months).
8. B.8 (`e2_hook_gas.py`) and B.9 (`e2_bootstrap.py` per regime, `e2_r_scenarios.py`); B.4 comes from the E2 ideal run.

If any step fails, the run stops and is reported; nothing is fixed and rerun.

## Incident during the test run (2026-10-08)

**What failed.**
- The run on f0e46a9 stopped at CORE step 1, at the B.5 fee/TVL ratio (`experiments/fees_tvl_check.py`).
- The error was `FileNotFoundError` on `data/data/subgraph/pool_day_data_jun_jul_2026.json`.
- Every step before it had completed with exit 0: the test eps, the E2 ideal replay and the delayed grid, the E2 bootstraps, and the ex-post margin.

**Cause.**
- The worktree's `data/data` folder did not link the main checkout's `subgraph` folder. `gas` and `lido` are directory junctions to the main checkout; `subgraph` had no link.
- A check of the remaining steps found the same gap for `data/data/bq`, read by `e8_mev_tax_observed.py` (priority fees).

**Fix (link only, approved by the user).**
- `data/data/subgraph` and `data/data/bq` were added as directory junctions to the main checkout, like `gas` and `lido`.
- No code, config or parameter changed. The tree was clean at f0e46a9 before the run resumed.
- `config/data_manifest.json` has no hash for either file, so their SHA-256 is recorded here:

| File | SHA-256 |
|---|---|
| `data/data/subgraph/pool_day_data_jun_jul_2026.json` | c5e07c61c80d7a397603fd049c98e48bc420d3ba1233db2484fe04730fd022d8 |
| `data/data/bq/swap_logs_decoded.parquet` | dd2a01c0f36144231200e4d548d29e97573c2404f55dbe833876517bd390b479 |

**Resumption.**
- The run resumed at the fee/TVL step, then ran E4, E6, E7/B.10, E8, E5, B.8 and B.9 unchanged.
- No completed step was rerun.
- Every result is from f0e46a9. This note is a separate NOTES-only commit, made after the run so that the result manifests keep f0e46a9.
