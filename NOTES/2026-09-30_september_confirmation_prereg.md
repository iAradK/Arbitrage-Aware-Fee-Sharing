# September 2026 confirmation month: pre-registration

Written 2026-09-30 11:30 UTC, before any September 2026 swap, gas, Lido or additional Binance data was downloaded
and before September 30 closed in UTC. Plan item 2.3, decided by the user on 2026-09-30.

**Wording for the paper:** "a confirmation month not analysed during development". It is not a sealed holdout,
since the data was not sealed in advance (a sealed holdout stays a Phase 3 item, `CHANGELOG_revision.md`).

## 1. Frozen state

- Commit: `3bf2c5feb7255fc0954c820c8701d9ffe163df6b` on branch `update_pyth_exp` ("Freeze analysis state before the
  September 2026 confirmation month"). It contains all analysis code, configs, tests and the decision log.
- Frozen configurations (SHA-256 of the canonical config, `results/<exp>/frozen_config.sha256`; each verified equal
  to the committed YAML before the commit):

  | Experiment | Config hash |
  |---|---|
  | E1 depth calibration | `139d6b0395d86f3394cf7bab5af9842bf6d31b902980bf8a61d030defef9e0c1` |
  | E2 sequential replay | `1e8527ba2d0ce274f185fb692406a39b7a4c8414ad954c9f7b57871322b9cf9f` |
  | E4 estimation error | `6026852cf0059f5a21d89896cfd8fc96907946ce2029bfd146e289bfb5a890a7` |
  | E6 fragmentation | `5a8fd8cc3829a36377d2c992714ec64ffab3bbb53a0092f52f423afbc18d61b0` |
  | E7 S_min share | `70558f065624efd6e1aa406a71428e6306f48cc594c56904202e050ba45febd6` |

- Baseline decision 1.1(b) is final: the baseline stays the zero-transfer hook that pays the hook's gas, and the
  hook-free AMM is reported alongside it. No config changes before the September run.
- Every parameter comes from earlier months and is not refitted: the reservation regimes R (training months),
  lambda = 0.75, gamma = 0.02 (replay) and 0.05 (E4, E6, E7), tau_hat = 0.02 gwei (training median tip), the hook
  gas (30,032 and 13,704), the one-hour trailing depth window, the 24-hour basis offset, the fixed buffer fitted on
  the validation months, and the rolling 7-day buffer seeded with the preceding split (the test months).
- Pools: ETH/USDC, ETH/WBTC (24-hour offset) and ETH/wstETH (24-hour offset). USDC/USDT stays excluded (W8).
  Pyth is excluded, since its on-chain export ends on 2026-08-17.

## 2. State of the September data today

Binance one-minute data for 2026-09-01 to 2026-09-22 has been on disk since 2026-09-24 (commit `477bc3a`). It was
downloaded but never analysed. Its only use was the Phase 0 data-quality counts on whole files (row counts,
zero-volume minutes, close range), which carry no information about pool deviations or mechanism outcomes. No
September swaps, gas records or Lido events exist on disk (`NOTES/2026-09-29_september_untouched.md`).

## 3. Scope (one run each, after the month has closed)

| Run | Settings | Main text claim it checks |
|---|---|---|
| E1 | trailing depth over September, frozen window | calibration used by every run |
| E2 ideal replay | k = 1, d = 0, all three R regimes, plus the hook-free AMM (`e2_hook_gas.py`) | RQ1, RQ2, hook gas |
| E2 delayed replay | k in {1, 5, 15}, d in {0, 1, 5}, median R, rolling buffer | buffered recovery vs delay |
| E4 delay sweep | d in {0, 1, 5, 10, 30, 60}, k in {1, 5, 15}, eta grid, no Pyth | RQ3 |
| E6 fragmentation | n in {2, 4, 8, 16}, reversal paths, plus the cross-transaction statistic | RQ4 |
| E7 | S_min share of the September opportunities | Section 5.5 |

E3 (frontier) is derived from the E2 outputs. E5 uses training months only and is not rerun.

## 4. Metrics reported (all of them, whatever they show)

Per pool: execution rate of the baseline-feasible corrections; participation and cap violation rates; mean
time-weighted price error E_TW relative to the baseline; recovered funds (USD, 95% paired day-block intervals);
recapture rate; per-step LP excess over HODL; share of recovered funds in the baseline's ex-post margin; for the
delayed runs, violations and recovered funds relative to the ideal reference at the same k; for E4, violations and
effective recapture per (k, d) with and without the buffer; for E6, exactness failures, independent-rule ratios and
the share of corrections for which a two-transaction split pays off; the hook-free share X of corrections removed by
the hook's gas and the price-error ratio; the S_min share of opportunities. September has 30 days, so the intervals
are wider than on the 62 test days.

## 5. Confirmation criteria (proposed on 2026-09-30, to be approved by the user before any download)

A claim counts as confirmed in September if its criterion holds. Each criterion is reported with its outcome, and
a failed or weaker result is reported as it is.

| # | Claim (test-month result) | Criterion in September |
|---|---|---|
| C1 | Participation-aware rules execute every baseline-feasible correction without violations (exact reference) | Maximal cap and retained margin: execution rate 100%, participation and cap violations 0, every pool, every R regime |
| C2 | Retained margin keeps the baseline price error (ratio 1.000) | E_TW ratio to the baseline within [0.99, 1.01] in every pool |
| C3 | Unconstrained sharing and the static 0.30% charge suppress corrections and raise the error (ETH/USDC 26.3% and 4.3% executed, ratio 1.30 and 2.74) | ETH/USDC: execution rate below 100% and E_TW ratio above 1.05 for both |
| C4 | The retained margin recovers substantial funds (ETH/USDC 40,524 USD over 62 days) | ETH/USDC: lower end of the 95% interval above 0 |
| C5 | With a delayed reference the buffer keeps violations rare (at most 1.3%) and recovers 22-62% of the ideal funds when d < k, almost nothing when d >= k | ETH/USDC replay, delta = eps_S: violations at most 2% for every (k, d); recovery at least 15% of the ideal at the same k for every d < k, at most 10% for every d >= k |
| C6 | The rolling buffer reduces violations (E4, k = 1: 5.8% to 1.0% at 5 min) | ETH/USDC, every d >= 1 and k: violations with delta = eps_S below those without a buffer |
| C7 | The cumulative accounting is exact under fragmentation and reversals | 0 exactness failures, every pool and n |
| C8 | The hook's gas is not material (pre-registered rule: 95% interval of the E_TW ratio baseline / hook-free entirely above 1.01) | The rule does not fire in any pool |

## 6. Procedure

1. Wait until 2026-10-01 00:00 UTC. Download only data for 2026-09-01 to 2026-09-30 (UTC):
   swaps of the three pools (`data/fetch_swaps.py`, The Graph key, run by the user); base fees of every block
   (BigQuery `blocks` export, run by the user); tips (`data/download_fee_history.py --start 2026-09-01 --end
   2026-10-01`, RPC URL, run by the user); Lido rate events (`data/download_wsteth_rate.py --start 2026-09-01 --end
   2026-10-01`); Binance one-minute candles for 2026-09-23 to 2026-09-30 (`data/download_binance_klines.py`).
2. Check that the node provider returns tips for September. If it does not, apply the rule already used for August
   2026: base fee plus the trailing seven-day median tip frozen at the last block with real tips, flagged
   `base+frozen_tip`, and report the share of swaps priced with real tips.
3. Code changes that open a `confirm` split (September) must not change any existing output. They are committed
   before the download together with a test that rebuilds the test months and checks them bit for bit, and their
   commit hash is appended below.
4. Run every experiment of Section 3 once with `--split confirm --confirm-frozen`. A marker file per experiment
   refuses a second run.
5. Report: an appendix table with every metric of Section 4 and the outcome of each criterion of Section 5, plus
   one sentence in Section 5.2.

## Appendix: later commits

- 2026-09-30, amendment before any September download: E7 now covers the three evaluated pools (DECISIONS W11).
  Its frozen config hash changes from `70558f065624...` to
  `1074ff37d1e6df41ead22516cd5044922ac213d36025c09b304017db343da04b`. The S_min tables of the three pools are
  unchanged. The September E7 run uses this hash. Committed together with this amendment.
- Open before the download: the gas overhead of the current contract (`8ca840a`, with settlement) is 30,214 gas
  against the 30,032 that every frozen config uses. The configs stay at 30,032 unless the user decides otherwise
  before the download; any change will be recorded here first.
- 2026-09-30, decision before any download: the frozen configs keep the pre-settlement hook gas (30,032), so the
  September run is directly comparable with the test months. The paper reports the gas of the current contract
  separately (DECISIONS W12).
- (to be filled: commit of the `confirm` split code, approval of the criteria, download times)
