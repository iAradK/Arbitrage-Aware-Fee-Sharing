# Q4: a proportional buffer ε_P·|ΔX| instead of the fixed buffer δ (validation months)

Branch `q4-prop-buffer`: `63cd747` (Q3, on the frozen analysis state `efca476` plus Experiment B), with Experiment C and Q2 cherry-picked. Both runs were on a clean tree:

| Part | Script | Commit | Manifest |
|---|---|---|---|
| 1–2 | `experiments/eQ4_prop_buffer.py` | 1a258b3 | `manifest_swaps_valid.json` |
| 3–4 | `experiments/eQ4_splitting.py` | 45c4cb5 | `manifest_splitting_valid.json` |

The klines and the gitignored inputs come from the main checkout; their hashes are in the manifests. The test months are refused by both scripts. The reference is `ScopedHookReference(buffer="abs" | "rel", eps_wad=…)` (ef0d0d4, `tests/test_proportional_buffer.py`). The Solidity hook is unchanged.

**Rules** (τ̂ = 3 gwei in K̂ throughout; λ = 0.75, γ = 0.02, median R):

| Rule | κ | Buffer |
|---|---|---|
| none (δ = 0) | K̂ = R + 180,214 gas × (base fee + τ̂) | none |
| (i) fixed | K̂ + δ, δ = ε_S (rolling, d = 1) | inside κ, once per scope |
| (ii) prop_abs | K̂ | each transaction's bracket [Ŝ_tx − ε_P·G_tx]⁺, G_tx its gross \|ΔX\| |
| (ii) prop_rel | K̂ | [Ŝ_tx − ε_rel·P̂·G_tx]⁺ |

λ applies to the buffered surplus.

**The buffer as a shifted reference.** With token Y as numeraire, [Ŝ − ε|ΔX|]⁺ is the surplus at a reference shifted against the trade: ρ − ε when buying ETH and ρ + ε when selling, or ρ(1 ∓ ε_rel) for the relative version. That is how parts 3–4 plan and value it.

## 1. Calibration of ε_P(d = 1) (`eQ4_calibration_valid.csv`, `eQ4_eps_series_*`)

The procedure is E2's rolling ε_S procedure unchanged:
- P95 over the baseline-feasible candidates of the k = 1 baseline replay;
- candidates strictly earlier, within 7 days;
- seeded with the end of the training months.

The same code reproduces E2's ε_S exactly, which is asserted. It also equals Experiment C's δ at every swap.

| Pool | ε_S median (P10–P90), USD | ε_P absolute median | ε_rel median | Coverage on candidates: ε_S / ε_P / ε_rel |
|---|---|---|---|---|
| ETH/USDC | 16.80 (13.8–20.8) | 6.19 USDC per ETH (28.6 bp of price) | 0.283% | 95.1% / 95.3% / 95.4% |
| ETH/WBTC offset | 73.99 (28.4–113.1) | 9.0e-5 WBTC per ETH (28.9 bp) | 0.287% | 94.6% / 95.0% / 94.5% |
| ETH/WBTC raw | 58.95 (31.9–95.2) | 8.0e-5 WBTC per ETH (26.0 bp) | 0.263% | 94.5% / 95.3% / 94.8% |

Coverage is out of sample: the error is compared with the bound from strictly earlier data.

**On the observed swaps** (`eQ4_coverage_swaps_valid.csv`, |Ŝ − S| against the buffer):

| Pool | Swaps | Coverage: ε_S / ε_P·\|ΔX\| / ε_rel·P̂·\|ΔX\| | Median buffer, USD: fixed / proportional |
|---|---|---|---|
| ETH/USDC | all (98,087) | 99.4% / 98.3% / 98.3% | 17.06 / 1.99 |
| ETH/USDC | arbitrage (20,326) | 99.2% / 97.2% / 97.3% | 17.06 / 2.75 |
| ETH/WBTC offset | arbitrage (1,364) | 98.8% / 98.4% / 98.1% | 77.30 / 13.17 |
| ETH/WBTC raw | arbitrage (2,023) | 99.2% / 99.4% / 99.2% | 65.25 / 12.75 |

