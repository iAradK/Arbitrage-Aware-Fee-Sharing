# Block-scoped hook accounting (2026-10-06)

The hook's accounting scope changed from one transaction to one block per pool.
`contracts/src/hooks/ParticipationAwareHook.sol` is the new hook. The old one is kept,
unchanged except for its name, as `TxScopedParticipationAwareHook.sol` so that tests and
gas profiles can compare the two scopes on identical sequences. No configuration was
changed and no test-month replay was rerun.

## 1. Old state (transaction-scoped, transient storage)

Each field is its own 256-bit transient slot, keyed by `keccak256(namespace, poolId, field)`
(11 slots per pool, `CumulativeSurplusAccountingLib`):

| field | type stored | note |
|---|---|---|
| reference price | `uint256` x 2 | `referencePrice0Wad` (always `WAD`, token0 is the numeraire) and `referencePrice1Wad` (oracle `priceWad`) |
| kappa | not stored as one value | `executionMarginHat` (`uint256`) and `delta` (`uint256`) are separate slots, combined inside `computeTransfer` |
| watermark | `uint256` | |
| cumulative deltas | `int256` x 2 | |
| also | `uint256` x 4 | status, direction (`int8` as `uint256`), lambda (WAD), gamma (WAD) |

## 2. New state (block-scoped, persistent storage)

`mapping(PoolId => Slots)` with three packed slots:

- A: `uint64 block | uint128 W | uint16 lambdaBps | uint16 gammaBps`
- B: `int128 cum0 | int128 cum1`
- C: `uint128 reference | uint128 kappa`. The reference fits 128 bits: an oracle price above
  `2^128 - 1` is rejected as invalid. With `|cum1| <= 2^127` and `reference < 2^128`, the
  product `cum1 * reference` fits `int256`, so the surplus cannot overflow.

Rules:

- A scope opens when `A.block != block.number` and the oracle read is valid. Reference, kappa,
  lambda and gamma are locked then.
- With an invalid oracle, no scope opens, the swap is charged 0 and is not accumulated, and
  the next swap retries.
- No zero writes: the first swap of a fresh scope writes `B = delta` and `A = (block, W,
  lambda, gamma)` over the previous block's values. The scope opens in the same `afterSwap`,
  so a local flag replaces the transient one (see section 3).
- A is written only when W changes, or on the scope's first swap.
- kappa = `K_const + delta + g_hat * (basefee + tau_hat) * p(ETH -> token0) / WAD`, where `p` is
  `gasPriceToken0Wad`, or the locked reference when that is 0. It is clamped to `uint128`.
- A cumulative delta outside `int128`, a target above `uint128`, or a charge that does not fit
  the `int128` return delta saturates W (`W = 2^128 - 1`). That fails open for the rest of the
  block.
- `quote(PoolKey, int256 d0, int256 d1) view` returns the numeraire charge that a swap with
  that core delta would raise W by now. In a block without an open scope, it reads the oracle
  and the base fee.

## 3. Callback layout: gas of both variants

> Superseded for the block-scoped figures: `results/gas/gas_cold.csv` and `results/gas/README.md`.
> The measurement below kept the oracle price fixed between blocks. That understated the
> block-scoped new-block and charged overheads by 2,800 gas: they are 34,559 and 48,177. The
> comparison between the two callback layouts is unaffected, since both had the same bias.

Gas is measured inside the transaction around the router call (`forge test --isolate`; every
call is a cold transaction). Overhead is the swap with the hook minus the identical swap
without one. Uncharged swaps use a reference of 1.0; the charged swap uses 1.2, with a
nonzero vault balance.

| scenario | afterSwap only | beforeSwap + afterSwap (transient flag) | transaction-scoped (old) |
|---|---:|---:|---:|
| first swap of a new block | 31,759 | 34,785 | 30,214 |
| first swap of a 2nd tx in the same block | 18,607 | 21,250 | 30,214 |
| 2nd swap in one tx | 7,307 | 9,950 | 13,823 |
| charged, settled swap (new block) | 45,377 | 48,532 | 44,707 |
| invalid oracle (new block) | 20,094 | 22,627 | 23,249 |
| first swap the pool ever sees | 85,859 | 88,885 | 30,214 |

The afterSwap-only variant is cheaper in every scenario, by 2.5k to 3.2k gas, and is the one
kept. Raw numbers are in `contracts/snapshots/blockScopedHookGas.json` and in the
`BSGAS,` log lines of `BlockScopedHookGas.t.sol`.

Against the transaction-scoped contract:

- A new block costs 1.5k gas more (cold `SSTORE`s of A, B and C).
- Later transactions in the same block cost 11.6k less, because they read the locked scope
  instead of reading the oracle again.
