# E2 validation months with the block-scoped hook's gas (`_blockgas` files)

Produced by `experiments/e2_blockgas.py` on code commit `eed22ff` (branch `wt-e2gas`). The run reads the
gas figures in `config/gas_block_scope.json` (commit `4811cd2`). Validation months (April–May 2026) only;
no test-month run.

## How the runs were set up

**Inputs.** The stored E2 results come from an uncommitted state of the main checkout, so
`5302d2b` snapshots that state on this branch:

- `common/pools.py`, `common/data_io.py`, `experiments/e2_sequential_replay.py` and
  `experiments/configs/e2.yml`, copied verbatim. The config hash is `bb532f47`, equal to the frozen
  hash.
- The five modified Binance files are copied in but not committed. They, the Lido events and the
  aligned caches match the stored manifests' SHA-256 values: 9 of 9 present inputs, plus Lido.

**Reproduction check.** Mode `asfrozen` reruns the stored setting, which charges 30,214 gas for
every correction. Every row of `e2_summary_valid_lag0_med` and `e2_summary_valid_lag1_k15_med`
is identical: 540 rows × 30 columns, 0 differing columns. The checks are in
`e2_asfrozen_check_*_blockgas.json`.

**Gas.** Each correction is the first swap of its block.

| case | gas |
|---|---:|
| uncharged correction | 150,000 + 34,559 |
| charged correction (r > 0) | 150,000 + 48,177 |
| sensitivity: charged, empty vault (`_blockgas_emptyvault`) | 150,000 + 65,277 |

- The hook's g-hat is the charged amount, as in DECISIONS I3: K̂ matters only when the hook
  charges.
- The baseline and the ε_S candidate log pay the uncharged amount.
- R, the median regime and the rolling ε_S window are unchanged.

**Rules compared**, both at λ = 0.75 and γ = 0.02:

- **ideal:** retained margin, lag 0, cadence 1 (tag `valid_lag0_med`)
- **buffered:** δ = ε_S, lag 1, cadence 15 (tag `valid_lag1_k15_med`)

## Results

Relative changes against the stored results. Every pool and variant is in
`tables/e2_blockgas_comparison_valid.csv`.

| | ideal, 48,177 | ideal, 65,277 | buffered, 48,177 | buffered, 65,277 |
|---|---:|---:|---:|---:|
| funds, ETH/USDC | −0.56% | −2.41% | −0.35% | −0.49% |
| funds, ETH/WBTC offset | +1.34% | +0.71% | −0.09% | −0.19% |
| funds, ETH/wstETH offset (94 / 127 USD stored) | −26.2% | −22.9% | −25.8% | −28.7% |
| funds, all nine pool-variants | +0.32% | −0.84% | −0.79% | −0.93% |
| mean price error, ETH/USDC | +1.94% | +4.26% | +0.20% | +0.19% |
| mean price error, ETH/WBTC offset | +0.55% | +1.14% | −0.01% | +0.02% |
| executed corrections, ETH/USDC | +3.5% | +9.3% | −0.04% | 0 |
| execution rate | 0 (1.000) | 0 | 0 | 0 |
| participation violations | 0 (none) | 0 | +0.04% (ETH/USDC rate 0.77%), others ≤ 0.24% | −0.95% to +0% |

A charged correction now pays more gas. The searcher therefore stops some corrections short,
which leaves more but smaller corrections and a slightly higher price error. In ETH/wstETH the
stored funds are tens of USD from fewer than 100 corrections, so its relative changes are
large but small in USD: −25 USD under the ideal rule.

## Corrections removed by the hook's gas

X = |F150 and not F(150 + g)| / |F150| on the states of the no-hook path, as in
`e2_hook_gas.py`. The table is `tables/e2_blockgas_removed_valid.csv`.

| ideal run | 30,214 | 34,559 | 48,177 | 65,277 |
|---|---:|---:|---:|---:|
| ETH/USDC X | 2.40% | 2.67% (+11.5%) | 3.58% (+49.5%) | 4.56% (+90.4%) |
| ETH/WBTC offset X | 1.08% | 1.08% (0) | 2.15% (+100%) | 3.10% (+188%) |

- The corrections that the hook's gas removes barely clear R, so they are uncharged. The
  34,559 column is therefore the relevant one, and the charged columns are upper bounds.
- The no-hook path itself is identical to the stored one (asserted).
- Rescaling the stored hook cost reproduces the replay's own feasibility counts at 30,214 and
  at 34,559 exactly.
- `e2_nohook_steps_*_blockgas*.parquet` (2 × 20.8 MB, 2 × 0.9 MB) are not committed. Their
  content is the removal table, and they regenerate from this commit.
