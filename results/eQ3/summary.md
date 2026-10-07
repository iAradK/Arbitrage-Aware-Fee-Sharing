# Q3: tip estimate τ̂ in the hook's K̂ (validation months)

`experiments/eQ3_tip_quantile.py`, run on commit 25903ad (branch q3-tip-quantile). The tree was clean. The klines and the gitignored inputs come from the main checkout; their hashes are in `manifest_valid.json`. No test month was used, and `--split test` is refused.

## Setup

**Harness.** The run uses experiment B's harness at N = 1 (unsplit) for the ideal rule: retained margin, k = 1, d = 0, median R, λ = 0.75, γ = 0.02.

| Pool | Corrections | Baseline-feasible |
|---|---|---|
| ETH/USDC | 8,567 | 8,398 |
| ETH/WBTC raw | 892 | 883 |
| ETH/WBTC offset | 750 | 740 |

**Costs and κ.**
- The arbitrager pays 180,214 gas at block 0's gas price, which is the base fee plus that block's median priority fee.
- The hook's κ = 180,214 × (base fee + τ̂) × ETH + R.
- τ̂ is a quantile of the block median priority fee over the training months.
- "gasprice" is E2's K̂, which uses the actual gas price and so has no tip error.

**Reproduction check.** P50 (0.05 gwei, the current τ̂) and gasprice reproduce experiment B's unsplit rows exactly.

**Violations.** A violation is a baseline-feasible correction that ends below R or no longer executes. Transfer is shown as a share of the gasprice transfer.

## Requested settings

| τ̂ | gwei | ETH/USDC: violations / transfer | ETH/WBTC raw: violations / transfer | ETH/WBTC offset: violations / transfer |
|---|---|---|---|---|
| P50 (current) | 0.05 | 29.4% / 90.0% | 28.1% / 92.9% | 28.1% / 93.2% |
| P90 | 0.50 | 10.3% / 92.5% | 13.3% / 95.0% | 12.2% / 96.3% |
| P95 | 1.00 | 3.1% / 92.8% | 4.8% / 96.2% | 5.0% / 97.0% |
| P99 | 1.85 | 1.1% / 89.3% | 1.7% / 95.5% | 2.2% / 96.4% |
| Actual gas price (E2) | n/a | 0% / 100% (36,959 USD) | 0% / 100% (12,746 USD) | 0% / 100% (13,282 USD) |

Absolute transfers are in `eQ3_summary_valid.csv`.

**No requested quantile keeps violations below 1% in every pool.** P99 comes closest.

## Supplementary scan (training quantiles above P99)

| τ̂ | gwei | ETH/USDC | ETH/WBTC raw | ETH/WBTC offset |
|---|---|---|---|---|
| P99.25 / P99.5 | 2.0 | 0.64% / 88.7% | 1.36% / 95.3% | 1.62% / 96.3% |
| **P99.75** | **3.0** | **0.24% / 84.2%** | **0.79% / 93.5%** | **0.95% / 94.9%** |
| P99.9 | 17.3 | 0.04% / 52.9% | 0.23% / 77.3% | 0.14% / 80.4% |

## Reading

- **Up to P95 there is no trade-off.** Raising τ̂ lowers violations and raises the transfer. Every rescued correction executes again and pays a charge, which outweighs the smaller charge on the others.
  - Transfer peaks around P95 (93–97% of the actual-gas-price transfer).
  - Beyond P95 the larger κ starts to cost transfer: about 1 point at P99 in ETH/WBTC, 3.5 points in ETH/USDC.
- **Violations shrink slowly in the tail.** Arbitrage clusters in blocks with high tips. At the opportunities, the tip above the base fee has a 99th percentile of 2.4 gwei in ETH/USDC and 5.0 gwei in ETH/WBTC. The training distribution's P99 is 1.85 gwei, and the validation months' own P99 is 2.1 gwei.
- **Tips are discrete.** They cluster at round values (1, 2, 3 gwei). The quantiles therefore jump: P95 and P97.5 are both 1.0 gwei, P99.25 and P99.5 are both 2.0 gwei.

## Recommendation

**Among the four requested quantiles: P99 (τ̂ = 1.85 gwei).** It has the lowest violations (1.1% / 1.7% / 2.2%) and keeps 89–96% of the actual-gas-price transfer. It does not meet the 1% target.

**To meet the target in every pool: τ̂ = 3 gwei (training P99.75).**
- Violations: 0.24% (ETH/USDC), 0.79% and 0.95% (ETH/WBTC).
- Transfer: 84.2% / 93.5% / 94.9% of the actual-gas-price transfer, against 89.3% / 95.5% / 96.4% at P99.
- Compared with today's τ̂ (P50): it cuts violations from about 29% to below 1%. It loses about 6 points of transfer in ETH/USDC and gains about 1 point in ETH/WBTC.

Two caveats:
- The margin in ETH/WBTC offset (0.95%) is thin.
- Because of the gwei clustering, a round 3 gwei is the operative choice rather than "P99.75".

**A κ priced at the actual gas price would avoid the trade-off entirely, but the hook can only see the swapper's own tx.gasprice.** An arbitrager controls that value, so using it would let the arbitrager inflate κ by bidding a higher tip. Not recommended without further analysis.

## Files

| File | Contents |
|---|---|
| `eQ3_summary_valid.csv` | Every setting × pool |
| `eQ3_opportunities_valid.csv.gz` | Per correction and setting, including the tip gap |
| `eQ3_checks_valid.csv` | Exact match with experiment B |
| `manifest_valid.json` | Commit, inputs and hashes, tip quantiles, recommendation |
