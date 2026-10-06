# Cold gas table for both hooks

`gas_cold.csv` comes from `python experiments/gas_cold_table.py`, which runs
`contracts/test/hooks/BlockScopedHookGas.t.sol` with `forge test --isolate`. No simulation was
run. `results/e7/gas_profile.json` and `hook_overhead_gas` are unchanged.

How the numbers are measured:

- Every measured swap is its own transaction, so accounts and storage start cold. Storage
  written by earlier transactions is nonzero, as on chain.
- Gas is measured inside the transaction around the router call.
- Overhead is the gas with the hook minus the gas of the identical swap sequence on a pool
  without a hook.
- Before a measured new-block swap the oracle price moves slightly, as it does between real
  blocks. This makes the block-scoped hook rewrite its reference slot with a new value
  (2,900 gas). An earlier version of this test kept the price fixed, which understated the
  block-scoped new-block and charged figures by 2,800. That version is what
  `NOTES/2026-10-06_block_scoped_hook.md` reports.

## Overhead (gas)

| scenario | block-scoped | tx-scoped |
|---|---:|---:|
| first swap of a block, no charge | 34,559 | 30,214 |
| 2nd transaction in the same block, no charge | 18,607 | 30,214 |
| 2nd swap in one transaction, no charge | 7,307 | 13,823 |
| charged swap, vault already holds the token | 48,177 | 44,707 |
| charged swap, vault holds none of the token | 65,277 | 61,807 |
| charged 2nd transaction in the same block | 35,129 | 44,707 |
| invalid oracle, first swap of a transaction | 20,094 | 23,249 |
| invalid oracle, 2nd swap of a transaction | 7,094 | 5,778 |
| first swap a pool ever sees, no charge | 85,859 | 30,214 |
| first swap a pool ever sees, charged, vault empty | 116,577 | 61,807 |

## The paper's 61.8k and 5.7k

Both paper figures are reproduced exactly. They measure different states from the cold
first swap of a transaction in steady state.

**Charged swap: 61.8k against 44,707.** The 61,807 comes from `HookGasSettled.t.sol`
(DECISIONS W12). That test measures the first swap of a fresh pool, and that swap is also
the vault's first mint of the charged token. Minting writes the vault's ERC-6909 balance
from zero to nonzero, which costs 20,000 + 2,100 gas instead of 2,900 + 2,100, so 17,100 more.

| case | settlement (charged minus uncharged) | charged overhead |
|---|---:|---:|
| vault balance zero before the mint | 31,593 | 61,807 |
| vault balance already nonzero | 14,493 | 44,707 |

The cold runs here give the same 61,807 for `charged_vault_empty` and for
`charged_first_ever` (the exact conditions of `HookGasSettled`). With a nonzero vault
balance, the charged overhead is 44,707.

Both are correct, for different vault states:

- 61.8k applies once per vault and token, or again after the vault withdraws its whole
  balance.
- 44.7k is the cost of a charged swap once the vault holds claims in that token. For a vault
  that accumulates claims, this is the figure for a typical charged swap.

**Invalid oracle: 5.7k against 23.2k.** The 5.7k is `fail_open_overhead_median` in
`results/e7/gas_profile.json`. It is the median over all 248 invalid-oracle rows of
`HookGasBenchmark`. That test runs every scenario inside one transaction, so after its very
first swap everything is warm.

- 208 of the 248 rows are the 2nd to 16th fragment of a transaction. The transaction-scoped
  hook has already disabled the pool in transient storage for those, so they cost only
  5,482 to 5,763 (median 5,676). They dominate the median.
- The 40 first-fragment rows cost 12,253 to 12,536 warm.
- Measured cold here, the first invalid swap of a transaction costs 23,249. The 2nd costs
  5,778, which matches the paper.

The figure for "a swap with an invalid oracle" is therefore 23.2k. The 5.7k describes only
later swaps of a transaction that already saw the invalid oracle. The block-scoped hook
re-reads the oracle on every swap until a valid read opens a scope: 20,094 for the first
invalid swap, 7,094 for the 2nd, which reads warm.

The paper's other two figures, 30.2k for a single swap and 13.8k per extra fragment, are cold
and match this table.

## Additional arbitrage transaction

Cost = 21,000 + the swap without a hook + the hook overhead of the scenario that starts the
transaction. It is priced at the study window's median of 5.449e-7 USD per gas. That rate is
the P50 of `C_gas_actual_usd` (0.0982 USD) in `results/e7/tables/e7_smin_gas_conditions.csv`,
divided by its g_hat of 180,214. This is how the paper prices its extra transaction. The swap
is a cold single-range swap on a pool in steady state: 100,036 gas.

| hook, case | gas | USD |
|---|---:|---:|
| tx-scoped: extra transaction, no charge | 151,250 | 0.0824 |
| tx-scoped: extra transaction, charged (vault holds token) | 165,743 | 0.0903 |
| block-scoped: extra transaction in the same block, no charge | 139,643 | 0.0761 |
| block-scoped: extra transaction in the same block, charged | 156,165 | 0.0851 |
| block-scoped: extra transaction in a new block, no charge | 155,595 | 0.0848 |
| block-scoped: extra transaction in a new block, charged | 169,213 | 0.0922 |

The paper's 185,180 gas (0.1009 USD) is 21,000 + 133,966 + 30,214. Its 133,966 is the first
swap of a freshly initialized pool from `HookGasIsolated`. Here that swap costs 131,760,
which is 31,724 more than a steady-state swap:

- 17,100 of the difference is the pool's global fee growth going from zero to nonzero. A state
  diff confirmed this as the swap's only zero-to-nonzero write.
- The remaining ~14.6k is not attributed. Both swaps change the same two PoolManager slots.

On that swap basis, every extra-transaction cost above rises by about 31.7k gas, or 0.017 USD.

Under the block scope, a split across transactions in the same block no longer saves kappa,
so its extra transaction is pure cost to the arbitrager. Under the transaction scope, each
extra transaction still saves up to (1 - gamma) kappa.