- **Size.** The proportional buffer is a fraction of the fixed one (median 2 against 17 USD in ETH/USDC).
- **Weakness.** Its coverage is weakest on the large-deviation swaps that are feasible standalone. On the 1,038 ETH/USDC swaps with margin ≥ 0 it covers overestimation in 90.1% of cases, against 96.3% for ε_S. In ETH/WBTC offset both cover 99.6%.

## 2. Observed swaps of Experiment C (`eQ4_swaps_metrics_valid.csv`, per swap: `eQ4_swaps_charges_valid.csv.gz`)

**Check.** With no buffer and the old τ̂ (0.05 gwei), the replay reproduces Q2's transaction-scope, V1 and standalone charges exactly.

**Metrics:**
- **(a)** tipped violations of the swaps feasible standalone;
- **(b)** spillover: n and USD;
- **(c1)** reversal leak;
- **(c2)** netting shortfall against the rule's own standalone charges;
- **(d)** non-arbitrage charges: n and USD.

The transfer column gives "all swaps (without the 294 ETH block)". Without that block, only transfer and (c2) move; the other columns are unchanged.

**ETH/USDC** (1,038 swaps feasible standalone):

| Rule | Scope | Transfer USD | (a) | (b) | (c1) | (c2) | (d) |
|---|---|---|---|---|---|---|---|
| none | tx | 49,074 (15,436) | 398 | 35, 33 | 0 | 47 | 468, 2,632 |
| none | V1 | 49,563 (15,926) | 446 | 410, 267 | 0 | 47 | 601, 2,752 |
| (i) fixed | tx | 42,157 (8,519) | **40** | 16, 63 | 0 | 0.3 | 34, 729 |
| (i) fixed | V1 | 42,995 (9,357) | **104** | **138, 709** | 0 | 0.3 | 52, 856 |
| (ii) prop_abs | tx | 38,517 (6,394) | **73** | 15, 18 | 0 | 4.1 | 64, 609 |
| (ii) prop_abs | V1 | 38,706 (6,583) | **95** | **132, 113** | 0 | 4.1 | 89, 637 |
| (ii) prop_rel | tx | 38,538 (6,385) | 74 | 14, 17 | 0 | 4.6 | 63, 610 |
| (ii) prop_rel | V1 | 38,722 (6,569) | 95 | 121, 102 | 0 | 4.6 | 87, 635 |

**ETH/WBTC offset** (835 feasible; the event is not in this pool):

| Rule | Transfer tx / V1 | (a) tx / V1 | (b) V1 |
|---|---|---|---|
| none | 6,890 / 6,919 | 71 / 75 | 10, 21 USD |
| (i) fixed | 2,309 / 2,309 | 3 / 3 | 0 |
| (ii) prop_abs / prop_rel | 1,471 / 1,480 (both scopes) | 3 / 3 | 0 |

**ETH/WBTC raw:**
- Fixed: transfer 2,636 / 2,653 (tx / V1); (a) 4 / 6; (b) 2 swaps, 17 USD.
- prop_abs: 1,771 / 1,780; (a) 3 / 4; (b) 4 swaps, 9 USD.

## 3. Splitting

