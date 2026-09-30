# Deferred to the revision (after the SIGMETRICS submission of 2026-10-09)

These items came up in the external review and the submission plan. They are deferred because
none of them fits into the submission window without risking the numbers that the paper
already reports. Each needs a new design or a new frozen run, not a text change.

| Item | Why it is deferred |
|---|---|
| Combined one-sided buffer rerun | The buffer is currently calibrated separately for the surplus error and the cost error. A single one-sided buffer calibrated on the combined error needs a new validation calibration and a new frozen test run of E2 and E4. |
| Settlement through `afterSwapReturnDelta` | The prototype records the charge but does not settle it (Section 5.5). Settling it changes the hook's gas, so the E7 gas numbers, the 30,032-gas overhead used in every replay and the conformance tests would all have to be redone. |
| Realistic-reference experiments | The replays use a benchmark reference delayed by d minutes and the July 2026 on-chain Pyth feed. A reference supplied by the swap transaction itself (future work in Section 6) needs a model of the incentives that such updates create. |
| Full measurement | The evaluation replays opportunities through a calibrated constant-product pool. Measuring the mechanism on real arbitrage transactions (their gas, builder payments and hedging legs) needs mempool or builder data that we do not have. |
| Block scoping of the accumulator | The accumulator is scoped to one transaction. Proposition `prop:cross-tx` bounds what splitting across transactions saves, but at the median gas price an extra transaction costs about 185k gas (0.07 USD), far less than the bound (0.47 to 5.0 USD per pool at gamma = 0.05). A block-scoped accumulator would close this gap but needs a new contract design and new conformance and gas tests. |
| A sealed holdout | The test months were run twice (v1 and v2, see `results/DECISIONS.md`), and several analyses were added after the second freeze (W2 to W7). A September 2026 confirmation run (plan item 2.3) or a later sealed month would restore a clean holdout. `NOTES/2026-09-29_september_untouched.md` records that September 2026 has not been analysed. |
| Dynamic-fee baseline (plan item 2.5) | A dynamic-fee AMM (a fee that rises with volatility or with the deviation from the reference) is the natural competitor that reviewers may ask for. A fair comparison needs a fee rule chosen and tuned on the validation months, its own replay configuration and a new frozen test run, and a half-finished version would be worse than none. Skipped for the submission (decision 2026-09-30) and kept ready here for the rebuttal or the revision. |
| Further robustness work | Other pools and chains, other gas assumptions for the arbitrage transaction, a model of competing arbitragers and a buffer that adapts to volatility. |
