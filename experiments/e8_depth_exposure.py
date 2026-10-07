#!/usr/bin/env python3
"""E8 robustness check: how much of the replay's recovered funds comes from minutes whose depth overstates the liquidity.

The replay applies the E1 trailing depth L_t (median over the preceding hour) to the whole correction. When a large
position sits in a narrow range, the measured in-range liquidity can jump by orders of magnitude for a few swaps
(ETH/USDC, 2025-07-09: 3.26e9 against a median of 6.4e6), and the trailing median carries the jump to minutes in which
the swaps met far less. The CPMM with that depth then produces surplus that a correction could not have earned.

Per minute, the measured in-range liquidity is the `liquidity` of the last Swap event strictly before t (BigQuery,
data/data/bq/swap_logs_decoded.parquet, converted to E1 units by sqrt(10^dec0 * 10^dec1)). The frozen E2 configurations
are re-simulated (ideal reference, k = 1, d = 0, median R; reproducing results/e2/e2_summary_<split>.parquet exactly), and
the recovered funds and the baseline's gross surplus are split by the ratio L_t / measured liquidity.

  python experiments/e8_depth_exposure.py --split test
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

import e2_sequential_replay as e2  # noqa: E402
from common import minutegrid as mg, reporting  # noqa: E402
from common.pools import POOLS  # noqa: E402

PRIMARY = {"eth_usdc_005": "raw", "eth_wbtc_030": "corr24h", "eth_wsteth_001": "corr24h"}
BQ = ROOT / "data" / "data" / "bq" / "swap_logs_decoded.parquet"
CUTS = [2.0, 5.0, 10.0]
MECHS = ["baseline", "static_0.05pct", "static_0.30pct", "unconstrained", "cap_gamma0", "retained"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["train", "valid", "test"], default="test")
    a = ap.parse_args()
    cfg = reporting.load_config(ROOT / "experiments" / "configs" / "e2.yml")
    C2 = e2.make_configs(cfg)
    keep = (C2.mech == "baseline") | C2.mech.str.startswith("static") | \
           (C2.mech.isin(["unconstrained", "cap_gamma0"]) & np.isclose(C2.lam, 0.75)) | \
           ((C2.mech == "retained") & np.isclose(C2.lam, 0.75) & np.isclose(C2.gamma, 0.02))
    C = C2[keep].reset_index(drop=True)
    bq = pd.read_parquet(BQ, columns=["pool", "timestamp", "liquidity_f"])
    summ_f = ROOT / "results" / "e2" / f"e2_summary_{a.split}.parquet"
    summ = pd.read_parquet(summ_f) if summ_f.exists() else None
    rows = []
    for key, var in PRIMARY.items():
        pool = POOLS[key]
        R = mg.r_regimes_usd(key, var, cfg["gas_units"], cfg["r_quantiles"])["median"]
        g = mg.build_grid(key, a.split, var, lags=(0,))
        b = bq[bq.pool == key].sort_values("timestamp")
        meas = mg.asof_before(pd.DatetimeIndex(b["timestamp"]), (b["liquidity_f"] / np.sqrt(10.0 ** pool.dec0 * 10.0 ** pool.dec1)).to_numpy(), g["t"])
        ratio = g["L"].to_numpy() / meas
        r = e2.simulate(g, pool, C, cfg, R, 0.0, 0, 1)
        t = r["table"].set_index("mech")
        if summ is not None and a.split != "train":
            s = summ[(summ.pool == key) & (summ.variant == var) & (summ.regime == "median")]
            for m in MECHS:
                x = s[s.mech == m]
                x = x[np.isclose(x.lam, 0.75) & np.isclose(x.gamma, 0.02)] if m == "retained" else (x[np.isclose(x.lam, 0.75)] if m in ("unconstrained", "cap_gamma0") else x)
                assert np.isclose(x.protection_usd.iloc[0], t.loc[m, "protection_usd"], rtol=1e-12), (key, m)
        sp = r["step_protection"].astype(float)
        for m in MECHS:
            j = int(np.flatnonzero(C.mech.to_numpy() == m)[0])
            tot = sp[:, j].sum()
            row = {"pool": key, "variant": var, "split": a.split, "mech": m, "recovered_usd": float(t.loc[m, "protection_usd"]),
                   "minutes": int(np.isfinite(ratio).sum())}
            for c in CUTS:
                hi = ratio > c
                row[f"minutes_ratio_gt{c:g}"] = int(hi.sum())
                row[f"recovered_share_ratio_gt{c:g}"] = float(sp[hi, j].sum() / tot) if tot > 0 else np.nan
            rows.append(row)
        print(f"{key}: L/measured q50 {np.nanquantile(ratio, .5):.3f} q99 {np.nanquantile(ratio, .99):.2f} max {np.nanmax(ratio):.1f}", flush=True)
    res = pd.DataFrame(rows)
    out = reporting.out_dir("e8")
    reporting.write_table(res, out / "tables" / f"e8_depth_exposure_{a.split}",
                          {"recovered_usd": "{:,.0f}", **{f"recovered_share_ratio_gt{c:g}": "{:.4f}" for c in CUTS}})
    reporting.write_manifest("e8", {"analysis": "depth exposure", "cuts": CUTS, "e2_hash": reporting.config_hash(cfg)},
                             [BQ, ROOT / "results" / "e1" / "depth_eth_usdc_005.parquet"], a.split, tag=f"depth_exposure_{a.split}", latest=False)
    with pd.option_context("display.width", 250):
        print(res.round(4).to_string(index=False))


if __name__ == "__main__":
    main()