- The 2nd swap in a transaction costs 6.5k less.
- The first swap a pool ever sees costs 55.6k more, once per pool (three zero-to-nonzero
  `SSTORE`s).

The transaction-scoped column reproduces the paper's 30,214 and 13.8k. Its invalid-oracle
overhead is 23.2k here, against 5.7k in the paper. `HookGasBenchmark` measured the paper's
figure with every scenario inside one test function, so the oracle and hook were already
warm. A cold transaction pays 23.2k.

## 4. Tests

Run with `forge test --isolate`, so each call is its own transaction. Exception:
`BlockScopedHookBaseFeeTest`. Forge 1.5.1 executes isolated calls with
`block.basefee = 0`, whatever `vm.fee` or `--block-base-fee-per-gas` sets (verified with a
probe contract), so the base-fee re-lock of kappa is tested without isolation. The tests that
need isolation assert it.

1. Existing vectors:
   - E7 conformance passes under `--isolate`: 96 atomic and 360 sequence fragments, 0
     mismatches, counts identical to `results/e7/conformance_counts.json`. Each sequence now
     runs inside one call (`E7SequenceRunner`).
   - `SurplusSharingAccounting` passes (9 tests).
   - The ported hook vectors pass (11).
   - The transaction-scoped suites (`CumulativeSurplusAccounting`,
     `TxScopedParticipationAwareHook`, `E7GasProfile`, `HookGas*`) model one transaction as
     one test function, and still pass without `--isolate`.
2. 2/4/8-transaction splits in one block, one sender and several (`vm.prank`):
   - Fragments pay exactly F(A) of their net delta.
   - The unsplit swap pays 10.221253e18. The splits pay that minus 0, 3 and 7 wei for 2, 4
     and 8 transactions. The difference is the pool's per-swap input rounding.
   - The transaction-scoped contract collects 5.471e18 (2 transactions), 0 (4) and 0 (8).
3. `vm.roll` resets the scope and re-locks the reference (isolated). `vm.roll` plus `vm.fee`
   re-locks kappa at the new base fee (non-isolated). A base-fee change within a block does
   not move the locked kappa.
4. Gas snapshots: section 3.

After these passed:

- Reversal across 5 transactions by 4 senders: W = max_j F(A_j) after every transaction, and
  r_j = W_j - W_{j-1}. Nothing is refunded.
- An oracle update mid-block does not move the locked reference. The next block locks the new
  one. `quote()` equals the next swap's charge, both mid-scope and in a fresh scope.
- Invalid oracle on the first swap, then valid: no scope and charge 0; then a scope holding
  only the second swap, charged as an unsplit swap.
- Spillover bound r_j <= F(a_j) + (1 - gamma) kappa: fuzzed over 2,000 runs of up to 8 swaps
  from random senders, with correcting and adverse directions. The bound is attained: a swap
  with surplus kappa that follows another pays (1 - gamma) kappa although F(a_j) = 0.
- Random splits into at most 8 transactions (2,000 runs): sum = F(A) of the net delta
  exactly, and the result equals the unsplit charge within 4n wei. The transaction-scoped
  contract collects strictly less for n >= 2.

## 5. Integer reference and replays

- `common/fixedpoint.ScopedHookReference` mirrors the hook, with `scope="block"` or `"tx"`.
  `tests/test_block_scope_reference.py` adds 6 tests, and all 42 Python tests pass.
  `test_fixedpoint_matches_existing_solidity_reference` fails when run alone on a Hypothesis
  deadline (first-call import). That failure predates this change.
- `experiments/e6_block_scope_check.py` reruns the E6 exactness checks with every fragment as
  its own transaction of one block. It uses the same opportunities as E6 (same config, seed
  and order).
  - Validation months: 5,385 opportunities across 3 pools and 5 variants, n in
    {1, 2, 4, 8, 16}, with the equal, adversarial and reversal paths. Every violation count
    is 0: per-fragment charges equal the in-transaction watermark; monotone and adversarial
    splits sum to F(A_n); reversals sum to max_j F(A_j); there are no negative charges; and
    the transaction-scoped baseline charges sum_j F(a_j). That baseline's take relative to
    the block scope on equal splits:
    - ETH/USDC: 0.418 (n=2), 0.042 (n=16)
    - ETH/WBTC: 0.370 (n=2), 0.005 (n=16)
    - ETH/wstETH: 0.791 (n=2), 0.276 (n=16)

    Output: `results/e6/tables/e6_block_scope_checks_valid.{csv,tex}` and
    `results/e6/manifest_block_scope_valid.json`. No existing E6 file was touched.
  - Test months: not run (it is a test-month replay). Command:
    `python experiments/e6_block_scope_check.py --split test --confirm-frozen`.
