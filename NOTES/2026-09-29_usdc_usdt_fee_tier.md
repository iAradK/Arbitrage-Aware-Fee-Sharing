# USDC/USDT pool fee is 0.001%, not the 0.01% used in the model (found 2026-09-29)

## Finding

`common/pools.py` sets the LP fee of `usdc_usdt_0001` (pool id `0x8aa4e11c...8e47`) to `1e-4` (0.01%),
and the paper labels it "USDC/USDT 0.01%" (Tables `tab:eval-data` and the E1 table). Two independent
sources say the fee is 0.001% (`1e-5`):

1. The Uniswap v4 subgraph reports `feeTier = 10` for the pool, in v4 fee units of 1e-6
   (`data/data/subgraph/pool_day_data_jul_aug_2026.json`, fetched 2026-09-29). The same field gives 500,
   3000 and 100 for the other three pools, which match the model.
2. The pool's own swaps. For a swap inside one tick range, the depths implied by the token-0 and token-1
   legs agree only at the true fee. On 20,000 test-month swaps the implied fee is 1.000e-5 (IQR 1.000e-5
   to 1.001e-5). The same computation gives 5.000e-4 for ETH/USDC and 1.000e-4 for ETH/wstETH, matching
   their fee tiers (script `infer_fee.py` in the session scratchpad).

The pool key `usdc_usdt_0001` itself follows the naming of `eth_usdc_005` (0.05%) and
`eth_wbtc_030` (0.30%), so it reads as 0.001%.

## What it affects

Every USDC/USDT number in E1 to E7: the CPMM fee in the replays, the opportunity threshold
(`gap_multiple` 2 x fee), depth inference (negligible, 9e-5 relative), the swap-derived fees, the
R calibration, the fragmentation and S_min shares, the hook-gas share X (6.0%) and the recovered
funds (320.5 USD). App. B.1 justifies the VWAP reference and the 2 x fee threshold with "the minute
close resolves only about one basis point, which equals the pool fee". With the true fee of 0.1 bp
the reference resolves about ten times the fee.

The subgraph TVL of this pool is negative (mean -23.7 M USD), so no TVL is available for it from
this source. The subgraph fees of the pool (8,451 USD over Jul-Aug 2026) equal volume x 0.001%.

## Status

Reported to the user. No result has been changed.

**User decision (2026-09-29): drop USDC/USDT from the evaluation.** The paper evaluates three pools
(ETH/USDC, ETH/WBTC, ETH/wstETH). App. B.1 states that the pool was excluded after its fee tier was found to be
0.001%, which the one-minute Binance reference cannot resolve. No experiment is rerun and no other pool's number
changes (every experiment treats pools independently). The USDC/USDT outputs stay in `results/` for the record.
