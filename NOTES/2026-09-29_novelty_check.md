# Novelty check for the intro claim (plan item 1.11), 2026-09-29

Claim under test (intro, after Stream B): "no prior AMM mechanism bounds a per-swap transfer by
this margin" (the arbitrager's participation margin).

## Closest work found (all verified at the source)

1. **MEV taxes.** Dan Robinson and Dave White, "Priority Is All You Need", Paradigm, June 2024,
   https://www.paradigm.xyz/2024/06/priority-is-all-you-need . An application charges a fee that
   is an increasing function of the transaction's priority fee (e.g. 99 times it). Under
   competitive priority ordering (OP Stack chains), the winning searcher bids so that its total
   payment just equals the opportunity, so the application captures almost all of the searcher's
   profit. The post names AMMs and LVR reduction as an application and sketches a modified
   constant-product check. The payment is therefore bounded by the searcher's profit, but through
   competition, and it requires priority ordering by the block proposer. It does not bound a
   per-swap transfer by an explicit margin, and it does not leave a retained margin.
2. **Diamond.** Conor McMenamin, Vanesa Daza and Bruno Mazorra, "An Automated Market Maker
   Minimizing Loss-Versus-Rebalancing", Mathematical Research for Blockchain Economy (MARBLE 2023),
   Lecture Notes in Operations Research, Springer, 2023, pp. 95-114,
   doi:10.1007/978-3-031-48731-6_6 (arXiv:2210.10601; earlier ePrint 2022/1420 "Diamonds are
   Forever, Loss-Versus-Rebalancing is Not"). Block producers auction the right to the first
   arbitrage, and part of the correction is retained for the pool (an LVR rebate parameter). The
   payment is set by the auction, not by a per-swap formula.
3. Already cited in the paper: RediSwap (auction and redistribution of application MEV), am-AMM
   (auction of pool-management rights), FM-AMM / CoW AMM (batch execution). Also seen but
   industry-only (no paper): Angstrom (Sorella), Arrakis "Diamond" LVR hook, Balancer v3 MEV-tax
   hooks on Base, MEV-capturing AMM (McAMM, ethresear.ch 2022). Also seen: Fritsch et al., "MEV
   Capture Through Time-Advantaged Arbitrage" (arXiv:2410.10797), which auctions a time advantage.

## Assessment

No work found bounds a per-swap transfer by an estimated participation margin with a formula
applied by the pool. However, auction- and tax-based designs already extract an amount that is
bounded by the arbitrager's margin, because competition drives the payment up to it. The current
claim is literally true but can read as broader than it is. The distinguishing features of our rule
are (i) the bound is enforced by the rule itself, per swap, without an auction, priority ordering
or competition among arbitragers, and (ii) it leaves a retained margin gamma so the optimal
correction stays strictly preferred.

## Proposed edits (apply once the Overleaf version is in the repo; wrap in \extended)

- Intro, keep the claim but qualify it: "Auction-based designs let competing arbitragers bid away
  their margin~\cite{robinson2024priority,mcmenamin2023diamond,rediswap,adams2024amamm}. To the
  best of our knowledge, no prior AMM mechanism bounds a per-swap transfer by this margin without
  such an auction."
- Section 2.3, one sentence after the RediSwap / am-AMM / FM-AMM sentences: "MEV taxes charge a
  multiple of the priority fee and, under competitive priority ordering, capture almost all of the
  winning searcher's profit~\cite{robinson2024priority}. Diamond auctions the first arbitrage in
  each block and retains part of the correction for the pool~\cite{mcmenamin2023diamond}. In these
  designs competition sets the payment, whereas our hook bounds it by a rule and needs no auction
  or ordering rule."

## Bib entries to add (verified: Paradigm page and Crossref record)

```bibtex
@misc{robinson2024priority,
  author       = {Robinson, Dan and White, Dave},
  title        = {Priority Is All You Need},
  howpublished = {Paradigm research post},
  year         = {2024},
  month        = jun,
  url          = {https://www.paradigm.xyz/2024/06/priority-is-all-you-need}
}

@inproceedings{mcmenamin2023diamond,
  author    = {McMenamin, Conor and Daza, Vanesa and Mazorra, Bruno},
  title     = {An Automated Market Maker Minimizing Loss-Versus-Rebalancing},
  booktitle = {Mathematical Research for Blockchain Economy (MARBLE 2023)},
  series    = {Lecture Notes in Operations Research},
  publisher = {Springer},
  address   = {Cham},
  pages     = {95--114},
  year      = {2023},
  doi       = {10.1007/978-3-031-48731-6_6}
}
```
