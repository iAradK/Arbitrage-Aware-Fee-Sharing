#!/usr/bin/env python3
"""E2 v3: does the hook's own gas suppress corrections that the AMM without the hook would execute? (DECISIONS W2)

Reads the outputs of e2_sequential_replay.py run with `nohook_baseline: true`:
  e2_nohook_steps_<tag>.parquet  every action step of the no-hook baseline path (150k gas), with feasibility tested on
                                 that same state with and without the hook overhead
  e2_daily_<tag>.parquet         per-day aggregates, for the paired day-block bootstrap

State-matched count. On each state of the no-hook path, F150 = [S - C(gas_units) >= R] and F180 = [S - C(gas_units +
hook_overhead) >= R]. Gas cost is increasing in the gas amount, so F180 is a subset of F150 and X = |F150 and not F180| / |F150|
lies in [0, 1]. R is the same in both (calibrated at gas_units on the training months). X is reported per pool, overall and
in the top gas-price decile, and repeated on the real observed pool states as a check that involves no simulated path.

Path-level effect. Mean E_TW of the baseline with hook gas and of the retained margin, each relative to the no-hook
baseline, and the LP outcome per baseline-feasible step, with paired day-block bootstrap intervals.

Decision (hook_gas_rule in e2.yml, fixed before the test run): the hook gas is material in a pool if X >= material_share
or the CI of E_TW(baseline) / E_TW(baseline_nohook) lies entirely above material_etw_ratio.

  python experiments/e2_hook_gas.py --tag valid
  python experiments/e2_hook_gas.py --tag test
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

from common import minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE  # noqa: E402
from e2_bootstrap import PRIMARY  # noqa: E402

CFG = ROOT / "experiments" / "configs" / "e2.yml"


def gas_cutoff(split: str, q: float) -> float:
    """P_q of the gas price over all minutes of the split. Gas is chain-wide, so the cutoff is the same for every pool."""
    t = mg.grid_times(split)
    bg = pd.read_parquet(CACHE / "block_gas.parquet", columns=["timestamp", "gas_price_wei"])
    g = mg.asof_before(pd.DatetimeIndex(bg["timestamp"]), bg["gas_price_wei"].to_numpy(), t)
    return float(np.nanquantile(g, q))


def share(f150: np.ndarray, f180: np.ndarray) -> tuple[int, int, float]:
    n, m = int(f150.sum()), int((f150 & ~f180).sum())
    return n, m, (m / n if n else np.nan)


def boot_share(s: pd.DataFrame, rng, B: int, ci: float) -> tuple[float, float]:
    """Day-block bootstrap interval of X = sum marginal / sum F150."""
    day = pd.DatetimeIndex(s["t"]).floor("D")
    d = pd.DataFrame({"n": s["f150"].to_numpy(), "m": (s["f150"] & ~s["f180"]).to_numpy()}).groupby(day.values).sum().to_numpy(float)
    idx = rng.integers(0, len(d), size=(B, len(d)))
    tot = d[idx].sum(1)
    x = tot[:, 1] / np.maximum(tot[:, 0], 1)
    return tuple(float(v) for v in np.quantile(x, [(1 - ci) / 2, (1 + ci) / 2]))


def path_effects(daily: pd.DataFrame, rng, B: int, ci: float, lam: float, gamma: float) -> dict:
    """Paired day-block bootstrap of mean E_TW relative to the no-hook baseline, and LP outcome per baseline-feasible step."""
    pick = {"baseline": daily["mech"] == "baseline", "baseline_nohook": daily["mech"] == "baseline_nohook",
            "retained": (daily["mech"] == "retained") & np.isclose(daily["lam"], lam) & np.isclose(daily["gamma"], gamma)}
    days = np.sort(daily["day"].unique())
    cols = ["n_steps", "sum_err", "n_bf", "n_exec", "sum_lp_bp"]
    m = {k: daily[v].set_index("day").reindex(days)[cols].fillna(0.0).to_numpy() for k, v in pick.items()}
    idx = rng.integers(0, len(days), size=(B, len(days)))
    etw = {k: v[:, 1].sum() / max(v[:, 0].sum(), 1) for k, v in m.items()}
    etw_b = {k: v[idx, 1].sum(1) / np.maximum(v[idx, 0].sum(1), 1) for k, v in m.items()}
    qs = [(1 - ci) / 2, (1 + ci) / 2]
    out = {}
    for k in ("baseline", "retained"):
        lo, hi = np.quantile(etw_b[k] / etw_b["baseline_nohook"], qs)
        out[f"etw_ratio_{k}"] = (float(etw[k] / etw["baseline_nohook"]), float(lo), float(hi))
    for k, v in m.items():
        out[f"n_exec_{k}"] = int(v[:, 3].sum())
        out[f"lp_bp_{k}"] = float(v[:, 4].sum() / max(v[:, 2].sum(), 1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CFG))
    ap.add_argument("--tag", required=True, help="e2 output tag, e.g. test or test_lag0_med")
    ap.add_argument("--lam", type=float, default=None)
    ap.add_argument("--gamma", type=float, default=None)
    a = ap.parse_args()
    cfg = reporting.load_config(a.config)
    rule = cfg["hook_gas_rule"]
    lam = a.lam if a.lam is not None else cfg["headline"]["lambda"]
    gamma = a.gamma if a.gamma is not None else cfg["headline"]["gamma"]
    split = a.tag.split("_")[0]
    out = reporting.out_dir("e2")
    steps = pd.read_parquet(out / f"e2_nohook_steps_{a.tag}.parquet")
    daily = pd.read_parquet(out / f"e2_daily_{a.tag}.parquet")
    if "baseline_nohook" not in set(daily["mech"]):
        raise SystemExit(f"e2_daily_{a.tag} has no baseline_nohook: rerun e2_sequential_replay.py with nohook_baseline: true")
    cut = gas_cutoff(split, rule["gas_quantile"])
    rng = np.random.default_rng(cfg["seed"])
    B, ci = rule["bootstrap_B"], rule["ci"]
    gas_hook = cfg["gas_units"] + cfg["hook_overhead_gas"]
    rows = []
    for pool, variant in PRIMARY.items():
        s = steps[(steps["pool"] == pool) & (steps["variant"] == variant) & (steps["regime"] == "median")]
        dl = daily[(daily["pool"] == pool) & (daily["variant"] == variant) & (daily["regime"] == "median")]
        if s.empty or dl.empty:
            continue
        R = float(s["R_usd"].iloc[0])
        f150, f180 = s["f150"].to_numpy(), s["f180"].to_numpy()
        top = s["gas_wei"].to_numpy() >= cut
        n, m, x = share(f150, f180)
        n_top, m_top, x_top = share(f150 & top, f180 & top)
        x_lo, x_hi = boot_share(s, rng, B, ci)
        # check on the real observed pool states: no simulated path at all
        g = mg.build_grid(pool, split, variant, lags=(0,))
        c150 = mg.real_state_candidates(g, pool, cfg["gas_units"], R, 0)
        c180 = mg.real_state_candidates(g, pool, gas_hook, R, 0)
        top_r = g["gas_wei"].to_numpy() >= cut
        b150, b180 = c150["baseline_feasible"].to_numpy(), c180["baseline_feasible"].to_numpy()
        nr, mr, xr = share(b150, b180)
        _, _, xr_top = share(b150 & top_r, b180 & top_r)
        pe = path_effects(dl, rng, B, ci, lam, gamma)
        r_b, lo_b, hi_b = pe["etw_ratio_baseline"]
        material_x, material_etw = bool(x >= rule["material_share"]), bool(lo_b > rule["material_etw_ratio"])
        rows.append({"pool": pool, "variant": variant, "R_usd": R,
                     "median_hook_gas_usd": float(s.loc[s["f150"], "C_hook_usd"].median()) if n else np.nan,
                     "n_F150": n, "n_marginal": m, "X": x, "X_ci_lo": x_lo, "X_ci_hi": x_hi,
                     "n_F150_top": n_top, "n_marginal_top": m_top, "X_top": x_top,
                     "n_F150_real": nr, "n_marginal_real": mr, "X_real": xr, "X_real_top": xr_top,
                     "n_exec_nohook": pe["n_exec_baseline_nohook"], "n_exec_baseline": pe["n_exec_baseline"],
                     "etw_ratio_baseline": r_b, "etw_ratio_baseline_lo": lo_b, "etw_ratio_baseline_hi": hi_b,
                     "etw_ratio_retained": pe["etw_ratio_retained"][0], "etw_ratio_retained_lo": pe["etw_ratio_retained"][1],
                     "etw_ratio_retained_hi": pe["etw_ratio_retained"][2],
                     "lp_bp_nohook": pe["lp_bp_baseline_nohook"], "lp_bp_baseline": pe["lp_bp_baseline"], "lp_bp_retained": pe["lp_bp_retained"],
                     "material_X": material_x, "material_etw": material_etw, "material": material_x or material_etw})
    res = pd.DataFrame(rows)
    reporting.write_table(res, out / "tables" / f"e2_hook_gas_{a.tag}",
                          {c: "{:.4f}" for c in res.columns if c.startswith(("X", "etw_ratio"))} | {"lp_bp_nohook": "{:.5f}", "lp_bp_baseline": "{:.5f}",
                                                                                                   "lp_bp_retained": "{:.5f}", "R_usd": "{:.2f}", "median_hook_gas_usd": "{:.4f}"})
    decision = {"tag": a.tag, "gas_cutoff_wei": cut, "rule": rule, "headline": {"lambda": lam, "gamma": gamma},
                "material_any_pool": bool(res["material"].any()), "material_pools": res.loc[res["material"], "pool"].tolist()}
    (out / "tables" / f"e2_hook_gas_{a.tag}.json").write_text(json.dumps(decision, indent=1))
    with pd.option_context("display.width", 250, "display.max_columns", 40):
        print(f"top-decile gas cutoff: {cut / 1e9:.3f} gwei")
        print(res[["pool", "n_F150", "n_marginal", "X", "X_ci_lo", "X_ci_hi", "X_top", "X_real", "X_real_top",
                   "etw_ratio_baseline", "etw_ratio_baseline_lo", "etw_ratio_baseline_hi", "etw_ratio_retained", "material"]].to_string(index=False))
        print("material in any pool:", decision["material_any_pool"])


if __name__ == "__main__":
    main()
