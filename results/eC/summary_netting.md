# Experiment C, Q2: per-transaction and per-swap clipping (validation months)

Script: `experiments/eC_netting_variants.py`. Provenance: `manifest_netting_valid.json` (git a19edc7+dirty; the dirty files are the same mirror of the main checkout as in Experiment C). Post-processing of `eC_swaps_valid.csv.gz`. The test months are refused.

Outputs:
- `eC_netting_variants_valid.csv`: the metrics.
- `eC_netting_both_directions_valid.csv`: transactions that swap in both directions.
- `eC_netting_split_check_valid.csv`: the within-block split check.
- `eC_netting_swaps_valid.csv.gz`: per-swap charges under every accumulation.

## Variants

Implemented in `common/fixedpoint.ScopedHookReference(accumulation=...)` and tested in `tests/test_accumulation_variants.py`. The watermark, settlement and κ are unchanged.

| Name | Accumulator A of the block |
|---|---|
| tx | transaction scope: [π·Δ of the transaction]^+ |
| block net | the deployed hook: [π·Δ of the whole block]^+ |
| **V1** `tx_clip` | Σ over completed transactions of [π·Δ_tx]^+, plus [π·Δ of the current transaction]^+ |
| **V2** `swap_clip` | Σ over swaps of [π·Δ_swap]^+ |
| standalone | every swap its own scope |

- **Reproduction.** The tx, block-net and standalone charges, and block net's c1 and c2, reproduce `eC_swaps_valid.csv.gz` exactly (asserted per swap).
- **Ordering.** net ≤ V1 ≤ V2 holds swap by swap, both in A and in W (property test).

## Metrics

The metrics are kept separate:
- **(a)** tipped violations: standalone margin ≥ 0 and charge > margin.
- **(b)** spillover: charged although the standalone charge is 0.
- **(c1)** reversal leak below the watermark.
- **(c2)** netting shortfall against the standalone charge.
- **(d)** charges paid by non-arbitrage swaps.

"Excl." drops block 24915388 (the 294 ETH event).

**ETH/USDC, δ=0.** 1,038 swaps are feasible standalone (1,037 excl.).

| Accumulation | Transfer USD (excl.) | (a) tipped | (b) spillover: n, % of charged, USD | (c1) USD | (c2) shortfall USD, % of standalone (excl.: USD, %) | (d) non-arb: n, USD |
|---|---|---|---|---|---|---|
| tx | 50,515 (16,877) | 567 | 33, 1.5%, 17 | 0 | 48, 0.09% (48, 0.28%) | 756, 3,171 |
| block net | 8,770 (8,770) | 356 | 364, 18.4%, 145 | 20.5 | **42,001, 83.1% (8,363, 49.6%)** | 886, 3,120 |
| **V1** | 50,879 (17,241) | 581 | 520, 19.5%, 209 | 1.0 | 48, 0.09% (48, 0.28%) | 953, 3,274 |
| **V2** | 50,932 (17,294) | 582 | 525, 19.6%, 210 | 0 | 0 | 953, 3,274 |
| standalone | 50,515 (16,877) | 567 | 0 | 0 | 0 | 747, 3,160 |

**ETH/USDC, buffered δ=ε_S:**

| Accumulation | Transfer USD (excl.) | (a) tipped | (b) spillover: n, % of charged, USD | (c1) USD | (c2) shortfall USD, % of standalone (excl.) | (d) non-arb: n, USD |
|---|---|---|---|---|---|---|
| tx | 42,344 (8,706) | 43 | 16, 7.8%, 68 | 0 | 1.5, 0.00% | 40, 772 |
| block net | 3,112 (3,112) | 49 | 56, 32.9%, 240 | 11.9 | **39,502, 93.5% (5,864, 68.1%)** | 56, 873 |
| **V1** | 43,193 (9,556) | **108** | **143, 43.2%, 696** | 0 | 1.5, 0.00% | 59, 899 |
| **V2** | 43,222 (9,584) | **111** | **148, 43.8%, 703** | 0 | 0 | 59, 899 |
| standalone | 42,250 (8,612) | 36 | 0 | 0 | 0 | 39, 766 |

