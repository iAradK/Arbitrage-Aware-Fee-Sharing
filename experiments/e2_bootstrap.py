#!/usr/bin/env python3
"""E2 v2: paired day-block bootstrap confidence intervals for the headline replay metrics (DECISIONS V8).

Reads results/e2/e2_daily_<tag>.parquet (per-day aggregates written by e2_sequential_replay.py) and resamples whole UTC
days with replacement; the same resampled days are used for every mechanism (paired). Ratios are recomputed from the
resampled sums: execution rate = sum n_exec / sum n_bf, violation rate = sum n_viol / sum n_bf, mean E_TW = sum err /
sum steps; protection and searcher payoff are totals over the resampled period. Also reports the paired difference in
protection between the retained margin and the maximal cap.

  python experiments/e2_bootstrap.py --tag test_lag0_med
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import reporting  # noqa: E402

PRIMARY = {"eth_usdc_005": "raw", "usdc_usdt_0001": "raw", "eth_wbtc_030": "corr24h", "eth_wsteth_001": "corr24h"}
METRICS = ("execution_rate", "violation_rate", "etw_mean", "protection_usd", "searcher_net_usd")


def metrics(s: pd.DataFrame) -> dict:
    return {"execution_rate": s["n_exec"] / np.maximum(s["n_bf"], 1), "violation_rate": s["n_viol_q0"] / np.maximum(s["n_bf"], 1),
            "etw_mean": s["sum_err"] / np.maximum(s["n_steps"], 1), "protection_usd": s["sum_r"], "searcher_net_usd": s["sum_pi"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--lam", type=float, default=0.75)
    ap.add_argument("--gamma", type=float, default=0.02)
    ap.add_argument("--B", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260924)
    a = ap.parse_args()
    out = reporting.out_dir("e2")
    d = pd.read_parquet(out / f"e2_daily_{a.tag}.parquet")
    d = d[(d["regime"] == "median") & d.apply(lambda r: PRIMARY[r["pool"]] == r["variant"], axis=1)]
    keep = (np.isclose(d["lam"], 0) | np.isclose(d["lam"], a.lam)) & (np.isclose(d["gamma"], 0) | np.isclose(d["gamma"], a.gamma))
    d = d[keep]
    rng = np.random.default_rng(a.seed)
    rows = []
    cols = ["n_steps", "sum_err", "n_bf", "n_exec", "n_viol_q0", "sum_r", "sum_pi"]
    for pool, g in d.groupby("pool"):
        days = np.sort(g["day"].unique())
        nd = len(days)
        idx = rng.integers(0, nd, size=(a.B, nd))                    # the same resampled days for every mechanism (paired)
        boot = {}
        for mech, h in g.groupby("mech"):
            m = h.set_index("day").reindex(days)[cols].fillna(0.0).to_numpy()   # (nd, len(cols))
            tot = m.sum(0)
            point = metrics(pd.Series(tot, index=cols))
            bs = m[idx].sum(1)                                          # (B, len(cols))
            bm = metrics(pd.DataFrame(bs, columns=cols))
            boot[mech] = bm
            for k in METRICS:
                lo, hi = np.quantile(np.asarray(bm[k]), [0.025, 0.975])
                rows.append({"pool": pool, "variant": PRIMARY[pool], "mech": mech, "metric": k, "point": float(point[k]),
                             "ci_lo": float(lo), "ci_hi": float(hi), "n_days": nd})
        if "retained" in boot and "cap_gamma0" in boot:
            diff = np.asarray(boot["retained"]["protection_usd"]) - np.asarray(boot["cap_gamma0"]["protection_usd"])
            pr = g[g["mech"] == "retained"]["sum_r"].sum() - g[g["mech"] == "cap_gamma0"]["sum_r"].sum()
            lo, hi = np.quantile(diff, [0.025, 0.975])
            rows.append({"pool": pool, "variant": PRIMARY[pool], "mech": "retained - cap_gamma0", "metric": "protection_usd",
                         "point": float(pr), "ci_lo": float(lo), "ci_hi": float(hi), "n_days": nd})
    res = pd.DataFrame(rows)
    reporting.write_table(res, out / "tables" / f"e2_bootstrap_{a.tag}", {"point": "{:.4g}", "ci_lo": "{:.4g}", "ci_hi": "{:.4g}"})
    with pd.option_context("display.width", 250, "display.max_rows", 400):
        print(res[res["metric"].isin(["execution_rate", "protection_usd", "etw_mean"])].round(6).to_string(index=False))


if __name__ == "__main__":
    main()