- No two replay actions share a block. In every `results/e2/e2_nohook_steps_*` and
  `results/e8/e8_steps_*` file (valid and test, all lags and cadences), consecutive steps of
  one pool and configuration are at least 60 s apart, and no timestamp repeats. Blocks are 12 s
  apart, so the block scope never merges two replayed actions, and the replays' charges are
  unchanged by the new scope.

## 6. Open points

- The E7 gas pipeline (`e7_solidity_conformance.py --forge`: `HookGasIsolated`,
  `HookGasSettled`, `HookGasBenchmark`) still measures the transaction-scoped contract, so
  `results/e7/gas_profile.json` and the configs' `hook_overhead_gas = 30214` stay consistent.
  Switching to the block-scoped numbers belongs to the deployed-configuration sensitivity runs.
- The paper still describes the transaction scope. Affected passages:
  - Section 4: transient storage, "exactly one transaction", the lifecycle figure.
  - Proposition 2 and the cross-transaction discussion. Within a block the gap is now closed.
    Across blocks, (1 - gamma) kappa per additional block remains.
  - The scope paragraph: batching by a router now merges a whole block, not one transaction.
  - The gas numbers in Section 5.

## 7. Is the overflow fail-safe reachable? (2026-10-06)

Not with USDC, WETH, WBTC or wstETH. It is reachable only with tokens or prices far outside this study.

**Cumulative deltas (int128).** v4 already caps each swap's delta at int128. The hook's cumulative delta for one pool in one block is a net flow. Moving more than a token's supply would require tokens that do not exist: the pool cannot pay out more than it holds, and every unlock must settle in real tokens. Generous supply bounds, in base units:

| Token | Supply bound | Base units |
|---|---|---|
| USDC | 1e12 USDC | 1e18 |
| WETH, wstETH | below the ETH supply, 1.3e8 ETH | 1.3e26 |
| WBTC | 2.1e7 BTC cap | 2.1e15 |

The largest bound, 1.3e26, is 1.3e12 times below int128's 1.70e38.

**Target (uint128).** The target is at most the surplus, which is at most the token0 value of both supplies. With native ETH as token0 (wei):

- USDC supply at an ETH price of at least 100 USD: at most 1e28 wei.
- WBTC supply at a BTC/ETH ratio of at most 100: at most 2.1e27 wei.

So the target stays below about 1e28, 3.4e10 times below uint128's 3.40e38. With USDC as token0 the bound is smaller still: the ETH supply at 1e5 USD is 1.3e19 base units.

**Settlement amount (int128).** The settlement amount is the same charge expressed in the other token's base units. For example, 1e28 wei is at most 1e21 USDC base units, below int128 by more than 1e17.

**Reference price (uint128).** The 128-bit reference holds 1e30/P_USD for ETH/USDC (1e30 at P = 1 USD), and the BTC/ETH ratio times 1e28 for ETH/WBTC. It would exceed 2^128 only if the ratio passed 3.4e10. A price that large is rejected as invalid (fail open) and never overflows.

The fail-safe therefore guards against other tokens, for example an 18-decimal token with more than 1.7e20 whole tokens. In these pools it is unreachable by at least 10 orders of magnitude, so `BlockScopeBoundary` drives it through `DeltaManager` with synthetic deltas.

## 8. Block-scope conformance suites (2026-10-06)

| Suite | What it checks | How to run |
|---|---|---|
| `BlockScopeConformanceTest` | The 48 E7 sequences (validation months) in three modes: one transaction; one transaction per fragment in one block; 2-4 blocks. Each marginal charge is compared with `ScopedHookReference` (`experiments/e7_block_scope_vectors.py` writes `results/e7_block_scope/vectors.json`). | `--isolate` (asserted) |
| `BlockScopeFuzzTest` | 2,000 runs of real swaps from up to 8 senders, both directions, exact input and output, 1-4 blocks, with oracle updates between and within blocks and invalid-then-valid oracles. Each watermark increase and settled amount must equal the reference's, via `experiments/block_scope_ffi.py`. | `--ffi`; run once with `--isolate` (separate transactions, base fee 0) and once without (base fee changes every block) |
| `BlockScopeBoundaryTest` | New block, mid-block oracle update, invalid oracle (retry), the three overflow triggers. | Either mode |
| `BlockScopeBoundaryBaseFeeTest` | Re-lock of kappa at a new base fee. | Without `--isolate` (asserted): isolated calls see base fee 0 |

