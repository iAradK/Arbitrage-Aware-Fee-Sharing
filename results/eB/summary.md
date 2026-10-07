# Experiment B: splitting a correction across consecutive blocks (validation months)

`experiments/eB_cross_block_split.py`, run on commit 3555e77 (branch eB-cross-block). The run was clean except for `data/data/binance/*_1m.csv.gz`: the main checkout's current klines, uncommitted there, with their sha256 in `manifest_valid.json`. No test month was used. Opportunities run from 2026-04-01 to 2026-05-31; the 3 whose 75-block horizon would reach June were dropped.

## Setup

**Opportunities.** The executed corrections of two E2 replays on the validation months (median R, λ = 0.75, γ = 0.02):

| Rule | Mechanism | Cadence k | Delay d | ETH/USDC | ETH/WBTC raw / offset |
|---|---|---|---|---|---|
| Ideal | retained margin | 1 | 0 | 8,567 | 892 / 750 |
| Buffered | δ = ε_S, rolling | 15 | 1 | 2,605 | 515 / 442 |

The frozen `simulate()` was re-executed in memory to recover each correction's pool state. Its totals equal the stored `e2_summary_valid_*` files exactly (`eB_checks_valid.csv`).

**Blocks.** An opportunity at minute t uses the next N chain blocks (`cache/block_gas`), N ∈ {1, 2, 5, 75}; 75 blocks is about 15 minutes. In block b:

- Benchmark π_b and hook reference ρ_b are the lookahead-free grid values at the block's minute (1-minute bars).
- κ_b = 180,214 gas × (base fee_b + τ̂) × ETH + R + δ, the hook's Eq. 9, with τ̂ = 0.05 gwei.
- Each block is its own scope.

