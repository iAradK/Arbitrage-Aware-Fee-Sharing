# Arbitrage-Aware Fee Sharing for Automated Market Makers

Code and data for the paper *"Arbitrage-Aware Fee Sharing for Automated Market
Makers."* The repository contains the synthetic and historical experiments
behind the paper's evaluation section, plus the Solidity/Python implementation
of the proposed surplus-sharing mechanism.

## Paper-to-code mapping

Section 5 (Evaluation) of the paper has five deliverables, each backed by a
script in this repository:

| Paper item | What it measures | Primary script(s) |
|---|---|---|
| §5.1 / Fig 2 | Synthetic retained-margin (γ) frontier: LP recovery, trade quality, price tracking, at λ=0.75, x0=1000/y0=1e6 CPMM | `experiments/synthetic_frontier/run_synthetic_recovery_incentive_frontier.py` |
| §5.2.1 / Fig 3 | Historical fixed-candidate replay, daily ETH/USD Apr 2021–2026, λ sweep, 3 rules | `experiments/historical_fixed_candidate/run_trace_driven_replay_gamma.py` |
| §5.2.2 / Fig 4 | Trace-calibrated LP outcomes, >100k real Uniswap v4 swaps Jan–Mar 2026 | `contracts/data_prep/run_lp_outcome_daily.py` |
| §5.3 / Fig 5 | Oracle-staleness robustness, Pyth vs Binance, 1–60 min delays | `experiments/oracle_staleness/run_stale_oracle_robustness.py` |
| §5.4 / Table 1 | Solidity↔Python parity (128 cases), gas benchmarks, fragmentation resistance | `contracts/src/*.sol`, `contracts/python/*.py` |

## Repository structure

```
experiments/   one subfolder per Section-5 deliverable (synthetic + historical)
common/        shared Python library (CPMM mechanics, replay helpers)
data/          shared input data and acquisition scripts
contracts/     Foundry + Python implementation of the fee-sharing mechanism
archive/       superseded or orphaned artifacts, kept for provenance
results/       generated output (gitignored)
```

### `experiments/`

Each subfolder is self-contained: driver script(s) plus a `results/` output
directory. Scripts are run from their own directory.

| Folder | Script(s) | Paper mapping | Notes |
|---|---|---|---|
| `synthetic_frontier/` | `run_synthetic_recovery_incentive_frontier.py` | §5.1 / Fig 2 | Self-contained, no external data inputs. |
| `synthetic_price_shock/` | `synthetic_cpmm_price_shock.py` | Adjacent to §5.1, sweeps λ not γ | Self-contained. |
| `historical_fixed_candidate/` | `run_trace_driven_replay_gamma.py` | §5.2.1 / Fig 3 | Reads `data/exp_data/`; imports `common/cpmm_replay_common.py`. |
| `oracle_staleness/` | `run_stale_oracle_robustness.py` | §5.3 / Fig 5 | Reads `data/pyth_eth_usd_tradingview_.../` and `data/exp_data/pool_data_2026_01_to_04/`. |
| `gas_robustness/` | `run_gas_aware_replay.py`, `run_required_payoff_gas_error.py` | Appendix material, uses 2021-era data | Reads `data/exp_data/`; imports `common/cpmm_replay_common.py`. The second script post-processes the first's `results/gas_robustness_events.csv`. |

### `common/`

- `cpmm_replay_common.py` — CPMM mechanics, γ-aware `sharing_amount`/`participation_aware`
  rule, and trace-replay helpers (`RULES = (baseline, unconstrained, participation_aware)`).
  Imported by the historical and gas-robustness experiments via an explicit
  `sys.path` insert.

### `data/`

| Item | Purpose |
|---|---|
| `exp_data/` | Binance ETH/FTT/USDC CSVs, AAVE yield data, `eth_usd_5y.csv`, gas CSVs, and per-pool JSON data (2021-era and 2026-era). |
| `pyth_eth_usd_tradingview_2026_01_01_to_2026_04_01_1min/` | Full-resolution Pyth 1-min data, Binance monthly CSVs, and merged oracle-vs-Binance error data. |
| `poolday_graph_response.json` | Raw two-pool GraphQL response, split by `gather_pool_data.py`. |
| `gather_pool_data.py` | Splits `poolday_graph_response.json` into per-pool JSON files. |
| `prepare_external_prices.py` | Merges 1-min Pyth + Binance data, computes oracle error, synthesizes staleness delays. |
| `collect_pyth_data.py` | Downloads Pyth ETH/USD 1-min candles with checkpointing/resume. |

### `contracts/`

Foundry + Python subproject implementing the transfer function `F` and its
transaction-scoped watermark rule. See [`contracts/README.md`](contracts/README.md)
for the mechanism description and full usage instructions (installing
dependencies, generating replay vectors, running the Solidity conformance
tests, and running the fragmentation experiment).

- `src/` — `SurplusSharingAccounting.sol` (stateless implementation of `F`) and
  `CumulativeSurplusAccounting.sol` (Cancun-EVM prototype using TSTORE/TLOAD
  for the transaction-scoped watermark rule, §3.5).
- `test/` — Foundry test suites for both contracts.
- `python/` — `hook_replay.py` (generates atomic replay vectors) and
  `fragmentation_replay.py` (the fragmentation-resistance experiment, Table 1).
- `data_prep/` — acquisition/prep scripts for the Uniswap v4 swap data used in
  §5.2.2, including `run_lp_outcome_daily.py`, the primary script for that figure.
- `data/` — loose input files for the contracts subproject (v4 swaps, gas and
  price CSVs).
- `results/` — generated output, including `lp_outcomes_v4_auto_r/` (§5.2.2)
  and `fragmentation/` (Table 1).

### `archive/`

Superseded or orphaned data/results kept for provenance rather than deleted —
see the folder for details on each item.

## Setup

Python dependencies (used across `experiments/`, `common/`, and `data/`):

```bash
python -m pip install numpy pandas matplotlib
```

For the `contracts/` subproject (Foundry + Python), see
[`contracts/README.md`](contracts/README.md).

## Running an experiment

Each experiment script is run from within its own directory, e.g.:

```bash
cd experiments/synthetic_frontier
python run_synthetic_recovery_incentive_frontier.py
```

Output is written to a `results/` folder alongside the script.