`ScopedHookReference` now models settlement in token1: W advances by the collected value, rounded up, and the remainder stays owed. It also models the int128 limit of the settlement amount.

Pitfall: with `via_ir`, a `block.number` cached in a local can be re-read after `vm.roll`, because the optimizer treats `NUMBER` as constant within a transaction. Tests use `vm.getBlockNumber()` instead.

## 9. Final design (S1, 2026-10-07)

`ParticipationAwareHook.sol` now implements block scope with per-transaction clipping (V1) and the relative proportional buffer. `TxScopedParticipationAwareHook.sol` is unchanged.

**Accounting.** One scope per pool and block. Each transaction t has a bracket b_t = [X_t · π̂ − ceil(ε_rel · π̂ · G_t)]⁺, where X_t is its net core delta and G_t its gross token1 volume. The block surplus is A = Σ (completed brackets) + the current transaction's bracket, and the watermark applies to F(A).

**Transactions.** A transient flag per pool marks the first swap of each transaction. That swap folds the previous transaction's bracket into the block total.

**Locked parameters.** κ = K̂ only (no δ), deducted once per block. ε_rel is configured in parts per billion and can be updated by `epsilonAdmin`; it is locked into the scope when the scope opens, together with the reference, κ, λ and γ. The evaluated configuration is τ̂ = 3 gwei, g = 180,214, γ = 0.02, ε_rel = 0.283%.

**State.** Four slots:
- A: block | W | λ | γ | ε_ppb
- B: the current transaction's net delta
- C: reference | κ
- D: closed surplus | the current transaction's gross token1 volume

A closed surplus or gross volume above 2^128 − 1 saturates W, like the earlier limits.

**Reference.** The integer mirror is `ScopedHookReference(scope="block", accumulation="tx_clip", buffer="rel")`.

**Changed test expectations.** Seven existing block-scoped tests encoded net accumulation across transactions (or a surplus without the buffer), so their expectations changed for V1. New tests were also added: closed-sum and gross-volume overflow, the eps lock, and `BlockScopeV1Properties`.

**Where the new expected values come from.** In the first S1 draft, the seven tests computed their expected charges in Solidity, by applying `_F` / `_scopeSurplus` to the hook's own stored scope. That mirrors the contract; it is not a reference. They now record every swap they make:
- real swaps: the core delta from the PoolManager `Swap` event, the settlement token, the block, the transaction index (each call is a transaction under `--isolate`; one transaction without it), the oracle price a scope opened at that swap would lock, and the base fee;
- DeltaManager swaps: the deltas passed in.

`BlockScopedHookTestBase._refRun` sends the trace through `experiments/block_scope_v1_ffi.py` to `common.fixedpoint.ScopedHookReference(scope="block", accumulation="tx_clip", buffer="rel")`. It passes the hook's own constructor parameters (K̂, λ, γ, g, τ̂, gas price, ε_ppb · 1e9), and asserts that each charge equals the reference's W increase. Where stated below, it also asserts that the block surplus `scopeSurplus()` equals the reference's A. The suites need `forge test --ffi`.

**Negative control.** With the reference's K̂ raised by 0.1 token0 (a temporary edit of `_refRun`, reverted), five of the seven fail on exactly these equalities:
- reversal;
- both mid-block tests;
- spillover;
- new block.

The two int128 overflow tests are unaffected, as they should be. Their reference checks are the saturation flags, and the charge before saturation is either λa with a ≈ 2^127 (κ is irrelevant) or 0.

