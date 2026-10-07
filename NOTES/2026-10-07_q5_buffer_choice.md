# Q5: choice of the buffer's error level (2026-10-07)

Validation months only. Branch `claude/q5-buffer-choice-c328ab`, cut from q5-buffer-calibration (0a7a65b).

| Run | Commit |
|---|---|
| Base outputs | ed45895 (committed in 0a7a65b) |
| Quantile grid (`--variants grid --tag grid`) | da10ef8 |
| Deviation variants (`--variants dev --tag dev`) | 1015dcb (byte-identical to a first run on da10ef8) |
| Diagnostic (`experiments/eQ5_dev_diagnostic.py`) | d216245 |
| Criteria table (`experiments/eQ5_criteria_table.py` → `results/eQ5/eQ5_criteria_valid.csv`) | d216245 |

All runs used a clean tree. Every run's p95 rows reproduce Q4 exactly (`eQ5_checks_*`).

**Outputs are not committed** (user's instruction). They are left untracked in this worktree, under `results/eQ5/`:
- grid: `eQ5_observed_swaps_grid_valid.csv`, `eQ5_crossblock_grid_valid.csv`, `eQ5_checks_grid_valid.csv`, `eQ5_bucket_edges_grid_train.csv`, `manifest_grid_valid.json`;
- dev: the same five files with `_dev_`;
- diagnostic: `eQ5_dev_diagnostic_valid.csv`, `manifest_dev_diagnostic_valid.json`;
- criteria table: `eQ5_criteria_valid.csv`;
- summary addendum: `summary_extension.md`.

The base outputs from 0a7a65b stay as they were committed. Each output reproduces by rerunning its script on the commit named above.

**Criteria (ETH/USDC):**
- tipped violations (a) below 104, ideally about 40;
- five-block survival (SW, N = 5) of at least 75%;
- unsplit transfer in Experiment B's harness within 5% of p95.

## Decision: keep ε_rel at the rolling 95th percentile

It is only a configuration value, and the contract needs no extension.

### Quantile frontier (ETH/USDC; ETH/WBTC unsplit transfer change in brackets, raw / corr24h)

| ε | Tipped | Unsplit transfer vs p95 | Survival at N = 5 | Spillover n | Coverage |
|---|---|---|---|---|---|
| p95 | 95 | 24,239 USD (0) | 83.4% | 121 | 90.8% |
| p95.5 | 89 | −3.9% (−4.5 / −4.2%) | 83.9% | 118 | 92.1% |
| p96 | 82 | −7.5% | 84.3% | 118 | 93.3% |
| p97 | 75 | −15.1% | 85.2% | 97 | 93.4% |
| p97.5 | 64 | −18.7% | 85.5% | 91 | 93.6% |
| p98 | 54 | −23.2% | 85.7% | 82 | 93.7% |
| p98.5 | 43 | −31.6% | 86.9% | 66 | 94.7% |
| p99 | 31 | −41.4% | 88.3% | 51 | 96.9% |

- Only p95 and p95.5 meet all three criteria. Survival is never binding: it is at least 80.6% in every pool for every quantile.
- About 40 violations needs p98.5–p99, at a −32% to −41% transfer cost (−46% to −54% in ETH/WBTC).
- Why p95 over p95.5:
  - p95.5 trades 6 violations (−6%) for a certain −3.9% to −4.5% of transfer. That leaves about 0.5 pp of margin to the 5% bound in ETH/WBTC raw.
  - That exchange rate is no better than anywhere else on the frontier.
  - It would replace the conventional level with a tuned 0.955.
- If the transfer budget is to be spent anyway, p95.5 is the admissible alternative.

### Deviation-based buffers fail the transfer criterion

The variants tested set ε = max(p95, c·(dev − m·fee)⁺) or p95 + c·(dev − m·fee)⁺, with:
- c ∈ {0.25, 0.5, 1};
- m ∈ {2, 4, 6};
- dev = |log(P_pool / P̂)| at the opening of the block's scope.

In the split harness, dev is recomputed at each block.

**Observed swaps.** The deviation variants work well here. For example:
- add_c0.5_m6: 38 tipped at −29% observed ex-event transfer;
- add_c0.25_m6: 64 tipped at −16%.

That is only marginally better than the quantile frontier on the same axis (p98.5: 43 at −26%; p97.5: 64 at −16%).

**Replay.** They fail Experiment B's unsplit transfer:
- add_c0.25_m6: −52% at 64 tipped;
- max_c0.25_m6: −17% with no fewer violations (95);
- c ≥ 0.5 removes 56–100% of the transfer.

The diagnostic (`eQ5_dev_diagnostic_valid.csv`) shows why. The two populations occupy the same deviation range with opposite error behaviour, so any ε that rises with deviation cuts both:

| ETH/USDC | Share at dev ≥ 50 bp | Overestimation p95 there | corr(dev, overestimation) |
|---|---|---|---|
| Replay, by p95 charge (median dev 99 bp) | 90.6% of the transfer | 12 bp | −0.21 |
| Observed p95 tipped swaps (median 68 bp) | 77.9% | 76 bp | +0.43 |

Why the populations differ:
- In the replay, the reference error (one-minute lag) is independent of where the 15-minute-corrected pool sits.
- On real swaps, a large apparent deviation is often the reference being wrong.

A different bucketing, or a direct error-on-deviation fit, is still an increasing function of dev, so it hits the same overlap. ETH/WBTC agrees:
- the variants within 5% of transfer (m = 6, and m = 4 with c ≤ 0.5) leave the 3–4 violations unchanged;
- those that remove violations cost 9–87%. Caveat: under the deviation variants the observed "standalone" scope uses each swap's own deviation, so spillover is not comparable to the quantile rows.

**Not built.** A deviation buffer would also have needed:
- a `beforeSwap` permission, which means a new hook address, to read the pre-swap sqrtPrice at scope opening (about 2–3k gas per swap);
- a push-away manipulation check: moving the pool away in block n − 1 to enlarge block n's buffer.

## For the final reruns

**Python.** Use ε_rel = the rolling 0.95 quantile of |P̂ / P − 1|, exactly as Q4 and the Q5 p95 rows (e2.yml `eps_quantile: 0.95`, `eps_window_days: 7`):
- the population is the baseline-feasible candidates of the k = 1, d = 1 baseline replay (config 0) of the same split;
- only candidates stamped strictly earlier, within the trailing 7 days, are used;
- with fewer than 20 in the window, all history is used;
- the seed is the last 8 days of the preceding split (validation from the end of training; test from the end of validation);
- each swap uses ceil(ε · WAD), locked at the block's scope opening.

**Contract.** `epsilonRelPpb` = ceil(10⁹ · that rolling value), pushed by `epsilonAdmin`.
- The fixed 2,830,000 ppb (0.283%, the ETH/USDC validation median of 0.2826%) stays the value for the Foundry tests and gas runs.
- The rolling series ends the validation months at 0.261% (ETH/USDC) and 0.146% / 0.160% (ETH/WBTC raw / corr24h).
- A constant frozen at the end of validation would be a different, unevaluated design.
