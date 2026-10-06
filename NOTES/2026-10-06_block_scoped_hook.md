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
