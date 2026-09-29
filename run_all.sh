#!/bin/bash
# Reproduce everything from the raw inputs (v2, DECISIONS section F). Run from the repo root. Windows: use Git Bash; forge runs through WSL (see E7).
# Prerequisites: .venv with requirements.txt installed; data/ populated (see results/data_manifest.json).
set -euo pipefail
PY=./.venv/Scripts/python.exe
$PY -m pytest tests -q
$PY experiments/legacy_check.py --legacy
$PY experiments/build_aligned_dataset.py                       # cache/aligned/*.parquet, results/data_qc.md

# --- development on train + validation months
$PY experiments/e1_cpmm_calibration.py --split valid
$PY experiments/e4_oracle_robustness.py --split valid          # eps_S(d) feeds the E7 vectors (hook overhead is a constant in the configs)
$PY experiments/e7_solidity_conformance.py --vectors --forge   # asserts the measured overhead equals hook_overhead_gas in the configs
$PY experiments/e5_size_dependent_costs.py                     # train months only
$PY experiments/e2_sequential_replay.py --split valid
for D in 0 1 5; do for K in 1 5 15; do $PY experiments/e2_sequential_replay.py --split valid --lag $D --cadence $K --median-only; done; done   # v2
$PY experiments/e3_pareto_frontier.py --tag valid
$PY experiments/e6_fragmentation.py --split valid
$PY experiments/e7_solidity_conformance.py --smin

# --- freeze, then evaluate the test months once
for E in e1 e2 e4 e6 e7; do case $E in e1) S=e1_cpmm_calibration;; e2) S=e2_sequential_replay;; e4) S=e4_oracle_robustness;; e6) S=e6_fragmentation;; e7) S=e7_solidity_conformance;; esac; $PY experiments/$S.py --freeze; done
$PY experiments/e1_cpmm_calibration.py --split test --confirm-frozen
$PY experiments/e2_sequential_replay.py --split test --confirm-frozen
for D in 0 1 5; do for K in 1 5 15; do
  $PY experiments/e2_sequential_replay.py --split test --confirm-frozen --lag $D --cadence $K --median-only    # v2
  T="test_lag${D}"; [ $K != 1 ] && T="${T}_k${K}"; $PY experiments/e2_bootstrap.py --tag "${T}_med"
done; done
for T in test test_lag1_med test_lag5_med test_lag1_k5_med test_lag1_k15_med test_lag5_k15_med; do $PY experiments/e3_pareto_frontier.py --tag $T; done
$PY experiments/e4_oracle_robustness.py --split test --confirm-frozen
$PY experiments/e6_fragmentation.py --split test --confirm-frozen
$PY experiments/e7_solidity_conformance.py --smin --split test --confirm-frozen