| Test (file) | Old expectation | Why V1 changes it | New expected value |
|---|---|---|---|
| `test_ReversalAcrossTransactions` (Scenarios, `--isolate`) | Five transactions (forward, adverse, small recovery, large recovery, adverse) net into one A; W = max_j F(A_j); the small recovery below the earlier peak pays nothing; the path ends below its peak. | Each transaction is its own bracket clipped at 0. An adverse transaction neither lowers A nor refunds, and every later correcting transaction is charged on its own bracket. A is non-decreasing; W = F(A) = the total paid. | Each charge r_j = reference w_j; `scopeSurplus` after each swap = reference A_j; the adverse transactions show reference A unchanged and w = 0 (asserted on the reference's output); total paid = W = Σ reference w_j. |
| `test_OracleUpdateMidBlockDoesNotMoveLockedReference` (Scenarios, `--isolate`) | The second swap's charge = F(net of both swaps at 1.2) − W₁, via `_scopeSurplus` on the netted cumulative delta. `_lastDelta` = cumulative after − cumulative before. | The second swap is a new transaction: the first is folded into the closed surplus, and the cumulative delta holds only the current transaction. The old `_lastDelta` difference would then be wrong, so `_lastDelta` returns the cumulative delta itself. | Both charges (the mid-block swap with oracle 1.5 in the trace, and the next block's opening swap at 1.5) = reference w. The reference is computed before `vm.revertToState`, because the revert also rolls back the trace. The `quote()` = charge assertions are unchanged. |
| `testFuzz_SpilloverBound` (Scenarios, `--isolate`) | r_j ≤ F(a_j) + (1−γ)κ + 4, with a_j from the `_lastDelta` difference. | Same `_lastDelta` problem: under V1 the stored delta is the current transaction's, not a running sum. | r_j = reference w_j (equality), and a_j now comes from the swap's own `Swap` event. The upper bound F(a_j) + (1−γ)κ is still evaluated with the hook's pure `computeTransfer`, the F that E7 conformance checks against `transfer_wad`. It is a bound, not an expected value. |
| `test_MidBlockOracleUpdateDoesNotMoveLockedReference` (Boundary, both modes) | Cumulative delta = Alice + Bob (netted); charge = F(net) − W₀; the post-revert swap charge = F(net of three) − W₁. | With `--isolate`, Bob's swap is a new transaction: Alice's bracket is folded, and the current delta is Bob's alone. Without isolation, one transaction nets as before. The boundary hook now also has the buffer (ε = 0.283%) and τ̂ = 3 gwei. | All four charges = reference w. The trace gives the reference 2e18 and an invalid price at the mid-block swaps; it ignores both because the scope is open, so matching it means the hook charged at the locked 1.2e18. The state-layout checks (folded closed surplus, current delta per mode) remain checks on the stored fields. |
| `test_NewBlockResetsAccumulatorAndRelocksReferenceAndKappa` (Boundary, both modes) | c₃ = F(own surplus) via `_scopeSurplus` (no buffer); W = c₁ + c₂. | The boundary hook now deducts the relative buffer inside each bracket, so F of the unbuffered surplus is no longer the charge. A closed-surplus reset is also checked (`closedSurplus == 0`). | c₁, c₂, c₃ = reference w; `scopeSurplus` after c₃ = reference A. The κ formula checks (`_kappa`) are unchanged parameter checks. |
| `test_OverflowCumulativeDeltaSaturatesForRestOfBlock` (Boundary, both modes) | Two separate calls: +(2^127 − 11), then +11; the second leaves int128 and saturates. | With `--isolate` the second call is a new transaction. It folds the first and holds only +11, so nothing overflows: the old test would no longer trigger the fail-safe. The two swaps are now one transaction (`DeltaManager.swapMany`). | First charge = reference w₀. From the second swap to the end of the block, the reference reports saturation (A = 2^256 − 1 sentinel, w = 0), as the hook does (W = 2^128 − 1, charge 0). The next block's charge = reference. |
| `test_OverflowNegativeCumulativeDeltaSaturates` (Boundary, both modes) | Two calls: d1 = −2^127 + 5, then −6; the second saturates. | Same: under V1 the second call would start a new transaction. Now one transaction. | Reference w₀ = 0 = hook. Reference saturated from the second swap on, w = 0; the next block's charge = reference. |

Other tests still use `_F(hook, s, _scopeSurplus(s))`, the contract mirror. Since S1, `_scopeSurplus` = closed surplus + buffered current bracket. These tests passed unchanged and are not among the seven:
- the invalid-oracle retry, the eps lock and the base-fee re-lock (Boundary);
- the opening-swap, fuzz and roll tests of `BlockScopedHook.t.sol`;
- two split tests, `BlockScopedHook.t.sol:229` and `BlockScopedHookScenarios.t.sol:266` ("sum of charges = F(A)"). These span several transactions under `--isolate`.

They check internal consistency (charges = F of the stored state), not reference values. Reference equality for multi-transaction blocks comes from:
- the seven tests above;
- E7 conformance (`vectors_v1.json`, split modes a/b/c);
- the 2,000-run fuzz (multi-swap transactions over several blocks).

Results and gas: `results/e7_block_scope/` (`conformance_counts_v1.json`, `fuzz_v1_*.csv`) and `results/gas/gas_cold.csv`.

## 10. Gas: the current transaction in transient storage (S2, 2026-10-07)

Commits on branch `claude/s2-gas-transient-ec0481`, cut from 3d163c0:

| commit | content |
|---|---|
| 24c855a | hook change; tests that read the transaction's fields from a later transaction; new quote-in-transaction test |
| c6303a4 | the two remaining split tests take their expected charges from the reference |
| f6db88f | the older suites write their output files only when the output variable is set |
| 27fef0a | two gas scenarios in which the block surplus changes without a charge |