Excluding the event block changes only transfer and (c2). Every other count moves by at most one swap; the full rows are in the CSV.

**ETH/WBTC corr24h** (E8 primary; 835 swaps feasible standalone; the event is not in this pool):

| Setting | Accumulation | Transfer USD | (a) | (b) | (c1) | (c2) shortfall | (d) |
|---|---|---|---|---|---|---|---|
| δ=0 | tx / block net / V1 / V2 | 7,531 / 6,483 / 7,563 / 7,563 | 120 / 123 / 123 / 123 | 0 / 12 / 12 / 12 | 0 | 0 / 1,080 / 0 / 0 | 3 / 4 / 4 / 4 swaps |
| δ=ε_S | tx / block net / V1 / V2 | 2,311 / 1,231 / 2,311 / 2,311 | 3 / 3 / 3 / 3 | 0 | 0 | 0 / 1,080 / 0 / 0 | 0 |

ETH/WBTC raw is analogous. With δ=0: transfer 14,924 / 13,750 / 15,024 / 15,024; (a) 234 / 244 / 244 / 244; (b) 0 / 24 / 24 / 24; (c2) 0 / 1,274 / 0 / 0.

## Transactions that swap in both directions (`eC_netting_both_directions_valid.csv`)

These are transactions with 2+ swaps whose base-token deltas have opposite signs: 35 in ETH/USDC (82 swaps), none in ETH/WBTC.

| Setting | V2 charge vs tx-scope charge on these transactions | Transactions over-charged by V2 | V2 excess USD (max) | V1 excess |
|---|---|---|---|---|
| δ=0 | 62.73 vs 14.11 USD | 3 | 48.61 (21.25) | 0 |
| δ=ε_S | 7.05 vs 0 USD | 2 | 7.05 (6.99) | 0 |

V1 charges these transactions exactly their transaction-scope charge. V2 charges each positive leg of a round trip, which is the 4.4× over-charge in the table.

## Experiment A within-block split check (`eC_netting_split_check_valid.csv`)

The setup is the same as Follow-up 2:
- every E2 correction, for ideal and buffered, ETH/USDC and ETH/WBTC raw / offset;
- split into l = 2..block capacity transactions of one block, in two families;
- 4,139,001 splits and 669,617,937 pieces per variant.

**Result.** Survival is **100%** for V1 and for V2, both sum-weighted and as the mean of per-correction ratios. The deviation is 0 WAD units (min and max) on every split. The pieces are numeraire deltas, so per-swap truncation of the base-token product does not enter. On real swaps, V1 or V2 can lose at most one numeraire base unit per extra transaction (V1) or swap (V2) to that truncation.

## Reading

- **What clipping fixes.** Clipping removes netting. V1 and V2 collect what the transaction scope collects, plus the block's spillover: 50,879 / 50,932 against 50,515 USD (δ=0), and 43,193 / 43,222 against 42,344 USD (δ=ε_S). The net block scope collects 8,770 and 3,112 USD.
- **What it costs.** It exposes the block scope's spillover. κ, including the buffer δ, is deducted once per block, so a later transaction in the same block pays λ or (1−γ) of its surplus with no κ of its own (Proposition 3: at most (1−γ)κ per swap). Netting had hidden part of this.
  - With δ=0 the extra tipped violations are few: 581 / 582 against 567 under the transaction scope.
  - With the buffer they more than double: 108 / 111 against 43, with 696 / 703 USD of spillover charges against 68 USD. The buffer protects only the block's first charged transaction.
- **V1 vs V2.** They differ only on transactions that swap in both directions. There V1 equals the transaction scope and V2 over-charges (48.6 USD on 3 transactions with δ=0). V1 is the variant without that defect.
- **Correction to the Experiment C summary.** Without the 294 ETH block, netting costs 49.6% (δ=0) and 68.1% (δ=ε_S) of the standalone transfer, not 16.6% / 13.9%. The earlier figures divided by the standalone transfer that still contained the event. `summary.md` is corrected.
