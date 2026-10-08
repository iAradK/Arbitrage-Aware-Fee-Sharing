#!/bin/bash
# Reproduce the paper's evaluation from raw data. Run from the repository root.
#
# Prerequisites (see README.md):
#   - Python environment with requirements.txt
#   - Foundry with contracts/lib installed (forge install, see contracts/README.md)
#   - credentials in data/.secrets.env (or the environment): GRAPH_API_KEY, ETH_RPC_URL
#   - Google BigQuery access for stage 0b
#
# Months: train before 2026-04-01, validation April-May 2026, test 2026-06-01 to 2026-07-26 (common/pools.py).
# Parameters are calibrated on the validation months and frozen; config/frozen/ holds the frozen hashes. The test
# months run with --confirm-frozen, which refuses to run if a config no longer matches its frozen hash.
#
# Override PY or FORGE to choose the interpreter or the forge binary (e.g. a WSL wrapper on Windows).
set -euo pipefail
PY=${PY:-python}
FORGE=${FORGE:-forge}
ROOT=$(pwd)

# ============================================================== 0a. Raw data (scripts; write under data/)
# The download scripts take paths relative to data/, so they run from there.
(
  cd data
  $PY fetch_swaps.py --start 2025-07-01 --end 2026-09-01 --pools eth_usdc_005 eth_wbtc_030 eth_wsteth_001   # The Graph -> swaps_data/
  $PY download_binance_klines.py --symbols ETHUSDC ETHBTC ETHUSDT BTCUSDT USDCUSDT --start 2025-07-01 --end 2026-09-01   # -> data/binance/
  $PY download_wsteth_rate.py --start 2025-07-01 --end 2026-09-01           # Lido TokenRebased -> data/lido/
  $PY download_pyth_onchain_events.py --start 2025-09-01 --end 2026-09-01    # Pyth ETH/USD updates -> data/pyth/
  $PY download_fee_history.py --start 2025-07-01 --end 2025-09-01 --out data/gas/fee_history_2025_07_08.csv   # tips (RPC eth_feeHistory)
  $PY download_fee_history.py --start 2025-09-01 --end 2026-08-01 --out data/gas/fee_history_head.csv
)
$PY data/fetch_pool_day_data.py                    # daily fees/TVL of the test months -> data/data/subgraph/

# ============================================================== 0b. Raw data (BigQuery, run by hand)
# Run data/bigquery/blocks.sql (for 2025-07-01..2025-09-01 and 2025-09-01..2026-09-01) and save each result as
# data/data/gas/bq-results-<name>.csv. Run data/bigquery/v4_swap_logs.sql, save it under data/data/bq/, then:
#   $PY data/decode_bq_swaps.py data/data/bq/<swap-log export>.csv
# (data/bigquery/pyth_eth_usd_logs.sql is an optional completeness check of the Pyth download.)

# ============================================================== 1. Aligned dataset, tests
$PY experiments/build_aligned_dataset.py           # -> cache/aligned/*.parquet, cache/block_gas.parquet, results/data_qc.md
$PY experiments/make_data_manifest.py              # -> config/data_manifest.json (input hashes, copied into every run manifest)
$PY -m pytest tests -q

# ============================================================== 2. E1: CPMM depth calibration (RESULTS_RUN unset: later
# steps read results/e1/ at a fixed path)
$PY experiments/e1_cpmm_calibration.py --split valid
$PY experiments/e1_cpmm_calibration.py --split test --confirm-frozen