**Corrections.** The executed corrections of E2's buffered rule (k = 15, d = 1) in the validation months, rebuilt with pool state by Experiment B's harness: ETH/USDC 2,605, ETH/WBTC raw 515, offset 442.
- **Reproduction check.** The fixed rule at Experiment B's τ̂ reproduces all 28,496 stored buffered rows (κ = base fee) exactly: N, mode, both executed flags, l*, charges and payoffs.
- **Correction size.** Each rule picks its own unsplit correction size (Experiment B's N = 1 plan).

### 3a. Within one block, block V1 (`eQ4_within_block_check_valid.csv`)

**Setup.** Each unsplit correction is split into every l = 2..cap transactions of its first block, cap = ⌊gas limit / 185,180⌋ = 322–324. Two families:
- equal inputs;
- the rule's cheapest pieces (κ − 1 unit after the remainder).

Every piece is a transaction through the integer reference, with token amounts that sum exactly to the whole. In total 3,457,125 splits and 559,784,646 pieces were evaluated.

| Rule | Survival of the cheapest split, sum-weighted / mean of ratios: ETH/USDC | ETH/WBTC raw | ETH/WBTC offset |
|---|---|---|---|
| (i) fixed | 100.0% / 99.86% | 99.97% / 95.7% | 99.98% / 93.6% |
| (ii) prop_abs | 100.0% / 99.97% | 99.98% / 99.94% | 99.98% / 98.6% |
| (ii) prop_rel | 100.0% / 99.93% | 99.98% / 99.97% | 99.98% / 98.9% |

**Survival is 100% up to rounding.** Splitting gains at most about one numeraire base unit per extra transaction: the minimum deviation is −182 to −351 units, never beyond the number of pieces. The cause is the per-bracket truncation of the reference product, plus the buffer's per-bracket ceiling. In USD:
- ETH/USDC: at most 0.0003 USD over 324 transactions;
- ETH/WBTC: at most 3.5e-6 BTC, about 0.27 USD at the validation median of 75.9k USD per BTC, over 300+ transactions.

That is far below one transaction's gas. The mean of ratios dips in ETH/WBTC only because some charged corrections pay a few hundred base units in total.

**Splits can pay more.** The maximum deviation is +79 USD in ETH/USDC and +0.0031 BTC (about 236 USD) in ETH/WBTC. It occurs on 15–16% of corrections under the fixed rule and on 33–35% under the proportional rules. Under V1 a split pays Σ[bᵢ]⁺ against [Σbᵢ]⁺ unsplit. A gap above rounding therefore requires a piece with a negative bracket: the trade continues past the hook's lagged (or buffer-shifted) reference toward the benchmark, and that tail no longer nets against the rest. This costs the splitter, not the hook.

### 3b. Experiment A's optimal split under the transaction scope, with the block cap (`eQ4_tx_scope_optimal_split_valid.csv`)

| Rule | ETH/USDC: SW / MR (l* median) | ETH/WBTC raw | ETH/WBTC offset |
|---|---|---|---|
| (i) fixed | 0.06% / 18.0% (2) | 0.03% / 7.8% (2) | 0.02% / 10.6% (2) |
| (ii) prop_abs | 16.5% / 4.2% (6) | 0.06% / 4.1% (5) | 0.32% / 3.3% (7) |
| (ii) prop_rel | 16.6% / 3.6% (6) | 0.05% / 3.3% (5) | 0.25% / 3.4% (7) |

Under the transaction scope both buffers are defeated by splitting.
- **Fixed rule.** κ includes δ (about 17 USD in ETH/USDC), so two transactions avoid almost everything.
- **Proportional rule.** κ is only K̂, so more pieces are needed (median 6). In ETH/USDC the block cap binds, leaving 16.5% sum-weighted (uncapped: 0.7%).

## 4. Cross-block splitting, Experiment B's harness, block V1 (`eQ4_crossblock_summary_valid.csv`)

The setup is N blocks, one transaction each, with the moving reference ("dynamic", primary); surviving share = charge_N / charge_1.

| Pool | Rule | Charged (unsplit transfer USD) | SW at N = 2 / 5 / 75 | MR at N = 2 / 5 / 75 | New violations from split at N = 75 (unsplit violations) |
|---|---|---|---|---|---|
| ETH/USDC | (i) fixed | 472 (24,085) | 76.9% / 50.4% / 26.8% | 43.7% / 12.9% / 7.4% | 0 (1) |
| ETH/USDC | (ii) prop_abs | 647 (24,334) | **94.5% / 83.4% / 61.5%** | **67.0% / 39.0% / 23.3%** | 7 (3) |
| ETH/USDC | (ii) prop_rel | 644 (24,239) | 94.5% / 83.4% / 61.7% | 63.8% / 38.4% / 23.0% | 7 (3) |
| ETH/WBTC raw | (i) fixed | 103 (10,376) | 71.8% / 42.9% / 22.3% | 28.7% / 9.9% / 5.8% | 0 (0) |
| ETH/WBTC raw | (ii) prop_abs | 125 (9,929) | 94.4% / 82.6% / 62.1% | 64.4% / 38.5% / 23.6% | 1 (3) |
| ETH/WBTC offset | (i) fixed | 85 (8,435) | 73.5% / 43.8% / 16.7% | 45.8% / 7.7% / 25.3% | 0 (0) |
| ETH/WBTC offset | (ii) prop_abs | 91 (7,987) | 94.6% / 83.5% / 48.1% | 68.2% / 43.8% / 24.3% | 1 (1) |

prop_rel matches prop_abs within about 1 point in every pool.

**Constant reference** (the bracket), N = 75:
- Fixed: 11.2% (ETH/USDC), 0.01% and 0% (ETH/WBTC).
- prop_abs: 31.0%, 20.6% and 19.3%.

At N = 2 and 5 the constant and dynamic modes differ by less than 1 point.

## Reading

- **Against cross-block splitting.** The proportional buffer is markedly more robust, which is the main result. Each extra block lets the splitter avoid up to (1 − γ)κ, and the fixed rule puts δ ≈ ε_S (17 USD in ETH/USDC) inside κ. The proportional rule keeps κ = K̂ and charges the buffer per unit of volume, so splitting does not shrink it. Surviving transfer at N = 5 rises from 50% to 83% (ETH/USDC) and from 43% to 83% (ETH/WBTC); at N = 75 from 27% to 62%.
- **Same unsplit transfer.** In the harness the unsplit transfer is about the same (24.3k vs 24.1k USD in ETH/USDC), with more corrections charged (647 vs 472).
- **On observed swaps the proportional rule collects less.** Transfer is 38.5k against 42.2k USD (−9%, tx scope), and only 6.4k against 8.5k without the 294 ETH block. It also has more tipped violations under the transaction scope (73 vs 40). Its per-swap buffer is small (median 2 USD) and covers only 90% of the overestimation on the large swaps that are feasible standalone, against 96% for ε_S.
- **Block V1.** The proportional buffer cuts spillover charges from 709 to 113 USD and tipped violations from 104 to 95. That is because κ, deducted once per block, no longer carries δ.
- **Within one block.** Both buffers keep 100% under V1, up to about one base unit per extra transaction of rounding in the splitter's favour.
- **Absolute vs relative.** They are interchangeable here: P̂ moves little within a 7-day window.
- **Trade-off.** The proportional buffer protects better against splitting and spillover, but less against the reference error of large standalone swaps. A larger quantile for ε_P (e.g. P99), or ε_P conditional on deviation size, could close that gap. This was not tested.

## Files

| File | Contents |
|---|---|
| `eQ4_calibration_valid.csv`, `eQ4_eps_series_*_valid.csv.gz` | ε_S, ε_P, ε_rel per minute and their summaries |
| `eQ4_coverage_swaps_valid.csv` | Coverage and size on observed swaps |
| `eQ4_swaps_metrics_valid.csv`, `eQ4_swaps_charges_valid.csv.gz` | Part 2 metrics and per-swap charges |
| `eQ4_within_block_check_valid.csv` (+ `_per_correction`) | Part 3a |
| `eQ4_tx_scope_optimal_split_valid.csv` (+ `_per_correction`) | Part 3b |
| `eQ4_crossblock_summary_valid.csv`, `eQ4_crossblock_opportunities_valid.csv.gz` | Part 4 |
| `eQ4_checks_*_valid.csv` | Reproduction checks |
