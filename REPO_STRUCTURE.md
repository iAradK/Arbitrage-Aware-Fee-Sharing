# Repository Structure Map

This document maps the repository for the paper *"Arbitrage-Aware Fee Sharing for
Automated Market Makers."* It reflects the **post-refactor layout**: root files are
now organized into `experiments/`, `common/`, `data/`, `contracts/`, and `archive/`.
(A prior version of this document mapped the pre-refactor, flat-root layout as
groundwork for this reorganization; that content is superseded by what follows.)

## Overview

Section 5 (Evaluation) of the paper has five deliverables, used as the organizing
anchor for `experiments/` and `contracts/`:

| Paper item | What it measures | Primary script(s) |
|---|---|---|
| §5.1 / Fig 2 | Synthetic retained-margin (γ) frontier: LP recovery, trade quality, price tracking, at λ=0.75, x0=1000/y0=1e6 CPMM | `experiments/synthetic_frontier/run_synthetic_recovery_incentive_frontier.py` |
| §5.2.1 / Fig 3 | Historical fixed-candidate replay, daily ETH/USD Apr 2021–2026, λ sweep, 3 rules | `experiments/historical_fixed_candidate/run_trace_driven_replay_gamma.py` |
| §5.2.2 / Fig 4 | Trace-calibrated LP outcomes, >100k real Uniswap v4 swaps Jan–Mar 2026 | `contracts/data_prep/run_lp_outcome_daily.py` |
| §5.3 / Fig 5 | Oracle-staleness robustness, Pyth vs Binance, 1–60 min delays | `experiments/oracle_staleness/run_stale_oracle_robustness.py` |
| §5.4 / Table 1 | Solidity↔Python parity (128 cases), gas benchmarks, fragmentation resistance | `contracts/src/*.sol`, `contracts/python/*.py` |

---

## 1. `experiments/` — one subfolder per Section-5 deliverable

Each subfolder is self-contained: the driver script(s) plus a `results/` output
directory (gitignored). Scripts are invoked from their own directory; paths to
shared data (`data/`) and shared code (`common/`) are resolved relative to the
script's own location (`__file__`-anchored or an explicit relative default), not the
caller's working directory.

| Folder | Script(s) | Paper mapping | Notes |
|---|---|---|---|
| `synthetic_frontier/` | `run_synthetic_recovery_incentive_frontier.py` | §5.1 / Fig 2 | Self-contained, no external data inputs. Renamed from `recovery–incentive frontier.py` to match its own docstring's self-reference. |
| `synthetic_price_shock/` | `synthetic_cpmm_price_shock.py` | Adjacent to §5.1, sweeps λ not γ; no confirmed figure mapping | Self-contained. Renamed from `Synthetic_CPMM_price_shock.py` (snake_case). |
| `historical_fixed_candidate/` | `run_trace_driven_replay_gamma.py` | §5.2.1 / Fig 3 | Reads `data/exp_data/`; imports `common/cpmm_replay_common.py`. |
| `oracle_staleness/` | `run_stale_oracle_robustness.py` | §5.3 / Fig 5 | Reads `data/pyth_eth_usd_tradingview_.../` and `data/exp_data/pool_data_2026_01_to_04/`. |
| `gas_robustness/` | `run_gas_aware_replay.py`, `run_required_payoff_gas_error.py` | Ambiguous — doesn't map to a named figure, uses 2021-era data; possibly appendix material. Kept isolated pending author confirmation. | Reads `data/exp_data/`; imports `common/cpmm_replay_common.py`. `run_required_payoff_gas_error.py` post-processes the first script's `results/gas_robustness_events.csv`. |

---

## 2. `common/` — shared library

- `cpmm_replay_common.py` — CPMM mechanics, γ-aware `sharing_amount`/`participation_aware`
  rule, trace-replay helpers (`RULES = (baseline, unconstrained, participation_aware)`).
  Imported by `experiments/historical_fixed_candidate/run_trace_driven_replay_gamma.py`
  and `experiments/gas_robustness/run_gas_aware_replay.py` via an explicit `sys.path`
  insert (it is no longer a sibling file to those scripts).

---

## 3. `data/` — shared inputs and acquisition scripts

