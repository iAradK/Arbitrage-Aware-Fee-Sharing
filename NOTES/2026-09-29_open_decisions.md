# Open decisions of the SIGMETRICS handoff, answered by the user on 2026-09-29

1. **Pyth August gap.** Keep the July 2026 truncation (E4 frozen `6026852cf005`). No change to
   the text or the run.
2. **Placement of the 1.4 numeric paragraph** (cost of splitting across transactions). Section
   5.4.2, next to the fragmentation results, at gamma = 0.05. Reuse the Fig. 9a
   "independent per-swap rule" numbers, so no new runs.
3. **1.1(b) baseline switch.** Keep the zero-transfer-hook baseline and the pre-registered
   price-error rule. Disclose the per-pool share X of corrections lost to the hook gas for all
   four pools (test and validation), not only ETH/USDC. No rerun.
   Numbers shown to the user (`results/e2/tables/e2_hook_gas_{test,valid}_lag0_med.csv`):
   test X = 1.2% ETH/USDC (CI 1.0-1.4), 6.0% USDC/USDT (3.2-9.0), 0.7% ETH/WBTC (0.1-1.4),
   21% ETH/wstETH (7-37, 6 of 28); validation 2.2%, 3.7%, 0.8%, 29%.
4. **LP metric name (1.5).** "per-step LP excess over HODL" (keeps the current sign: negative
   means the LP underperforms HODL).
5. **Marking of new text.** Wrap new or changed text in `\extended{...}`. `\extended` must
   render black at submission.
6. **Section 5.5 threshold vs the new Eq. (9).** Recompute `e7 --smin` with a constant tip
   tau_hat equal to the training-median tip, so that Section 5.5 evaluates Eq. (9) exactly.
   Finding behind the question: the current code uses each block's own base fee plus that
   block's own median tip from `eth_feeHistory` (a tip frozen at the July 7-day median for
   August, where tips are missing), not the preceding block's. The replay (Section 5.1) does
   use the last block strictly before each minute.
