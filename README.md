# Arbitrage-Aware Fee Sharing for Automated Market Makers

Code for the paper *"Arbitrage-Aware Fee Sharing for Automated Market Makers."* It contains:
- the Python implementation of the participation-aware transfer rule;
- the Solidity implementation as a Uniswap v4 hook, with its Foundry tests;
- the scripts that download the data;
- the experiments behind the paper's evaluation.

The repository contains no data or results. The scripts in `data/` download every input from public sources, and `run_all.sh` regenerates the evaluation from them.

## Layout

```
common/        shared library: CPMM mechanics, the transfer rule, integer reference of the hook, data loading, pools
experiments/   one script per experiment (E1-E8) plus data preparation and the figure script
  configs/     experiment configurations; *_final.yml are the frozen configurations behind the paper's numbers
config/        hook gas figures (gas_block_scope.json), frozen configuration hashes (frozen/), input-data manifest
contracts/     Foundry project: the hook (src/hooks/ParticipationAwareHook.sol), accounting libraries, tests
data/          download scripts, BigQuery queries (bigquery/) and the swap-log decoder; inputs land in data/data/
tests/         Python tests (pytest)
run_all.sh     the full pipeline, from raw data to the figures
```

## Experiments

| ID | What it measures | Script(s) |
|---|---|---|
| E1 | CPMM depth calibration of the replay, and its validation | `e1_cpmm_calibration.py` |
| E2 | Sequential replay of the test months: execution, protection funds, price error, stale references; bootstrap CIs, recoverable margin, hook gas, reservation-payoff scenarios, fees and TVL | `e2_sequential_replay.py`, `e2_bootstrap.py`, `e2_recoverable_margin.py`, `e2_hook_gas.py`, `e2_r_scenarios.py`, `fees_tvl_check.py` |
| E4 | Oracle delay and cadence, Pyth on-chain prices | `e4_oracle_robustness.py` |
| E5 | Size-dependent costs | `e5_size_dependent_costs.py` |
| E6 | Splitting a correction into fragments, within and across transactions | `e6_fragmentation.py`, `e6_cross_tx_split.py` |
| E7 | Solidity-Python conformance, gas, minimal profitable surplus | `e7_solidity_conformance.py`, `gas_cold_table.py`, Foundry suites in `contracts/test/` |
| E8 | Replayed baselines (dynamic fee, MEV tax), LVR, concentration | `e8_baselines.py`, `e8_lvr.py`, `e8_mev_tax_observed.py`, `e8_concentration.py` |

Supporting scripts:
- `eps_reference_calibration.py` computes the buffer ε per price reference.
- `freeze_final.py` writes the frozen hashes.
- `build_aligned_dataset.py` aligns swaps, prices and gas.
- `make_eval_figures.py` draws the figures.
- `eA_*`, `eC_*` and `eQ4_prop_buffer.py` are kept because `eps_reference_calibration.py` imports their helpers.

Months:
- training: before April 2026
- validation: April–May 2026
- test: 1 June – 26 July 2026

Parameters are chosen on the validation months and frozen. Test-month runs pass `--confirm-frozen`, which refuses to run if a configuration differs from its hash in `config/frozen/`.

## Setup

Python (tested with 3.13):

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Foundry: `contracts/README.md` lists the pinned compiler and the `forge install` commands for `contracts/lib` (forge-std, Uniswap v4-core v4.0.0). The fuzz and property suites call Python through `--ffi`.

Credentials: copy them into `data/.secrets.env`, which is gitignored. You can also set them as environment variables.

```
GRAPH_API_KEY=<The Graph API key>          # swaps and pool-day data from the Uniswap v4 subgraph
ETH_RPC_URL=<Ethereum mainnet RPC URL>     # fee history, Lido and Pyth events
```

## Data

| Input | Source | Script |
|---|---|---|
| Uniswap v4 swaps of the three pools | Uniswap v4 subgraph (The Graph) | `data/fetch_swaps.py` |
| Daily fees and TVL, test months | Uniswap v4 subgraph | `data/fetch_pool_day_data.py` |
| 1-minute prices (ETHUSDC, ETHBTC, ETHUSDT, BTCUSDT, USDCUSDT) | Binance public klines | `data/download_binance_klines.py` |
| wstETH/ETH rate | Lido `TokenRebased` events (RPC) | `data/download_wsteth_rate.py` |
| Pyth ETH/USD on-chain prices | Pyth `PriceFeedUpdate` events (RPC) | `data/download_pyth_onchain_events.py` |
| Per-block priority-fee percentiles | `eth_feeHistory` (RPC) | `data/download_fee_history.py` |
| Block timestamps and base fees | BigQuery `crypto_ethereum.blocks` | `data/bigquery/blocks.sql` |
| Swap transactions' receipts (gas, priority fee, builder) | BigQuery `crypto_ethereum.logs` / `transactions` | `data/bigquery/v4_swap_logs.sql`, then `data/decode_bq_swaps.py` |

Stage 0 of `run_all.sh` has the exact date ranges.

The BigQuery queries were rebuilt from the columns, filters and date ranges of the original console exports, not saved from the console. On the original swap-log export, `decode_bq_swaps.py` reproduces the parquet the experiments read, row for row.

On-chain identifiers (Ethereum mainnet). Uniswap v4 pools are identified by their ID in the PoolManager `0x000000000004444c5dc75cB358380D2e3dE08A90` (see `common/pools.py`).

| Source | Identifier |
|---|---|
| ETH/USDC 0.05% pool | `0x21c67e77068de97969ba93d4aab21826d33ca12bb9f565d8496e8fda8a82ca27` |
| ETH/WBTC 0.30% pool | `0x54c72c46df32f2cc455e84e41e191b26ed73a29452cdd3d82f511097af9f427e` |
| ETH/wstETH 0.01% pool | `0x1d5b2949ece8754c2d736991c62c5162bd144f497b2212182401b9bae77e2d76` |
| Lido stETH contract (`TokenRebased` events, wstETH rate) | `0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84` |
| Pyth contract | `0x4305FB66699C3B2702D4d05CF36551390A4c69C6` |
| Pyth ETH/USD feed ID | `0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace` |

## Running

```bash
bash run_all.sh
```

The script runs every stage in order and stops at the first error:
- 0: data downloads
- 1: aligned dataset and tests
- 2: E1
- 3: contract conformance and gas
- 4: validation calibration and freeze
- 5: test months
- 6: figures

The test-month outputs go to `results/final_test/`. Set `PY` or `FORGE` to choose the interpreter or the forge binary.

Notes:
- `config/gas_block_scope.json` holds the hook's measured gas. The experiments read it. It was transcribed from the output of `gas_cold_table.py` (stage 3); compare the two after a rerun.
- `contracts/test/E7Conformance.t.sol` reads `results/e7/vectors.json`, which stage 3 writes. Set `E7_VECTORS` to use another file.

Tests on their own:

```bash
python -m pytest tests -q
cd contracts && forge test --offline --ffi    # the gas suites also run with --isolate; see stage 3 of run_all.sh
```
