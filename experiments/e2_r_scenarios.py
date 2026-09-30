#!/usr/bin/env python3
"""E2: replay at the low, median and high reservation payoff R (external review 1.1(c)). No new replay.

Reads the frozen v2 test run (tag `test`: k = 1, d = 0, all three R regimes; provenance in results/e2/manifest_test.json)
and the paired day-block bootstrap of each regime (e2_bootstrap.py --tag test --regime low|median|high). Changing R changes
which corrections are feasible, so every quantity is taken against the baseline at the same R: the execution rate is the
share of the rule's own baseline-feasible corrections that it executes, and E_TW is divided by the baseline's E_TW at that R.

  python experiments/e2_r_scenarios.py --tag test
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

from common import reporting  # noqa: E402
from e2_bootstrap import PRIMARY  # noqa: E402

CFG = ROOT / "experiments" / "configs" / "e2.yml"
REGIMES = ["low", "median", "high"]
LABEL = {"baseline": "Baseline AMM", "static_0.05pct": "Static 0.05%", "static_0.30pct": "Static 0.30%",
         "unconstrained": "Unconstrained", "cap_gamma0": "Maximal cap", "retained": "Retained margin",
         "buffered_1eps": "Buffered (1 eps_S)", "buffered_2eps": "Buffered (2 eps_S)"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="test")
    ap.add_argument("--pool", default="eth_usdc_005", help="pool of the LaTeX table (the CSV covers all pools)")
    a = ap.parse_args()
    cfg = reporting.load_config(CFG)
    lam, gam = cfg["headline"]["lambda"], cfg["headline"]["gamma"]
    out = reporting.out_dir("e2")
    summ = pd.read_parquet(out / f"e2_summary_{a.tag}.parquet")
    summ = summ[summ.apply(lambda r: PRIMARY[r["pool"]] == r["variant"], axis=1) & summ["mech"].isin(LABEL)
                & (np.isclose(summ["lam"], 0) | np.isclose(summ["lam"], lam)) & (np.isclose(summ["gamma"], 0) | np.isclose(summ["gamma"], gam))]
    boot = []
    for rg in REGIMES:
        b = pd.read_csv(out / "tables" / (f"e2_bootstrap_{a.tag}" + ("" if rg == "median" else f"_{rg}") + ".csv"))
        b = b[b["metric"] == "protection_usd"][["pool", "mech", "ci_lo", "ci_hi"]]
        b.insert(0, "regime", rg)
        boot.append(b)
    res = summ.merge(pd.concat(boot), on=["regime", "pool", "mech"], how="left")
    base = res[res["mech"] == "baseline"].set_index(["pool", "regime"])["etw_mean"]
    res["etw_ratio"] = res["etw_mean"].to_numpy() / base.reindex(pd.MultiIndex.from_frame(res[["pool", "regime"]])).to_numpy()
    res["regime"] = pd.Categorical(res["regime"], REGIMES, ordered=True)
    res["rule"] = pd.Categorical(res["mech"].map(LABEL), list(LABEL.values()), ordered=True)
    res = res.sort_values(["pool", "regime", "rule"])
    cols = ["pool", "variant", "regime", "R_usd", "rule", "n_baseline_feasible", "n_executed", "execution_rate", "etw_ratio",
            "protection_usd", "ci_lo", "ci_hi"]
    res = res[cols].rename(columns={"ci_lo": "protection_ci_lo", "ci_hi": "protection_ci_hi"})
    res.to_csv(out / "tables" / f"e2_r_scenarios_{a.tag}_all_pools.csv", index=False)
    t = res[res["pool"] == a.pool].copy()
    t["regime"] = t["regime"].astype(str)
    t["rule"] = t["rule"].astype(str)
    t["execution_rate"] *= 100
    t = t.drop(columns=["pool", "variant"]).rename(columns={"regime": "R regime", "R_usd": "R (USD)", "rule": "Rule",
                                                            "n_baseline_feasible": "Feasible", "n_executed": "Executed",
                                                            "execution_rate": "Exec. (%)", "etw_ratio": "E_TW / baseline",
                                                            "protection_usd": "Protection (USD)", "protection_ci_lo": "CI low",
                                                            "protection_ci_hi": "CI high"})
    reporting.write_table(t, out / "tables" / f"e2_r_scenarios_{a.tag}",
                          {"R (USD)": "{:.2f}", "Exec. (%)": "{:.1f}", "E_TW / baseline": "{:.3f}", "Protection (USD)": "{:,.0f}",
                           "CI low": "{:,.0f}", "CI high": "{:,.0f}"})
    src = json.loads((out / f"manifest_{a.tag}.json").read_text()) if (out / f"manifest_{a.tag}.json").exists() else {}
    reporting.write_manifest("e2", cfg, [out / f"e2_summary_{a.tag}.parquet", out / f"e2_daily_{a.tag}.parquet"], a.tag.split("_")[0],
                             {"analysis": "e2_r_scenarios", "source_tag": a.tag, "source_config_hash": src.get("config_hash"),
                              "source_git_commit": src.get("git_commit")}, tag=f"r_scenarios_{a.tag}", latest=False)
    with pd.option_context("display.width", 250, "display.max_columns", 20):
        print(t.to_string(index=False))


if __name__ == "__main__":
    main()