| Item | Purpose |
|---|---|
| `exp_data/` | Binance ETH/FTT/USDC CSVs, AAVE yield data, `eth_usd_5y.csv`, gas CSVs, `pool_data/` (2021-era) and `pool_data_2026_01_to_04/` (2026-era) pool JSONs. Mixed-purpose, no per-experiment subdivision. |
| `pyth_eth_usd_tradingview_2026_01_01_to_2026_04_01_1min/` | Full-resolution Pyth 1-min data, Binance monthly CSVs, daily chunks, merged oracle-vs-Binance error data (`external_price_data/`). |
| `poolday_graph_response.json` | Raw two-pool GraphQL response; read only by `gather_pool_data.py`. |
| `gather_pool_data.py` | Splits `poolday_graph_response.json` into per-pool JSON files under `exp_data/pool_data_2026_01_to_04/`. |
| `prepare_external_prices.py` | Merges 1-min Pyth + Binance data, computes oracle error, synthesizes staleness delays. |
| `collect_pyth_data.py` | Downloads Pyth ETH/USD 1-min candles with checkpointing/resume. |

These three scripts use `__file__`-anchored or same-directory-relative paths and
needed no edits when moved here as a group — only their distance to sibling data
mattered, and that was preserved.

---

## 4. `contracts/` — Foundry + Python subproject (renamed from `hook_level_replay/`)

### 4.1 Documented package layout (per `contracts/README.md`)

- `src/SurplusSharingAccounting.sol` — stateless implementation of the transfer function `F`.
- `src/CumulativeSurplusAccounting.sol` — Cancun-EVM prototype using TSTORE/TLOAD for the transaction-scoped watermark rule (§3.5).
- `test/SurplusSharingAccounting.t.sol`, `test/CumulativeSurplusAccounting.t.sol` — Foundry test suites.
- `python/hook_replay.py` — generates atomic vectors into `results/` (`test_vectors.json`, `python_vectors.csv`, `hook_replay_events.csv`, `hook_replay_summary.csv`); can merge in `--solidity-gas-csv`.
- `python/fragmentation_replay.py` — the fragmentation-resistance experiment (Table 1 source), outputs to `results/fragmentation/`.
- `foundry.toml` — Solidity 0.8.24, `evm_version = "cancun"`, `via_ir = true`.
- `example/events.csv` — fixture used by the README's quickstart run.

The whole subtree (`src/`, `test/`, `python/`, `README.md`, `foundry.toml`,
`example/`) moved as a unit with no internal restructuring, so `foundry.toml`'s
`fs_permissions` on `./` and the README's documented relative commands keep working
unchanged from the new `contracts/` root.

### 4.2 `data_prep/` — undocumented acquisition/prep scripts (moved out of the root)

| Script | Purpose | Notes |
|---|---|---|
| `fetch_v4_swaps.py` | Paginates a Uniswap v4 subgraph `swaps` query, with retry/backoff | `--out` is required with no default; no path edit needed on relocation. |
| `align_swaps.py` | Merges paginated raw GraphQL JSON responses into one deduped, sorted swap list | Previously had a broken hardcoded input (`15_days_univ4_swaps.json`, nonexistent); fixed during the refactor to point at `../data/jan_univ4_swaps.json` → `../data/all_swaps.json`. |
| `build_hook_vectors.py` | Builds hook-replay vectors from a v4 pool-day JSON + Pyth CSV + Binance klines + gas CSV + ETH-USD CSV | `--output`/`--diagnostics` default to `../data/hook_events.csv` / `../data/hook_events_diagnostics.csv`; other inputs are required args with no default. |
| `prepare_v4_daily_csvs.py` | Converts a v4 `poolDayDatas` JSON into `v4_pool_daily_cpmm.csv` / `v4_nextday_price.csv` | `--out-dir` default is `../data`. |
| `measure_solidity_gas.py` | Compiles `SurplusSharingAccounting.sol` in-memory, benchmarks `computeTransfer` gas | Defaults now point at `../src/SurplusSharingAccounting.sol`, `../results/test_vectors.json`, `../results/solidity_gas.csv`. |
| `run_lp_outcome_daily.py` | **Primary §5.2.2 script.** Daily LP-outcome replay across baseline / fixed-fee tiers / unconstrained / strict cap / buffered rule, with optional R-quantile calibration | Module-level constants (`POOL_CSV`, `PRICE_CSV`, `GAS_CSV`, `SWAP_JSON`) now prefixed `../data/`; `OUT_DIR` now `../results/lp_outcomes_v4_auto_r`. Resolves paths via a helper that checks cwd first, falling back to script-directory-relative — consistent with `run_gas_aware_replay.py`'s pattern in `experiments/`. |

### 4.3 `data/` — loose input files

