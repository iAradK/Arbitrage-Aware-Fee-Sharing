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
  CumulativeSurplusAccountingLib.sol
  hooks/
    MinimalBaseHook.sol
    ParticipationAwareHook.sol           (block-scoped, persistent storage)
    TxScopedParticipationAwareHook.sol   (transaction-scoped baseline)
  mocks/
    MockOracle.sol
  interfaces/
    IReferenceOracle.sol
test/
  SurplusSharingAccounting.t.sol
  CumulativeSurplusAccounting.t.sol
  utils/
    HookMiner.sol
  hooks/
    BlockScopedHookTestBase.sol
    BlockScopedHook.t.sol             (run with --isolate, except BlockScopedHookBaseFeeTest)
    BlockScopedHookScenarios.t.sol    (run with --isolate)
    BlockScopedHookGas.t.sol          (run with --isolate)
    HookTestBase.sol                  (transaction-scoped baseline)
    TxScopedParticipationAwareHook.t.sol
    HookGasBenchmark.t.sol
    HookGasIsolated.t.sol
    HookGasSettled.t.sol
python/
  hook_replay.py
  fragmentation_replay.py
foundry.toml
remappings.txt
```

`SurplusSharingAccounting.sol` is a stateless implementation of `F`.
`CumulativeSurplusAccounting.sol` is a Cancun-EVM accounting prototype that
uses `TSTORE`/`TLOAD` to lock parameters, accumulate two-token baseline deltas,
and maintain a pool-specific watermark during one transaction; its private
logic lives in `CumulativeSurplusAccountingLib.sol`, which the
transaction-scoped baseline hook `TxScopedParticipationAwareHook.sol` also
uses.

`ParticipationAwareHook.sol` is the hook. Its accounting scope is one block
per pool: the watermark rule covers every swap of a pool in a block, across
transactions and senders, so splitting a correction across transactions of one
block no longer deducts `kappa = K_hat + delta` once per transaction. The state
is persistent and keyed by `PoolId`, in three packed slots
(A: `uint64 block | uint128 W | uint16 lambdaBps | uint16 gammaBps`,
B: `int128 cum0 | int128 cum1`, C: `uint128 reference | uint128 kappa`). The
first swap of a block whose oracle read is valid opens the scope and locks the
reference and `kappa = K_const + delta + g_hat * (basefee + tau_hat) * p_ETH`;
an invalid oracle opens nothing and charges 0, and the next swap retries. All
logic runs in `afterSwap` (a `beforeSwap` variant cost 2.5-3.2k gas more).
`quote(key, d0, d1)` returns the charge a swap with that core delta would pay
now. Each marginal charge is converted into the swap's unspecified currency at
the locked reference price (rounded down), returned as the `afterSwap` return
delta, and minted as ERC-6909 claims to the LP-protection vault.

## 1. Install dependencies

Python dependencies:

```bash
python -m pip install numpy pandas
```

Foundry dependencies:

```bash
forge install foundry-rs/forge-std --no-git
forge install Uniswap/v4-core@v4.0.0 --no-git
```

`forge install` also pulls v4-core's own pinned `openzeppelin-contracts` and
`solmate` submodules as flat siblings under `lib/`; `remappings.txt` maps
`v4-core/`, `@openzeppelin/`, and `solmate/` to those paths (re-run
`forge install` rather than editing the remappings by hand if v4-core's
pinned commits ever change). `HookMiner` (CREATE2 salt mining for hook
address flags) is vendored locally in `test/utils/HookMiner.sol` rather than
pulled from `v4-periphery`, since that helper lives outside v4-core in a
separately, loosely versioned repo.

The supplied `foundry.toml` selects Solidity **0.8.26** (forced by v4-core
`v4.0.0`'s own pragma pin; the accounting-only contracts previously targeted
`^0.8.24` and compile unchanged under 0.8.26), the Cancun EVM, and the IR
compilation pipeline, with `optimizer_runs = 200` (this repo's existing
setting, deliberately **not** v4-core's own `44444444` — that would change
gas numbers project-wide and inflate build time for no benefit here, so
absolute hook gas figures here will not exactly match a
production-optimized v4 deployment). Cancun is required for
transient-storage opcodes, and `via_ir = true` avoids legacy-code-generator
stack limits in the cumulative accounting path. Gas measurements must use
this same compiler configuration.

Note on the solc 0.8.24 -> 0.8.26 re-baseline: regenerating
`results/solidity_gas.csv` and `results/cumulative_solidity_gas.csv` after
the bump (before any v4-core code was added) changed the *total* gas
reported by `SurplusSharingAccountingTest.test_GasBenchmark`/
`test_VectorFile` from roughly 84.0-84.4M down to roughly 6.3-6.5M. The
per-call arithmetic-only figure used elsewhere in this document
(~4,192-4,801 gas) is unaffected; the large total-gas delta is confined to
these two aggregate benchmark tests, which sum gas across every vector in
the suite in a single Forge-reported number, and has not been further
root-caused (candidates include solc's own codegen changes between the two
compiler minor versions interacting with `via_ir`). Treat the per-call
figures, not the aggregate test totals, as the numbers to cite.

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

Optionally add `--hook-lifecycle-gas-csv results/hook_lifecycle_gas.csv`
(produced in step 8 below) to also compute a third
`including_full_lifecycle_hook_overhead` variant in
`results/hook_replay_summary.csv`, alongside the existing
`ignoring_hook_overhead`/`including_hook_overhead` variants. That variant
subtracts the true full-lifecycle hook gas `G_hook` from the participation
margin instead of the arithmetic-only figure: since the benchmark grid in
step 8 is a fixed scenario sweep rather than a per-event measurement, each
replay event conservatively uses the maximum `gas_used_with_hook` observed
across valid-oracle, non-reverted scenarios in that grid as `G_hook`. Expect
`including_full_lifecycle_hook_overhead`'s `participation_rate` to be lower
than the arithmetic-only variants' — it is the fully-costed number.

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
  --output-dir results/fragmentation \
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

- `results/fragmentation/fragmentation_events.csv`
- `results/fragmentation/fragmentation_summary.csv`
- `results/fragmentation/fragmentation_optima.csv`

By default, the adversarial optimizer permits zero-sized limiting fragments.
This reports the infimum of the old rule. Use `--minimum-fragment-usd` to impose
a positive economically or numerically meaningful fragment size.

## 7. Fuzz test the v4 hook prototype

Block-scoped hook. `--isolate` makes every external call of a test its own
transaction, so the tests exercise state that survives across transactions in
one block (`_assertIsolated` fails the tests otherwise). Forge 1.5.1 runs
isolated calls with `block.basefee = 0` whatever `vm.fee` sets, so the base-fee
re-lock of `kappa` is tested without isolation in `BlockScopedHookBaseFeeTest`:

```bash
forge test --isolate --match-contract '^(BlockScopedHookTest|BlockScopedHookScenariosTest|BlockScopedHookGasTest)$' -vv
forge test --match-contract BlockScopedHookBaseFeeTest
```

The transaction-scoped baseline below models one transaction as one test
function and must run without `--isolate`.

`TxScopedParticipationAwareHook.sol` is exercised against a real, freshly deployed
`PoolManager` (via v4-core's own `test/utils/Deployers.sol` fixture) with a
CREATE2-mined hook address, real `PoolSwapTest`/`PoolModifyLiquidityTest`
routers, and a `MockOracle` with a per-pool settable failure mode (stale,
zero-price, incomplete-round, self-reported-invalid), not a lightweight
simulation of the callback logic.

```bash
forge test --match-contract TxScopedParticipationAwareHookTest -vv
```

For a paper-quality run, increase the fuzz run count:

```bash
forge test --match-contract TxScopedParticipationAwareHookTest --fuzz-runs 10000
```

The suite covers: the `zeroForOne`-derived direction mapping; boundary
values of `K_hat`, `delta`, `lambda`, `gamma`, and swap size (from dust to
near-`uint128` scale); constructor rejection of invalid `lambda`/`gamma`;
monotone charge behavior across single-tick and multi-tick-crossing swap
sequences; fail-open behavior under every invalid oracle mode, including an
oracle that reverts (the swap never reverts, `marginalCharge` stays zero, and
`OracleRejected` fires with the matching reason); settlement for exact-input
(charge taken from the output token) and exact-output (charge added to the
input token) swaps, with the vault's claims matching the swapper's delta
adjustment and fragmented sequences never collecting more than the
watermark; and isolation between two pools sharing one hook
instance, driven through real swaps rather than direct library calls.

## 8. Full-lifecycle hook gas benchmark

The arithmetic-only figures from steps 4-5 (~4,192-4,801 gas) measure only
`SurplusSharingAccounting`/`CumulativeSurplusAccounting`'s internal
computation. They exclude oracle access, transient-storage tracking, and
Uniswap's own callback-routing overhead — all of which a deployed hook must
pay on every swap. The block-scoped hook's gas is profiled scenario by
scenario in `BlockScopedHookGas.t.sol` (run with `--isolate`; it writes
`snapshots/blockScopedHookGas.json` and `BSGAS,...` log lines and compares
with the transaction-scoped contract). The E7 gas pipeline below
(`HookGasBenchmark`, `HookGasIsolated`, `HookGasSettled`) still measures the
transaction-scoped baseline. `HookGasBenchmark.t.sol` measures `gasleft()` around the
entire `swapRouter.swap(...)` call against the real hook and pool, and
records an identically-sized, identically-liquidity-seeded baseline swap
with hooks disabled (`address(0)`) for comparison.

```bash
forge test --match-test test_GasBenchmarkFullLifecycle -vv
```

This regenerates `results/hook_lifecycle_gas.csv` (path overridable via
`HOOK_LIFECYCLE_GAS_OUTPUT`) over a fixed, deterministic scenario grid: `{1,
2, 4, 8, 16}` fragments (a fixed total swap notional split into that many
sequential callbacks, mirroring `test_GasBenchmarkCumulative`'s convention)
x `{single-tick, multi-tick}` swap sizes x `{valid, stale, zero-price,
incomplete-round, self-invalid}` oracle states, one row per fragment:

| column                 | meaning                                                             |
| ---------------------- | -------------------------------------------------------------------- |
| `scenario_id`          | index into the fragments x tick-class x oracle-mode grid            |
| `fragments`            | total fragment count for this scenario                              |
| `fragment_index`       | 1-based position of this row within the scenario                    |
| `oracle_mode`          | `MockOracle` mode active for the scenario                           |
| `swap_amount_wad`      | this fragment's swap size                                           |
| `zero_for_one`         | swap direction                                                      |
| `ticks_crossed`        | tick-spacing-aligned boundaries crossed by this fragment             |
| `tick_crossing_class`  | `single-tick` or `multi-tick`                                       |
| `cap_binds`            | whether `computeTransfer`'s cap (not the proportional target) bound |
| `gas_used_with_hook`   | gas for the fragment's swap against the real hook                   |
| `gas_used_without_hook`| gas for the identical swap on a `hooks = address(0)` baseline pool  |
| `marginal_charge_wad`  | this fragment's watermark increase                                  |
| `cumulative_target_wad`| `computeTransfer`'s target evaluated on this fragment's cumulative surplus |
| `reverted`             | true if either swap reverted                                        |

Invalid-oracle rows report lower `gas_used_with_hook` than valid-oracle rows
(the hook fails open before doing any accounting work) and
`cumulative_target_wad = 0` on every invalid-oracle row, confirming the
fail-open path never charges. Feed this CSV into `hook_replay.py` via
`--hook-lifecycle-gas-csv` (step 4) to fold the true full-lifecycle
`G_hook` into the participation-margin analysis.

## Interpretation and implementation scope

`CumulativeSurplusAccounting.sol`/`CumulativeSurplusAccountingLib.sol` are an
accounting prototype; `ParticipationAwareHook.sol` (block-scoped) and
`TxScopedParticipationAwareHook.sol` wire the same rule into real v4-core swap
callbacks (oracle access, storage, and callback routing are exercised, not
simulated), but neither is a deployable hook. In particular:

- the transaction-scoped baseline records a correction direction from each
  swap's own `zeroForOne` flag for diagnostics only; the block-scoped hook
  stores no direction (neither uses it to compute the charge);
- deltas are raw token base units and token0 is the numeraire (the oracle
  price is token1's value in token0 base units), so the watermark, `K_hat`,
  and `delta` are all in token0 base units;
- the charge is settled by minting ERC-6909 claims to a fixed vault address;
  the rounding remainder of the token conversion stays in the watermark and
  is collected by the next fragment; how the vault distributes claims to LPs
  is out of scope;
- under the cumulative watermark, a small fragment that pushes the cumulative
  surplus past `K_hat + delta` pays the whole marginal charge, which in an
  exact-input swap can exceed that fragment's own output; and
- the public harness methods should become internal hook-controlled operations
  in a deployable implementation.

Accordingly, report the arithmetic-only, cumulative-accounting, and
full-lifecycle gas results separately; only the full-lifecycle figure from
step 8 is a defensible estimate of `G_hook`.

The participation guarantee applies to the canonical unsplit execution.
Fragmented executions bear their additional gas costs, and reversal paths may
pay the peak watermark even when their final surplus is lower.