**Plan.** The plan uses start information only: pool state, π₀, ρ₀, κ₀ and the gas price, all assumed to stay. The arbitrager picks the correction size (E2's 200-point grid) and l ≤ N pieces:

- The remainder goes in block 0.
- Then come l − 1 pieces of κ₀ − 1 unit. That is the cheapest split, because F is 0 up to κ and concave above it.

**Costs.**

- Block 0 pays E2's transaction cost of 180,214 gas.
- Each extra block pays 155,595 gas, or 169,213 if it is charged (`config/gas_block_scope.json`, the new-block rows of `results/gas/gas_cold.csv`).

**Execution.** The plan executes as fixed and is valued ex post. True surplus uses π_b, the charge uses ρ_b and κ_b, and gas uses each block's gas price. No other trader acts during the split.

**Second mode.** "Constant" values the same plan at the start values throughout, which is the bracket's assumption.

**Metrics.** The surviving share is the split's total charge over the unsplit charge. "SW" (primary) is sum-weighted; "MR" is the mean of per-correction ratios.

## Results: κ from the base fee, moving reference (primary)

`eB_summary_valid.csv`, kappa = basefee, mode = dynamic. Payoff gains are in USD; price error in basis points of time-weighted |log(pool / benchmark)| over the N-block horizon.

| Pool | Rule | N | Surviving SW | Surviving MR | Splits planned | Profitable ex post (of planned splits) | Gain median (splits) / sum | Extra price error (both executed): mean, relative | New violations / unsplit violations |
|---|---|---|---|---|---|---|---|---|---|
| ETH/USDC | Ideal | 2 | **85.9%** | 37.8% | 89.2% | 88.3% (99.0%) | 1.18 / 8,635 | +3.9 bp, +78% | 46 / 2,465 |
| | | 5 | **57.1%** | 13.2% | 89.3% | 88.2% (98.8%) | 1.88 / 13,608 | +4.7 bp, +94% | 63 / 2,465 |
| | | 75 | **48.7%** | 11.1% | 89.3% | 85.4% (95.7%) | 1.73 / 12,537 | +0.7 bp, +5% | 244 / 2,465 |
| ETH/USDC | Buffered | 2 | **76.8%** | 48.7% | 20.0% | 20.0% (99.6%) | 17.14 / 8,180 | +1.9 bp, +25% | 0 / 1 |
| | | 5 | **50.3%** | 11.8% | 20.0% | 20.0% (99.6%) | 25.45 / 17,546 | +1.2 bp, +16% | 0 / 1 |
| | | 75 | **26.6%** | 8.3% | 20.0% | 19.8% (98.7%) | 24.07 / 25,034 | −1.2 bp, −8% | 0 / 1 |
| ETH/WBTC raw | Ideal | 2 / 5 / 75 | 84.9% / 60.6% / 56.4% | 33.2% / 11.0% / 10.8% | 95.5% | 95.0% / 94.8% / 92.8% | 3.68 / 5.82 / 5.45 (median) | +3.1 / +3.5 / +0.8 bp | 3 / 3 / 18 of 883 (248 unsplit) |
| ETH/WBTC raw | Buffered | 2 / 5 / 75 | 72.0% / 43.2% / 22.2% | 34.6% / 23.8% / 8.6% | 21.4% | 21.2% / 21.2% / 20.6% | 30.19 / 43.42 / 41.20 | +1.7 / +1.2 / −1.0 bp | 0 |
| ETH/WBTC offset | Ideal | 2 / 5 / 75 | 87.1% / 64.4% / 49.2% | 36.8% / 12.9% / 10.1% | 94.0% | 93.5% / 93.3% / 91.1% | 4.38 / 6.56 / 6.29 | +3.2 / +3.9 / +1.1 bp | 2 / 2 / 17 of 740 (208 unsplit) |
| ETH/WBTC offset | Buffered | 2 / 5 / 75 | 73.6% / 44.2% / 16.9% | 39.7% / 9.5% / 15.6% | 21.0% | 21.0% / 21.0% / 20.6% | 35.44 / 60.50 / 46.21 | +1.5 / +1.1 / −1.3 bp | 0 |

**Reading.**

- **Most of the money survives at N=2 and N=5, much less per correction.** About half the transfer survives a five-block split in every pool. The mean of per-correction ratios is far lower (11–24%): small corrections lose almost everything, while large ones keep much of theirs.
- **The arbitrager almost always gains.** For the ideal rule it plans a split for 89–96% of corrections, and 96–99% of those splits pay off ex post. Under the buffered rule only charged corrections split, about 20%; nearly all of them pay off, with a median gain of 17–60 USD.
- **Splitting raises the price error while the split lasts.** Over 2–5 blocks the extra error is +1 to +5 bp. Over 75 blocks it is small: the pieces finish in a few blocks, so the extra error is diluted over 15 minutes.
  - Under the buffered rule the 75-block figure is negative (−1 bp). An unsplit buffered correction often stops short: on the part of the surplus the hook charges at a marginal 1 − γ, a stale reference can make further correction unprofitable. The cheaper split corrects almost fully (mean correction 99% against 94% unsplit), which then lowers the error.
  - The unrestricted mean (`extra_twe_bp`) also counts corrections that only the split executes, so it is negative at N=75 for every rule (ideal ETH/USDC −0.8 bp). That is why the table restricts to corrections executed both ways.
- **Violations: the split adds few, the base-fee κ causes most.**
  - **New violations from splitting.** These are baseline-feasible corrections whose realized payoff falls below R, or which no longer execute, while the unsplit run was fine. Under the ideal rule there are 46, 63 and 244 of 8,398 in ETH/USDC (0.5%, 0.8%, 2.9%), and 2–18 in ETH/WBTC. They come from plans made at the start that turn out worse when the price, base fee or gas price moves. Under the buffered rule there are none.
  - **Violations of the unsplit ideal rule.** These are much larger: 2,465 of 8,398 in ETH/USDC (29%), 248 of 883 and 208 of 740 in ETH/WBTC. The cause is κ, not splitting. With τ̂ = 0.05 gwei, the hook's κ is below the gas the arbitrager pays at the block's median tip, and the retained margin γ(S − κ) cannot cover the gap on small corrections. Under E2's κ at the actual gas price (sensitivity below) the unsplit run has 0 violations.
  - **Splitting can also rescue.** It lowers the total from 2,465 to 716 at N=2, because charging less per transaction rescues many of these.

## Sensitivity: κ at the block's actual gas price (E2's K̂)

| Pool | Rule | SW at N = 2 / 5 / 75 | MR at N = 2 / 5 / 75 | New violations from split (unsplit: 0) |
|---|---|---|---|---|
| ETH/USDC | Ideal | 77.3% / 52.0% / 44.1% | 37.7% / 18.1% / 16.6% | 119 / 132 / 311 of 8,398 |
| ETH/USDC | Buffered | 77.4% / 50.9% / 27.0% | 126% / 89% / 85% | 0 |
| ETH/WBTC raw | Ideal / Buffered | 79.3 / 57.0 / 53.4%; 72.0 / 43.3 / 22.2% | 30.0 / 12.7 / 12.7%; 50.8 / 41.2 / 23.7% | 8 / 8 / 23; 0 |
| ETH/WBTC offset | Ideal / Buffered | 81.5 / 60.6 / 46.4%; 73.6 / 44.2 / 17.1% | 34.3 / 15.0 / 13.0%; 43.7 / 16.6 / 21.3% | 7 / 7 / 22; 0 |

The sum-weighted shares change by at most 10 percentage points. Two other results also hold under this κ:

- **Buffered MR above 100% at N=2.** Some buffered corrections have a tiny unsplit charge and pay more when split, because a piece gets charged after the base fee or reference moves. Few corrections carry these large ratios, so the sum-weighted share is the reliable measure.
- **New violations in the ideal run.** The ideal rule's new violations remain (311 at N=75). They are the ex-post risk of a fixed plan, not a κ artifact.

## Comparison with the constant-reference bracket (`eB_bracket_comparison_valid.csv`, ETH/USDC, sum-weighted)

| Rule | N | Bracket, eA_fu2 (test months) | Constant reference, validation months (base-fee κ / gas-price κ) | Moving reference, validation months (base-fee κ / gas-price κ) |
|---|---|---|---|---|
| Ideal | 5 | 50.3% | 56.9% / 50.5% | **57.1%** / 52.0% |
| Ideal | 75 | 14.7% | 19.4% / 17.7% | **48.7%** / 44.1% |
| Buffered | 5 | 60.5% | 50.3% / 50.6% | **50.3%** / 50.9% |
| Buffered | 75 | 6.5% | 11.2% / 11.2% | **26.6%** / 27.0% |

**Why the results differ from the bracket.**

1. **At N=5 the moving reference changes almost nothing.** Constant and moving values differ by at most 1.5 points. Five blocks take under a minute, and the benchmark and the delayed reference come from 1-minute bars, so within the split only κ (the base fee) moves. The remaining gap to the bracket comes from the sample and the cost model:
   - With E2's gas-price κ, the constant mode gives 50.5% in the ideal case, almost exactly the bracket's 50.3%.
   - The buffered gap (50.3% against 60.5%) is mostly the sample. Validation and test months have different correction sizes, and the bracket's cap binds for 80% of its test-month transfer at l ≤ 5.
2. **At N=75 the moving reference is the main effect.** It takes surviving recovery from 19% to 49% (ideal) and from 11% to 27% (buffered). Each piece is planned just below the charging threshold. Over 15 minutes the reference and the base fee move, and whenever a piece's estimated surplus ends up above its block's κ, that piece is charged. A piece above the threshold pays about (1 − γ) times the excess, and one below pays nothing, so random movement charges a positive amount on average, like an option the hook holds against each piece. The arbitrager also bears the downside of the moves: its realized gain at N=75 (median 1.73 USD, ideal ETH/USDC) is below the N=5 gain (1.88).
3. **The constant modes run a little above the bracket at N=75: 19.4% against 14.7% (ideal), 11.2% against 6.5% (buffered).** Three differences contribute:
   - Validation months instead of test months.
   - The arbitrager re-chooses the correction size here, while the bracket kept E2's executed size.
   - Extra transactions cost 155,595 gas here, against the bracket's 185,180 plus settlement.

## Limitations

- **1-minute benchmark.** Price moves within a minute are not modelled. A finer benchmark would increase the movement effect at N=5.
- **Each opportunity is evaluated alone.** Under the ideal rule (k=1) the next replay step comes 5 blocks later. At N ≥ 5 a real split would overlap the next corrections, by this arbitrager or a competitor, and neither is simulated.
- **Fixed plans.** The arbitrager does not adapt its plan after block 0. An adaptive arbitrager would avoid pieces that turn out charged, so the survival at N=75 is an upper bound for this mechanism.
- **Delayed reference.** With d = 1 the reference catches up to the start benchmark after 60 s. An arbitrager who anticipates that could time its pieces; this is not modelled.
- **Cost of block 0.** Block 0 pays E2's 180,214 gas, an assumed 150k swap plus the hook. Extra blocks pay the measured 155,595 or 169,213, so a split's later transactions are slightly cheaper than its first.

## Files

| File | Contents |
|---|---|
| `eB_opportunities_valid.csv.gz` | One row per opportunity, κ convention, mode and N |
| `eB_summary_valid.csv` | All metrics per pool, rule, κ, mode and N |
| `eB_bracket_comparison_valid.csv` | Bracket comparison |
| `eB_checks_valid.csv` | E2 reproduction checks |
| `manifest_valid.json` | Commit, uncommitted files, inputs with sha256, parameters, checks, output hashes |
