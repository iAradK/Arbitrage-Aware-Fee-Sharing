# Split-Resistant Surplus-Sharing Replay

This package implements the participation-aware transfer rule and its
transaction-scoped split-swap defense.

The canonical transfer for estimated surplus `S_hat` is

```text
F(S_hat) = min(lambda * S_hat,
               (1 - gamma) * max(0, S_hat - K_hat - delta)).
```

`K_hat` is the estimated baseline execution cost plus reservation payoff. The
order of operations is important: `gamma` is applied after subtracting
`K_hat + delta`.

For callbacks in the same transaction, the cumulative rule evaluates

```text
A_j = max(0, reference_price Â· cumulative_baseline_delta_j)
T_j = F(A_j)
W_j = max(W_(j-1), T_j)
r_j = W_j - W_(j-1)
```

Consequently, monotone partitions pay the same aggregate transfer as an
equivalent unsplit correction, while a reversal cannot refund a previously
charged transfer.

## Package layout

```text
src/
  SurplusSharingAccounting.sol
  CumulativeSurplusAccounting.sol
test/
  SurplusSharingAccounting.t.sol
  CumulativeSurplusAccounting.t.sol
python/
  hook_replay.py
  fragmentation_replay.py
foundry.toml
```

`SurplusSharingAccounting.sol` is a stateless implementation of `F`.
`CumulativeSurplusAccounting.sol` is a Cancun-EVM accounting prototype that
uses `TSTORE`/`TLOAD` to lock parameters, accumulate two-token baseline deltas,
and maintain a pool-specific watermark during one transaction.

## 1. Install dependencies

Python dependencies:

```bash
python -m pip install numpy pandas
```

Foundry dependency:

```bash
forge install foundry-rs/forge-std --no-git
```

The supplied `foundry.toml` selects Solidity 0.8.24, the Cancun EVM, and the
IR compilation pipeline. Cancun is required for transient-storage opcodes, and
`via_ir = true` avoids legacy-code-generator stack limits in the cumulative
accounting path. Gas measurements must use this same compiler configuration.

## 2. Prepare historical events

Create a CSV matching the existing event schema. Required economic columns are:

- `surplus_hat_usd`
- `execution_margin_hat_usd`
- `lp_loss_usd`
- `gas_price_gwei`
- `eth_usd`

Recommended optional columns include `event_id`, `timestamp`, `block_number`,
`true_surplus_usd`, reference and pool prices, and liquidity. A column may also
be supplied without `_usd` or as an integer `_wad` column.

The replay intentionally accepts estimated surplus as an input. It isolates
mechanism accounting from the separate oracle and AMM-state estimator.

## 3. Generate atomic vectors

Run from the package root:

```bash
python python/hook_replay.py \
  --events your_v4_events.csv \
  --output-dir results \
  --lambda-values 0.25,0.5,0.75,0.95 \
  --gamma 0.05 \
  --delta-usd 5 \
  --fallback-gas-units 3000
```

The fallback is only for pipeline testing. Do not report fallback gas as a
measurement.

Atomic outputs:

- `results/test_vectors.json`
- `results/python_vectors.csv`
- `results/hook_replay_events.csv`
- `results/hook_replay_summary.csv`

## 4. Run Solidity conformance tests

```bash
VECTORS=results/test_vectors.json \
forge test --match-contract SurplusSharingAccountingTest -vv
```

The test suite covers the corrected operation order, cap and proportional
branches, boundary values, fuzzed upper bounds, and Python-generated vectors.

To write the arithmetic-only gas CSV:

```bash
VECTORS=results/test_vectors.json \
GAS_OUTPUT=results/solidity_gas.csv \
forge test --match-test test_GasBenchmark -vv
```

Merge those measurements back into the atomic replay:

```bash
python python/hook_replay.py \
  --events your_v4_events.csv \
  --output-dir results \
  --lambda-values 0.25,0.5,0.75,0.95 \
  --gamma 0.05 \
  --delta-usd 5 \
  --solidity-gas-csv results/solidity_gas.csv
```

## 5. Test transaction-scoped accounting

```bash
forge test --match-contract CumulativeSurplusAccountingTest -vv
```

The cumulative tests cover:

- equal partitions with 1, 2, 4, 8, and 16 fragments;
- unequal monotone partitions;
- cumulative-surplus reversals and nonrefundable watermarks;
- multiple callers/routers in one transaction;
- isolation between pool IDs;
- locked parameters and reference prices;
- disabled charging when no initial discrepancy exists;
- subcall-revert rollback; and
- cumulative-target rounding boundaries.

Each Foundry test is a separate top-level transaction, so the suite also checks
that every test starts without transient state.

Write per-initialization and per-callback gas observations with:

```bash
CUMULATIVE_GAS_OUTPUT=results/cumulative_solidity_gas.csv \
forge test --match-test test_GasBenchmarkCumulative -vv
```

## 6. Run the fragmentation experiment

```bash
python python/fragmentation_replay.py \
  --events your_v4_events.csv \
  --output-dir results_fragmentation \
  --lambda-values 0.95 \
  --gamma 0.05 \
  --delta-usd 5 \
  --fragment-counts 1,2,4,8,16 \
  --incremental-fragment-gas-units 0
```

Replace the last value with the measured incremental callback gas when the
Foundry benchmark is available. The experiment compares equal partitions and
optimized adversarial partitions under:

1. the unsplit canonical transfer;
2. the old independently applied per-callback rule; and
3. the cumulative watermark rule.

Fragmentation outputs:

- `results_fragmentation/fragmentation_events.csv`
- `results_fragmentation/fragmentation_summary.csv`
- `results_fragmentation/fragmentation_optima.csv`

By default, the adversarial optimizer permits zero-sized limiting fragments.
This reports the infimum of the old rule. Use `--minimum-fragment-usd` to impose
a positive economically or numerically meaningful fragment size.

## Interpretation and implementation scope

The cumulative Solidity contract is an accounting prototype, not a complete
Uniswap v4 hook. In particular:

- the caller supplies the initial correction direction;
- deltas and prices are already normalized to WAD common-numeraire units;
- the watermark is maintained in that common numeraire;
- settlement-token conversion and conversion remainders are not implemented;
- oracle access, callback authorization, pool-state reads, vault settlement,
  and token transfers are excluded; and
- the public harness methods should become internal hook-controlled operations
  in a deployable implementation.

Accordingly, report the arithmetic-only and cumulative-accounting gas results
separately. Neither number is the total overhead of a deployed v4 hook.

The participation guarantee applies to the canonical unsplit execution.
Fragmented executions bear their additional gas costs, and reversal paths may
pay the peak watermark even when their final surplus is lower.