# ============================================================== 3. Contract conformance and gas (pre-final chain)
# The hook's gas figures in config/gas_block_scope.json come from this chain. The old E4 eps feeds the E7 vectors,
# E7 measures gas in Foundry, and gas_cold_table.py writes results/final_valid/gas/gas_cold.csv.
# config/gas_block_scope.json was copied by hand from that table; compare the two after a rerun.
$PY experiments/e4_oracle_robustness.py --split valid
$PY experiments/e7_solidity_conformance.py --vectors --forge     # -> results/e7/vectors.json (read by E7Conformance.t.sol)
$PY experiments/e7_solidity_conformance.py --smin
RESULTS_RUN=final_valid $PY experiments/gas_cold_table.py
(
  cd contracts
  # Foundry suites. --isolate measures per-transaction gas, but isolated calls see block.basefee = 0, so the
  # base-fee suites run without it. The fuzz and property suites call Python through --ffi.
  $FORGE test --offline --isolate --match-contract '^BlockScopeConformanceTest$'
  $FORGE test --offline --match-contract '^BlockScopeFuzzTest$' --ffi
  $FORGE test --offline --isolate --match-contract '^(BlockScopedHookTest|BlockScopeBoundaryTest|BlockScopeV1PropertiesTest|BlockScopedHookScenariosTest)$' --ffi
  $FORGE test --offline --match-contract '^BlockScopeBoundary' --ffi
  $FORGE test --offline --match-contract '^ParticipationAwareHookFeeTest$' --ffi
  $FORGE test --offline --isolate --match-contract '^BlockScopedHookGasTest$'
  $FORGE test --offline --match-contract '^(TxScopedParticipationAwareHookTest|HookGasIsolatedTest|HookGasSettledTest)$'
)

# ============================================================== 4. Validation-month calibration, then freeze
RESULTS_RUN=final_valid_eps $PY experiments/eps_reference_calibration.py --split valid                 # eps per reference
RESULTS_RUN=final_valid_eps $PY experiments/e8_baselines.py --config experiments/configs/e8_final.yml --calibrate   # dynamic-fee beta
$PY experiments/freeze_final.py --cal-root final_valid_eps      # -> config/frozen/final/*.sha256

# ============================================================== 5. Test months, once (RESULTS_RUN=final_test)
export RESULTS_RUN=final_test
# The test-month eps tables continue the validation ones; the test runs read both from the run root.
mkdir -p results/final_test/eps_reference
cp results/final_valid_eps/eps_reference/eps_ref_daily_valid.csv \
   results/final_valid_eps/eps_reference/eps_ref_fitted_once_valid.csv results/final_test/eps_reference/
$PY experiments/eps_reference_calibration.py --split test --confirm-frozen

E2="--config experiments/configs/e2_final.yml --split test --confirm-frozen"
for D in 0 1 5; do for K in 1 5 15; do                         # stale reference (lag d) x correction cadence (k)
  $PY experiments/e2_sequential_replay.py $E2 --pools eth_usdc_005 --lag $D --cadence $K --median-only
done; done
$PY experiments/e2_sequential_replay.py $E2                    # all pools and reservation-payoff regimes
$PY experiments/e2_bootstrap.py --tag test
for D in 0 1 5; do for K in 1 5 15; do
  T="test_lag${D}"; [ $K != 1 ] && T="${T}_k${K}"; $PY experiments/e2_bootstrap.py --tag "${T}_med"
done; done
$PY experiments/e2_recoverable_margin.py --tag test
$PY experiments/fees_tvl_check.py

$PY experiments/e4_oracle_robustness.py --config experiments/configs/e4_final.yml --split test --confirm-frozen
$PY experiments/e6_cross_tx_split.py --final --split test --confirm-frozen
$PY experiments/e6_fragmentation.py --config experiments/configs/e6_final.yml --split test --confirm-frozen
$PY experiments/e7_solidity_conformance.py --config experiments/configs/e7_final.yml --fragments
$PY experiments/e7_solidity_conformance.py --config experiments/configs/e7_final.yml --smin --split test --confirm-frozen

$PY experiments/e8_baselines.py --config experiments/configs/e8_final.yml --calibrate    # must reproduce the frozen beta
$PY experiments/e8_baselines.py --config experiments/configs/e8_final.yml --split test --confirm-frozen
$PY experiments/e8_lvr.py --split test
$PY experiments/e8_mev_tax_observed.py --split test
$PY experiments/e8_concentration.py --tag test

$PY experiments/e5_size_dependent_costs.py --config experiments/configs/e5_final.yml     # training months, exact reference
$PY experiments/e2_hook_gas.py --config experiments/configs/e2_final.yml --tag test
for RG in low high; do $PY experiments/e2_bootstrap.py --tag test --regime $RG; done
$PY experiments/e2_r_scenarios.py --tag test

# ============================================================== 6. Figures
$PY experiments/make_eval_figures.py --run final_test --figs eval_replay,eval_heat,eval_costs,eval_sens --out results/final_test/figures
unset RESULTS_RUN
echo "done: outputs in $ROOT/results/final_test"
