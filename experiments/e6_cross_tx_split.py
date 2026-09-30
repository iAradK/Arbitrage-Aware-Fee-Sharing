#!/usr/bin/env python3
"""E6 post-processing: what an arbitrager saves by splitting a correction across two transactions (Proposition
prop:cross-tx, Section 5.4.2). No new simulation: the sample is the frozen E6 fragmentation sample of ETH/USDC
(raw reference, test months, baseline-feasible swaps with a depth estimate).

Per swap, a is the estimated surplus of the correction (delta = 0 and an exact reference, as in E6) and
c = K_hat_tx + delta, with K_hat_tx from Eq. (9): 180,032 gas at the block's base fee plus a constant tip tau_hat
(the training-median tip, as in e7_solidity_conformance.smin) plus R. With F(x) = min(lam x, (1-gamma)[x-c]^+), the
best two-transaction saving is s = max over a1 in [0, a] of F(a) - F(a1) - F(a - a1). F is piecewise linear with
breakpoints c and x* = (1-gamma) c / (1-gamma-lam), so the maximum is attained at a1 in {0, c, x*, a-c, a-x*, a/2};
a 2,001-point grid checks this. The extra transaction costs 21,000 + 133,966 + 30,032 gas (intrinsic gas, a cold
swap and the hook, results/e7/hook_gas_isolated.csv) at the block's actual gas price.

  python experiments/e6_cross_tx_split.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

import e6_fragmentation as e6  # noqa: E402
from common import minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, RESULTS  # noqa: E402

POOL, VARIANT, SPLIT = "eth_usdc_005", "raw", "test"
INTRINSIC_GAS = 21_000


def F(x, c, lam, gam):
    return np.minimum(lam * x, (1 - gam) * np.maximum(x - c, 0.0))


def best_two_tx_saving(a, c, lam, gam):
    """Exact max over a1 in [0, a] of F(a) - F(a1) - F(a - a1), evaluated at the breakpoints."""
    xs = (1 - gam) * c / (1 - gam - lam)
    cands = np.stack([np.zeros_like(a), c, xs, a - c, a - xs, a / 2], axis=1)
    cands = np.clip(cands, 0.0, a[:, None])
    val = F(a[:, None], c[:, None], lam, gam) - F(cands, c[:, None], lam, gam) - F(a[:, None] - cands, c[:, None], lam, gam)
    return val.max(axis=1)


def main():
    cfg = reporting.load_config(ROOT / "experiments" / "configs" / "e6.yml")
    lam, gam, delta = cfg["lam"], cfg["gamma"], cfg["delta_usd"]
    R_usd = mg.r_regimes_usd(POOL, VARIANT, cfg["gas_units"], cfg["r_quantiles"])[cfg["r_regime"]]
    c_all, _ = e6.all_candidates(POOL, VARIANT, SPLIT, cfg, R_usd)
    sam = c_all[(c_all["n_used"] > 0) & (c_all["S"] - c_all["C"] >= c_all["R"])].reset_index(drop=True)
    assert len(sam) == 1177, len(sam)                                   # the sample reported in Section 5.4.2

    blocks = pd.read_parquet(CACHE / "block_gas.parquet", columns=["timestamp", "tip_p50_wei", "gas_source"])
    train = (pd.DatetimeIndex(blocks["timestamp"]) < mg.VALID_START) & (blocks["gas_source"] == "fee_history").to_numpy()
    tau_hat = float(blocks.loc[train, "tip_p50_wei"].median())
    gas_tx = cfg["gas_units"] + cfg["hook_overhead_gas"]                 # 180,032
    iso = pd.read_csv(RESULTS / "e7" / "hook_gas_isolated.csv")
    swap_gas = float(iso[(iso.n == 1) & (iso.hook == 0)].gas.iloc[0])
    hook_gas = float(iso[(iso.n == 1) & (iso.hook == 1)].gas.iloc[0]) - swap_gas
    extra_gas = INTRINSIC_GAS + swap_gas + hook_gas

    eth = sam["eth_usd"].to_numpy()
    a = sam["S_usd"].to_numpy()
    c = gas_tx * (sam["base_fee_wei"].to_numpy() + tau_hat) * 1e-18 * eth + R_usd + delta
    s = best_two_tx_saving(a, c, lam, gam)
    grid = np.linspace(0.0, 1.0, 2001)[None, :] * a[:, None]           # brute-force check of the breakpoint maximum
    s_grid = (F(a[:, None], c[:, None], lam, gam) - F(grid, c[:, None], lam, gam) - F(a[:, None] - grid, c[:, None], lam, gam)).max(1)
    assert np.all(s_grid <= s + 1e-9), "grid found a larger saving than the breakpoints"
    assert np.all(s <= (1 - gam) * c + 1e-9), "saving exceeds the upper bound of Proposition prop:cross-tx"
    big = a >= 2 * c
    assert np.all(s[big] >= min(lam, 1 - gam) * c[big] - 1e-9), "saving below the lower bound for a >= 2c"

    cost = extra_gas * sam["gas_price_wei"].to_numpy() * 1e-18 * eth
    g7 = pd.read_csv(RESULTS / "e7" / "tables" / "e7_smin_gas_conditions.csv")
    cost_med = float(g7[(g7.pool == POOL) & (g7.gas_quantile == "P50")].C_gas_actual_usd.iloc[0]) * extra_gas / gas_tx
    net, net_fixed = s - cost, s - cost_med
    q = lambda x, p: float(np.quantile(x, p))  # noqa: E731
    row = {"pool": POOL, "variant": VARIANT, "split": SPLIT, "n": len(sam), "lam": lam, "gamma": gam, "delta_usd": delta,
           "R_usd": R_usd, "tau_hat_wei": tau_hat, "extra_tx_gas": extra_gas,
           "c_median_usd": q(c, 0.5), "a_median_usd": q(a, 0.5), "share_a_ge_2c": float(big.mean()), "n_a_ge_2c": int(big.sum()),
           "saving_median_usd": q(s, 0.5), "saving_p95_usd": q(s, 0.95),
           "extra_tx_cost_median_usd": q(cost, 0.5), "extra_tx_cost_fixed_usd": cost_med,
           "net_median_usd": q(net, 0.5), "net_p95_usd": q(net, 0.95), "share_net_positive": float((net > 0).mean()),
           "net_fixed_median_usd": q(net_fixed, 0.5), "net_fixed_p95_usd": q(net_fixed, 0.95),
           "net_median_a_ge_2c_usd": q(net[big], 0.5) if big.any() else np.nan}
    res = pd.DataFrame([row])
    out = reporting.out_dir("e6")
    reporting.write_table(res, out / "tables" / "e6_cross_tx_split_test", {k: "{:.4f}" for k in row if k.endswith("usd") or k.startswith("share")})
    reporting.write_manifest("e6", cfg, [CACHE / "aligned" / f"{POOL}.parquet", CACHE / "block_gas.parquet"], SPLIT,
                             {"analysis": "cross-transaction split, post-processing of the frozen E6 sample"},
                             tag="cross_tx_split_test", latest=False)
    with pd.option_context("display.width", 200):
        print(res.T.to_string())


if __name__ == "__main__":
    main()