`ETHUSDT-1m-2026-{01,02,03}.csv`, `eth_usd_5y.csv`, `ether_gas_4y.csv`,
`pyth_eth_usd_2026_01_01_to_2026_04_01_1min.csv`, `all_swaps.json`,
`jan_univ4_swaps.json`, `uni_v4_1y_2526.json`, `v4_nextday_price.csv`,
`v4_pool_daily_cpmm.csv`, `hook_events.csv`, `hook_events_diagnostics.csv`.

### 4.4 `results/` — single flat convention (resolves prior `results/` vs `results_updated/` split)

Canonical contents (moved from the prior checkout's `results_updated/`, confirmed
freshest / same-day as the latest `src/*.sol` edits): `test_vectors.json`,
`python_vectors.csv`, `hook_replay_events.csv`, `hook_replay_summary.csv`.

**Known gap**: `solidity_gas.csv` and `cumulative_solidity_gas.csv` are *not* present
in this canonical set — they only exist in the archived quickstart snapshot
(`archive/contracts_results_quickstart/`). This is a pre-existing data gap carried
over from before the refactor (the canonical `results_updated/` checkout never had
these two files), not something the refactor caused. Re-run
`data_prep/measure_solidity_gas.py` to regenerate them here if needed.

- `results/lp_outcomes_v4_auto_r/` — `run_lp_outcome_daily.py`'s output (§5.2.2).
- `results/fragmentation/` — `python/fragmentation_replay.py`'s output (Table 1), if generated.

### 4.5 Foundry scaffolding

`cache/`, `out/`, `lib/forge-std` — standard build artifacts, gitignored.

---

## 5. `archive/` — orphaned, superseded, or ambiguous-provenance items (kept, not deleted)

| Path | What it was | Why archived |
|---|---|---|
| `root_uni_v4_json/uni_v4.json` | Raw single-pool GraphQL response at the old repo root | Orphaned — no script referenced it; likely superseded by `data/poolday_graph_response.json`. |
| `pyth_eth_usd_download/` | Smaller/older Pyth pull (`checkpoints/`, `download.log`, tiny CSV) | Orphaned — no producing script found; superseded by the tradingview pipeline in `data/`. |
| `contracts_uni_v4_json/uni_v4.json` | Raw single-pool GraphQL response from the old `hook_level_replay/` root | Superseded by `contracts/data/uni_v4_1y_2526.json`. |
| `contracts_results_swap_level_replay/` | Per-swap granularity results (28,999 events, 290MB events CSV) | Orphaned — no matching script anywhere in the repo; provenance unconfirmed. |
| `contracts_results_quickstart/` | Old `hook_level_replay/results/` — quickstart run + `hook_replay/` (32-event, modeled gas) + `hook_replay_measured/` (32-event, measured gas) | Superseded by the canonical `contracts/results/` (from `results_updated/`), but `hook_replay_measured/`'s numbers may be more representative for the paper's gas figures than whatever ended up canonical — worth a second look. Also the only surviving copy of `solidity_gas.csv`/`cumulative_solidity_gas.csv` (see §4.4). |

---

## 6. Deleted (confirmed dead, not archived)

- `cpmm_replay_common_old.py` — pre-γ version of `common/cpmm_replay_common.py`; not imported anywhere.
- `contracts/test/CumulativeSurplusAccounting.t.sol:4:1:`, `contracts/test/SurplusSharingAccounting.t.sol:4:1:` — 0-byte junk files, classic signature of a compiler-diagnostic line accidentally used as a filename.

---

## 7. Still-open items (carried over from the pre-refactor mapping, not resolved by this reorganization)

These require author judgment, not just file moves:

- `contracts_results_swap_level_replay/` — unclear provenance (§5).
- `contracts_uni_v4_json/uni_v4.json` vs `contracts/data/uni_v4_1y_2526.json` — confirm the latter is indeed authoritative.
- `experiments/gas_robustness/` — confirm whether this pipeline is still in use (appendix material?) or should be dropped from the active tree.
- `contracts/results/` missing `solidity_gas.csv`/`cumulative_solidity_gas.csv` (§4.4) — re-run `measure_solidity_gas.py` if the paper's gas figures depend on these.
- `hook_replay_measured/` (now archived) vs the canonical `results/hook_replay/` numbers — confirm which gas figures (modeled vs measured) the paper actually cites.
- `data/exp_data/v4_pool_daily_cpmm.csv` / `v4_nextday_price.csv` contain 2025-07 dates, while the Pyth/ETHUSDT data elsewhere is explicitly Jan–Apr 2026 — worth confirming this isn't a stale/mismatched calibration input.
- Stale "Section IV-B"/"Section V-C" labels in some script docstrings — cosmetic but misleading; don't trust in-file section numbers.