Every number below was measured on 27fef0a (clean), unless it says otherwise.

**Change.** The current transaction's state moves to transient storage, keyed `keccak256("ParticipationAwareHook.tx", poolId) + k`:
- T0: the net deltas, int128 | int128;
- T1: the gross token1 volume, uint128, with the transaction flag at bit 128;
- T2: closed, the sum of the earlier transactions' brackets.

Persistent slots B and D become one slot P = closed + the current transaction's clipped bracket, i.e. the block surplus A. P is rewritten on every swap. A transaction's first swap in an open scope takes closed := P. P carries a marker bit (bit 255; A < 2^197), so after the pool's first swap it is never zero and every write is nonzero to nonzero. Without the marker, `charged_vault_empty` measured 12.4k more than before: P went from 0 to nonzero, a 22,100-gas SSTORE.

Unchanged:
- every charge;
- the saturation triggers: closed (now P at a transaction's first swap) or gross above 2^128 − 1, the int128 deltas, the target, the settlement;
- ε stays a storage variable, locked at scope open;
- every uint128/int128 limit.

**`getScope`.** The current transaction's fields (`cumulativeDelta0/1`, `txGross1`) are transient.
- Inside the transaction that swapped, they hold its running values, and `closedSurplus` holds the earlier brackets.
- Anywhere else, including every later call under `--isolate`, they read 0 and `closedSurplus` = P, the whole block surplus.

In both cases `closedSurplus` + the bracket of the current fields = `scopeSurplus()`, which now returns P. `closedSurplus` widened from uint128 to uint256, because P can exceed 2^128 − 1 until the next transaction saturates. `quote()` reads the transient state, so it stays exact inside a transaction. The new `BlockScopeBoundaryTest.test_QuoteWithinATransactionEqualsTheCharge` checks this in both modes: before each of 6 swaps in 2 transactions, with the buffer on, `quote()` read inside the same transaction equals the swap's charge, and every charge equals the reference.

### Gas (cold, `--isolate`, overhead = with hook − without)

How it was measured:
- **After:** 27fef0a, from `python experiments/gas_cold_table.py`.
- **Before:** the hook at 3d163c0, which is identical to ff69c37's. It was measured with the same gas test file, from 27fef0a, in a scratch copy of 3d163c0's contracts. For every older scenario this run reproduces the committed ff69c37 `results/gas/gas_cold.csv` exactly.
- **Where the table lives:** by the user's instruction, no result file is committed. The tracked `results/gas/gas_cold.csv` still holds the ff69c37 figures. The new table is the local, untracked `results/e7_block_scope/logs_27fef0a/outputs/gas_cold.csv`; the figures below are copied from it.

| scenario | before | after | change |
|---|---:|---:|---:|
| first swap of a block, no charge, P unchanged (0 → 0) (`new_block`) | 43,341 | 35,901 | −7,440 |
| first swap of a block, no charge, P changes (`new_block_positive_surplus`) | 43,336 | 38,696 | −4,640 |
| first swap of a 2nd transaction, no charge, P unchanged (`second_tx`) | 25,913 | 17,240 | −8,673 |
| first swap of a 2nd transaction, no charge, P changes (`second_tx_positive_surplus`) | 25,903 | 20,035 | −5,868 |
| 1st swap of a two-swap transaction (`first_in_multi_tx`) | 43,341 | 35,901 | −7,440 |
| 2nd swap in one transaction (`second_in_tx`) | 9,292 | 9,066 | −226 |
| charged, vault holds the token (`charged`) | 56,977 | 52,337 | −4,640 |
| charged, vault empty (`charged_vault_empty`) | 74,077 | 69,437 | −4,640 |
| charged 2nd transaction (`charged_second_tx`) | 42,448 | 36,580 | −5,868 |
| charged after another sender's transaction (`charged_after_other_tx`) | 42,461 | 36,593 | −5,868 |
| invalid oracle, first / 2nd swap of a transaction | 20,171 / 7,171 | 20,161 / 7,161 | −10 |
| first swap a pool ever sees, no charge / charged | 111,741 / 142,464 | 90,001 / 120,724 | −21,740 |

The transaction-scoped hook is unchanged: 30,214 for a first swap and 44,707 for a charged swap. It measures 30,198 in the two new scenarios.

The extra arbitrage transaction (21,000 + the swap without a hook + the overhead) at the study window's median of 5.449e-7 USD/gas:

| case | cost |
|---|---|
| in the same block, no charge | 138,276–141,071 gas (0.0753–0.0769 USD) |
| in the same block, charged | 157,616 gas (0.0859 USD) |
| in a new block, no charge | 156,937–159,732 gas (0.0855–0.0870 USD) |
| in a new block, charged | 173,373 gas (0.0945 USD) |

### Where the saving comes from

Storage cost schedule (EIP-2929/2200), checked against the measured differences:

| scenario | before | after | derived | measured |
|---|---|---|---:|---:|
| first swap of a block, P changes | SSTORE B and D cold, changed (2 × 5,000); TLOAD + TSTORE flag (200) | SSTORE P cold, changed (5,000); 3 TSTORE (300) | −4,900 | −4,640 |
| first swap of a block, P unchanged (0 → 0) | as above | SSTORE P cold, unchanged (2,200); 3 TSTORE (300) | −7,700 | −7,440 |
| 2nd transaction, P changes | SLOAD B, D cold (2 × 2,100) + SSTORE B, D (2 × 2,900) + flag (200); fold: the previous transaction's bracket recomputed | SLOAD P cold (2,100) + SSTORE P (2,900) + TLOAD T1 + 3 TSTORE (400) | −4,800 − fold | −5,868 |
| 2nd transaction, P unchanged | as above | SSTORE P unchanged (100) | −7,600 − fold | −8,673 |
| 2nd swap in a transaction | 4 warm SLOAD + 2 dirty SSTORE + flag | 3 TLOAD + 2 TSTORE + 1 dirty SSTORE | ≈ −200 | −226 |
| first swap a pool ever sees | B and D zero → nonzero (2 × 22,100) | P zero → nonzero (22,100) + 3 TSTORE | −21,800 | −21,740 |

The remaining differences, about +260 per swap, come from the extra masking and transient-slot arithmetic. In the second-transaction rows they also cover the fold the hook no longer computes: about 1.1k with ε ≠ 0, including the mulDivRoundingUp.

The estimate of about 4.9k holds when P changes: the first swap of a block saves 4,640 (43,336 → 38,696), and a charged one saves the same. A second transaction saves 5,868, which includes the fold, about 1.1k. When the uncharged swap's bracket clips to 0, as in `new_block` and `second_tx`, P is rewritten unchanged, so the saving is about 2.8k larger. The old slot D always changed, because it held the gross volume. Of the +8,782 that the S1 design added over fcb66ae, this recovers the slot-D write (5,000). It does not recover the cold read of `epsilonRelPpb` (2,100, kept as a storage variable), the κ gas term (510) or the buffer arithmetic (416).

### Tests

**Updated because they read the transaction's fields from a later transaction (`getScope` layout).** Each one now compares P with the swap's own bracket, or takes the swap's core delta from its PoolManager `Swap` event (`_swapRec` / `_lastRef`). In the non-isolated mode, the original field checks are kept.
- `BlockScopedHookTest`:
  - `test_Settlement_ExactInput_ChargesOutputTokenToVault` and `..._ExactOutput_...`: the swapper delta is checked against the core delta from the `Swap` event, not against `cumulativeDelta`;
  - `test_RollResetsScope`: P = the bracket of the new block's swap alone.
- `BlockScopedHookScenariosTest`:
  - `test_InvalidOracleOnFirstSwapThenValid`: P = the second swap's bracket;
  - `test_OracleUpdateMidBlockDoesNotMoveLockedReference`: `_lastDelta` (which read `cumulativeDelta`) is replaced by `_lastRef`, the `Swap` event's core delta, read before the revert.
- `BlockScopeBoundaryTest`:
  - `test_NewBlockResetsAccumulatorAndRelocksReferenceAndKappa` and `test_InvalidOracleChargesNothingAndRetriesWhenValidAgain`: `scopeSurplus` = the own swap's bracket in both modes; the field checks only without `--isolate`;
  - `test_MidBlockOracleUpdateDoesNotMoveLockedReference` (isolated branch): `closedSurplus` = Alice's folded bracket + Bob's bracket, and the transaction fields read 0;
  - `test_OverflowClosedSurplusSaturates`: after transaction 2, P = 2^128 − 2 + 1e18 (transaction 1 folded plus transaction 2's bracket), where the old D held 2^128 − 2. Saturation still triggers at transaction 3.
- Helpers in `BlockScopedHookTestBase`: `_txBracket`, `_lastRef`. In `DeltaManager`: `quoteAndSwapMany`.

No charge expectation changed. The suites that do not read those fields pass unmodified: conformance (3 modes), fuzz (iso and no-iso), V1 properties, the base-fee test and `ParticipationAwareHookFeeTest`.

**The two remaining self-checking split tests now use the reference.** `BlockScopedHookTest._txSplit` (the six `test_TxSplit_*`) and `BlockScopedHookScenariosTest.testFuzz_RandomTxSplits` record each split fragment with `_swapRec`. They assert:
- every fragment's charge = the reference's w_j;
- the total = Σ w_j;
- the final `scopeSurplus` = the reference's A.

The unsplit comparison (within 4n wei) and the transaction-scoped comparison stay. No test in section 9's list now uses `_F(hook, s, _scopeSurplus(s))` for a multi-transaction expectation.

**Negative control (27fef0a + a temporary edit).** `_refRun` passed `kappaConstant() + 0.1e18` to the reference; the edit was restored byte for byte, and git confirmed a clean tree. These fail on the reference equalities:
- `--isolate`, `BlockScopedHookTest`: all six `test_TxSplit_*` ("fragment charge = reference");
- `--isolate`, `BlockScopedHookScenariosTest`: `testFuzz_RandomTxSplits`, plus `test_ReversalAcrossTransactions`, `test_OracleUpdateMidBlockDoesNotMoveLockedReference` and `testFuzz_SpilloverBound`;
- `BlockScopeBoundaryTest`, both modes: `test_MidBlockOracleUpdateDoesNotMoveLockedReference`, `test_NewBlockResetsAccumulatorAndRelocksReferenceAndKappa` and the new `test_QuoteWithinATransactionEqualsTheCharge`.

Unaffected, as they should be:
- the two int128 overflow tests: their charge before saturation is λa with a ≈ 2^127, or 0;
- `BlockScopeV1PropertiesTest` (2/2): it compares two hooks with each other, not with the reference;
- the remaining tests, which do not call the reference.

So both converted tests are sensitive to the reference. So are all seven tests from section 9 that can be: the five charge tests plus the two overflow tests, which check saturation flags.

**Test outputs.** The older suites no longer write to `results/e7` or `contracts/results` by default. They write only when their variable is set:

| suite | variable |
|---|---|
| `E7ConformanceTest` | `E7_COUNTS` |
| `E7GasProfileTest` | `E7_GAS_CSV` |
| `HookGasBenchmarkTest` | `HOOK_LIFECYCLE_GAS_OUTPUT` |
| `CumulativeSurplusAccountingTest` | `CUMULATIVE_GAS_OUTPUT` |
| `SurplusSharingAccountingTest` | `GAS_OUTPUT` |

`experiments/e7_solidity_conformance.py --forge` now sets `E7_COUNTS` and `E7_GAS_CSV` explicitly; it relied on their defaults. It already set `HOOK_LIFECYCLE_GAS_OUTPUT`. All three paths are relative to its output directory.

Checked in a scratch copy of 27fef0a's contracts, with the gitignored inputs as read-only copies:
- With no variable set, the five suites pass 23/23 and write no file.
- With every variable set, they pass 23/23 and write all five outputs into the scratch tree.
- The main checkout's `results/e7` and `contracts/results` (88 files) hash identically before and after.

Not changed: `BlockScopeConformanceTest` and `BlockScopeFuzzTest` still write fixed paths in `results/e7_block_scope/`. The reruns copied those files into the log folder and restored the tracked ones.

**Suite results on 27fef0a (clean).**

| suite | mode | result |
|---|---|---|
| `BlockScopeConformanceTest` | `--isolate` | 360 fragments per mode; mismatches a/b/c 0/0/0; charged 90/119/87. The counts JSON matches ff69c37's. |
| `BlockScopedHookTest` | `--isolate --ffi` | 17/17 |
| `BlockScopedHookScenariosTest` | `--isolate --ffi` | 6/6 |
| `BlockScopeBoundaryTest` | `--isolate --ffi` | 12/12 |
| `BlockScopeV1PropertiesTest` | `--isolate --ffi` | 2/2 |
| `BlockScopeFuzzTest`, 2,000 runs | `--isolate` | pass; every row 0 mismatches |
| `BlockScopeFuzzTest`, 2,000 runs | no isolation | pass; every row 0 mismatches |
| `BlockScopedHookGasTest` | `--isolate` | 39/39 |
| `BlockScopeBoundary*`, including the base-fee test | no isolation | 12 pass, 1 skipped (the closed-surplus overflow needs separate transactions) |
| `ParticipationAwareHookFeeTest` | no isolation | 3/3 |
| `TxScopedParticipationAwareHookTest`, `HookGasIsolatedTest`, `HookGasSettledTest` | no isolation | 22/22 |
| the five older suites | scratch copy | 23/23 |

Logs, outputs and run scripts are local and untracked, in `results/e7_block_scope/logs_27fef0a/` (README there): the logs, `negative_control/`, `outputs/` (the new `gas_cold.csv`, conformance counts, fuzz CSVs) and `scripts/`.
