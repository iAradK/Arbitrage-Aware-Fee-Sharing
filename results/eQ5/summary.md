# Q5: calibration of the relative proportional buffer (validation months)

`experiments/eQ5_buffer_calibration.py`, run on commit ed45895 (branch q5-buffer-calibration, on the merged block-scope head fcb66ae), clean tree. Gitignored inputs (cache, results/e1, results/e7, data/data/lido, data/data/gas) come from the main checkout; hashes are in `manifest_valid.json`. No test month was used, and `--split test` is refused.

**Setup.**
- The Q4 harness, block scope V1, τ̂ = 3 gwei, λ = 0.75, γ = 0.02, median R.
- The buffer is relative: ε_rel · P̂ · |ΔX| per transaction.
- **Check.** The p95 variant reproduces Q4 exactly in all three pools: its ε series, its observed-swap charges (tx, V1, standalone) and its cross-block rows (`eQ5_checks_valid.csv`).

**Variants** (all use E2's rolling procedure: candidates strictly earlier within 7 days, seeded from the preceding split):

| Variant | ε_rel |
|---|---|
| p95 (current) | rolling 95th percentile of \|P̂/P − 1\| |
| p975, p99 | rolling 97.5th / 99th percentile |
| dev_p95 | rolling 95th percentile within buckets of the pre-swap deviation \|log(P_pool/P̂)\|; edges at the training months' 50/80/95th percentiles of baseline-feasible candidates (ETH/USDC 5.0 / 7.1 / 11.2 bp; ETH/WBTC about 30 / 34 / 37 bp) |

## Observed swaps (Experiment C, block V1)

**ETH/USDC** (1,038 standalone-feasible swaps). For reference, Q4 gives the fixed buffer 104 tipped violations under V1 and 40 under the transaction scope.

| Variant | Transfer, USD (without the 294 ETH block) | (a) tipped | (b) spillover: n, USD | Coverage of overestimation on the 1,038 | Median buffer on them, USD |
|---|---|---|---|---|---|
| p95 | 38,722 (6,569) | 95 | 121, 102 | 90.8% | 9.81 |
| p975 | 37,185 (5,504) | **64** | 91, 72 | 93.6% | 12.63 |
| p99 | 35,669 (4,412) | **31** | 51, 47 | 96.9% | 16.28 |
| dev_p95 | 39,802 (7,879) | 162 | 181, 147 | 81.1% | 6.52 |

The fixed buffer ε_S covers 96.3% of the 1,038 swaps, matching Q4.

Coverage here is the share of feasible swaps with Ŝ − S ≤ buffer. For p95 that gives 90.8%, against the 90.1% in Q4's summary text; the stored files do not show how that 90.1% was computed.

**ETH/WBTC.** The 294 ETH block is not in this pool, so transfer has no "without" column.

| Variant | Offset: transfer USD / (a) / coverage | Raw: transfer USD / (a) / coverage |
|---|---|---|
| p95 | 1,480 / 3 / 99.6% | 1,799 / 4 / 99.8% |
| p975 | 1,108 / 3 / 99.6% | 1,459 / 4 / 99.8% |
| p99 | 276 / 0 / 100% | 471 / 1 / 99.8% |
| dev_p95 | 2,147 / 4 / 99.6% | 7,583 / 16 / 98.8% |

## Experiment B's harness, buffered rule (k = 15, d = 1), block V1, dynamic reference

| Pool | Variant | Unsplit transfer USD (vs p95) | Survival SW at N = 2 / 5 / 75 | MR at N = 2 / 5 / 75 |
|---|---|---|---|---|
| ETH/USDC | p95 | 24,239 | 94.5% / 83.4% / 61.7% | 63.8% / 38.4% / 23.0% |
| | p975 | 19,703 (−18.7%) | 95.6% / 85.5% / 70.3% | 69.9% / 44.8% / 39.9% |
| | p99 | 14,206 (−41.4%) | 96.6% / 88.3% / 76.4% | 76.6% / 49.0% / 40.3% |
| | dev_p95 | 25,645 (+5.8%) | 94.1% / 82.6% / 60.1% | 63.3% / 36.8% / 22.5% |
| ETH/WBTC raw | p95 / p975 / p99 / dev_p95 | 9,926 / 7,101 (−28%) / 4,745 (−52%) / 10,803 (+9%) | N=5: 82.5% / 83.4% / 83.4% / 82.5% | |
| ETH/WBTC offset | p95 / p975 / p99 / dev_p95 | 7,976 / 5,343 (−33%) / 3,691 (−54%) / 9,360 (+17%) | N=5: 83.4% / 81.0% / 84.9% / 81.0% | |

## Reading

**Higher quantiles trade transfer for fewer violations.**
- In ETH/USDC, tipped violations fall from 95 (p95) to 64 (p97.5) and 31 (p99); spillover and coverage improve with them.
- Five-block survival stays at or above 81% in every pool for every variant.
- The cost is transfer. Observed V1 transfer falls by 4% (p97.5) and 8% (p99) in ETH/USDC, and by 25–81% in ETH/WBTC. The replay's unsplit transfer falls by 19% / 41% (ETH/USDC) and by 28–54% (ETH/WBTC).
- The buffer scales with |ΔX| on every swap, so a larger ε shrinks every charge. Only a few swaps are violations.

**The deviation-conditioned variant fails, and the reason is its calibration population.**
- In the k = 1 baseline replay, the pool is re-corrected to the benchmark every minute. A candidate's deviation from P̂ is therefore mostly the reference lag itself, and deviation and error are *negatively* correlated (−0.40).
- So the top-deviation bucket gets the *smallest* ε. Its error P95 is 17.7 bp, against 34.1 bp in the lowest bucket.
- The observed feasible swaps sit far beyond the candidates' range: median deviation 35 bp, against a top edge of 11 bp. 924 of the 1,038 (89%) fall in the top bucket, where their actual error P95 is 58 bp.
- They therefore get a smaller buffer than under p95 (median 6.5 against 9.8 USD), and tipped violations rise to 162.
- The diagnostic is `experiments/eQ5_bucket_diagnostic.py` (ETH/USDC, validation candidates and observed swaps). It was committed after the run and imports only the committed Q5 module.

## Recommendation

**No variant meets all three criteria.** Every variant with fewer tipped violations than p95 loses far more than 5% of the replay's unsplit transfer:
- p97.5: −18.7%;
- p99: −41%.

The only variant within about 5% of the transfer (dev_p95, +5.8%) raises violations.

**If the transfer constraint can be relaxed, p97.5 is the simplest step.**
- One parameter changes.
- Tipped violations fall from 95 to 64, well below the fixed buffer's 104.
- Five-block survival rises to 85.5%.
- Observed transfer falls by 4% in ETH/USDC and about 20–25% in ETH/WBTC.

**Reaching the transaction scope's ~40 needs p99** (31 violations), at −41% to −54% of the replay transfer.

**A conditioned buffer may still work.** It would have to be calibrated on observed arbitrage swaps, or conditioned on trade size |ΔX|, rather than on the minute-corrected replay candidates. That is untested here.

## Files

| File | Contents |
|---|---|
| `eQ5_observed_swaps_valid.csv` | Observed-swap metrics per pool and variant |
| `eQ5_crossblock_valid.csv` | Unsplit transfer and survival per N |
| `eQ5_bucket_edges_train.csv` | Training-month edges and candidate counts |
| `eQ5_checks_valid.csv` | Reproduction of Q4 |
| `manifest_valid.json` | Commit, inputs and hashes, parameters |
