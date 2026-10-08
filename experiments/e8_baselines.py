#!/usr/bin/env python3
"""E8: replayed baselines and gas/buffer sensitivities on the E2 traces (Phase 2 items 2.3 and 2.4).

Re-simulates the frozen E2 replay (e2_sequential_replay.simulate) with additional configurations. Configuration families:
  as_frozen  the E2 headline configurations, unchanged. Each run asserts that they equal the E2 rows of the same tag.
  settle     the charged configurations, with the settlement gas paid on charged swaps and included in the hook's g_hat.
  onesided   buffered rules with the one-sided buffer (P95 of S_hat - S), with and without settlement gas.
  mevtax     MEV tax under competitive bidding (t/(1+t) of the margin), t from the config.
  dynfee     oracle-deviation dynamic fee on a beta grid, plus beta calibrated on the training months (`dynfee_cal`).

  python experiments/e8_baselines.py --calibrate                    # beta on the training months
  python experiments/e8_baselines.py --split valid                  # all runs on the validation months
  python experiments/e8_baselines.py --freeze
  python experiments/e8_baselines.py --split test --confirm-frozen
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

import e2_sequential_replay as e2  # noqa: E402
from common import minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS  # noqa: E402

CFG = ROOT / "experiments" / "configs" / "e8.yml"
BOOT_COLS = ["n_steps", "sum_err", "n_bf", "n_exec", "n_viol_q0", "sum_r", "sum_pi"]
COMPARE = ["n_steps", "n_baseline_feasible", "n_executed", "execution_rate", "participation_violation_rate", "cap_violation_rate",
           "etw_mean", "etw_p95", "etw_p99", "etw_post_mean", "protection_usd", "searcher_net_usd", "gross_surplus_usd",
           "executed_correction", "mean_lp_vs_hodl_bp", "recapture_rate"]
STEP_MECHS = ("retained", "retained+settle", "mevtax_t5.7", "dynfee_cal")   # per-correction transfers kept for item 2.6


def load_cfgs(path):
    cfg = reporting.load_config(path)
    cfg2 = reporting.load_config(ROOT / cfg["e2_config"])
    return cfg, cfg2


def cal_path() -> Path:
    return reporting.out_dir("e8") / "dynfee_calibration.json"


def hashed_cfg(cfg: dict, cfg2: dict) -> dict:
    """What the freeze covers: the E8 config, the E2 config it reads, and the calibrated betas."""
    cal = json.loads(cal_path().read_text()) if cal_path().exists() else None
    return {"e8": cfg, "e2_hash": reporting.config_hash(cfg2), "beta_cal": cal and {k: v["beta"] for k, v in cal["pools"].items()}}


def configs(cfg: dict, cfg2: dict, beta_cal: float | None) -> pd.DataFrame:
    h = cfg["headline"]
    final = "final" in cfg2                             # e8_final.yml: the final rule's traces (e2_final.yml)
    C2 = e2.final_configs(cfg2) if final else e2.make_configs(cfg2)
    lam_ok, gam_ok = np.isclose(C2["lam"], h["lambda"]), np.isclose(C2["gamma"], h["gamma"])
    keep = (C2["mech"] == "baseline") | C2["mech"].str.startswith("static") | \
           (C2["mech"].isin(["unconstrained", "cap_gamma0"]) & lam_ok) | \
           (C2["mech"].isin(["retained", "buffered_1eps", "buffered_2eps"]) & lam_ok & gam_ok)
    base = C2[keep].reset_index(drop=True)
    assert base["mech"].iloc[0] == "baseline" and base["mech"].is_unique
    if final:                                           # keep the final rule's charged gas, g_hat and buffer
        base = base.assign(family="as_frozen", beta=0.0, tax_t=0.0)
        g_set = float(e2.final_configs(cfg2, cfg["final_settle"])["gas_ch"].iloc[0])
    else:
        base = base.assign(family="as_frozen", gas_ch=base["gas"], ghat=base["gas"], buf="abs", beta=0.0, tax_t=0.0)
        g_set = float(cfg2["gas_units"] + cfg["hook_overhead_settled_gas"])
    charged = base[base["mech"] != "baseline"]
    settle = charged.assign(family="settle", mech=charged["mech"] + "+settle", gas_ch=g_set, ghat=g_set)
    buf = base[base["mech"].str.startswith("buffered")]
    one = pd.concat([buf.assign(mech=buf["mech"] + "+1s"),
                     buf.assign(mech=buf["mech"] + "+settle+1s", gas_ch=g_set, ghat=g_set)]).assign(family="onesided",
                                                                                                buf="prop1s" if final else "signed")
    proto = base.iloc[[0]]
    if final:                                           # the MEV tax and the dynamic fee pay the uncharged hook gas, no settlement
        proto = proto.assign(gas_ch=proto["gas"], ghat=proto["gas"], buf="abs")
    mev = pd.concat([proto.assign(mech=f"mevtax_t{t:g}", kind=e2.MEVTAX, tax_t=float(t)) for t in cfg["mev_tax_t"]]).assign(family="mevtax")
    betas = [(f"dynfee_b{b:g}", float(b)) for b in cfg["dynfee_beta_grid"]] + ([("dynfee_cal", float(beta_cal))] if beta_cal is not None else [])
    dyn = pd.concat([proto.assign(mech=m, kind=e2.DYNFEE, beta=b) for m, b in betas]).assign(family="dynfee")
    C = pd.concat([base, settle, one, mev, dyn], ignore_index=True)
    assert C["mech"].is_unique
    return C


# ---------------------------------------------------------------- calibration of the dynamic fee (training months)
def calibrate(cfg: dict, cfg2: dict) -> None:
    cc, h = cfg["dynfee_calibration"], cfg["headline"]
    out = {"rule": "smallest beta whose recovered total on the training months equals the retained margin's; "
                   "if none does, the revenue-maximising beta", "settings": cc, "pools": {}}
    C_all = configs(cfg, cfg2, None)
    for key in cfg["pools"]:
        t0 = time.time()
        variant = cfg["variants"][key]
        R = mg.r_regimes_usd(key, variant, cfg2["gas_units"], cfg2["r_quantiles"])[cc["regime"]]
        g = mg.build_grid(key, cc["split"], variant, lags=(0, cc["lag"]))
        C = C_all[C_all["mech"].isin(["baseline", "retained"]) | C_all["mech"].str.startswith("dynfee_b")].reset_index(drop=True)

        def run(Cx):
            return e2.simulate(g, POOLS[key], Cx, cfg2, R, 0.0, cc["lag"], cc["cadence"])["table"].set_index("mech")

        tab = run(C)
        target = float(tab.loc["retained", "protection_usd"])
        grid = [(float(C.loc[C.mech == m, "beta"].iloc[0]), float(tab.loc[m, "protection_usd"])) for m in C.mech if m.startswith("dynfee_b")]
        reach = [i for i, (_, rev) in enumerate(grid) if rev >= target]
        steps = []
        if reach:
            j = reach[0]
            lo, hi = (grid[j - 1][0] if j > 0 else 0.0), grid[j][0]
            proto = C[C.mech == "dynfee_b" + f"{grid[j][0]:g}"]
            for _ in range(cc["bisection_steps"]):
                mid = 0.5 * (lo + hi)
                rev = float(run(pd.concat([C.iloc[[0]], proto.assign(beta=mid, mech="probe")], ignore_index=True)).loc["probe", "protection_usd"])
                steps.append((mid, rev))
                lo, hi = (lo, mid) if rev >= target else (mid, hi)
            beta, status = hi, "target reached"
        else:
            beta, status = max(grid, key=lambda x: x[1])[0], "target not reached: revenue-maximising beta on the grid"
        out["pools"][key] = {"variant": variant, "R_usd": R, "target_retained_usd": target, "beta": beta, "status": status,
                             "grid": [{"beta": b, "recovered_usd": r} for b, r in grid],
                             "bisection": [{"beta": b, "recovered_usd": r} for b, r in steps],
                             "retained_etw": float(tab.loc["retained", "etw_mean"]), "baseline_etw": float(tab.loc["baseline", "etw_mean"])}
        print(f"{key}: target {target:,.0f} USD, beta {beta:.4g} ({status}), {time.time() - t0:.0f}s", flush=True)
    cal_path().write_text(json.dumps(out, indent=1))


# ---------------------------------------------------------------- replay runs
def run_tag(cfg, cfg2, split, d, k, regimes, cal, prior, tag):
    tables, dailies, steps = [], [], []
    for key in cfg["pools"]:
        t0 = time.time()
        variant, pool = cfg["variants"][key], POOLS[key]
        C = configs(cfg, cfg2, cal[key]["beta"] if cal else None)
        R_all = mg.r_regimes_usd(key, variant, cfg2["gas_units"], cfg2["r_quantiles"])
        g = mg.build_grid(key, split, variant, lags=(0, d))
        for rname in regimes:
            R = R_all[rname]
            seed = e2.eps_seed(key, variant, R, d, k, prior, cfg2)
            if "final" in cfg2:
                er = e2.final_eps_rel(cfg2, key, variant, g, reference=f"lag{d}")
                ers = e2.final_eps_rel(cfg2, key, variant, g, cfg["eps_signed_source"], "eps_rel_signed_ppb", reference=f"lag{d}")
                r = e2.simulate(g, pool, C, cfg2, R, np.nan, d, k, seed, eps_rel=er, eps_rel_signed=ers)
            else:
                r = e2.simulate(g, pool, C, cfg2, R, np.nan, d, k, seed)
            t = r["table"].merge(C[["mech", "family", "beta", "tax_t", "buf"]], on="mech")
            t.insert(0, "regime", rname); t.insert(0, "variant", variant); t.insert(0, "pool", key)
            t["R_usd"], t["hook_lag_min"], t["cadence_min"] = R, d, k
            tables.append(t)
            dl = r["daily"].merge(C[["mech", "family"]].reset_index().rename(columns={"index": "cfg"}), on="cfg")
            dl.insert(0, "regime", rname); dl.insert(0, "variant", variant); dl.insert(0, "pool", key)
            dailies.append(dl)
            if rname == "median" and d == 0 and k == 1:
                for m in STEP_MECHS:
                    j = int(np.flatnonzero(C["mech"].to_numpy() == m)[0]) if m in set(C["mech"]) else None
                    if j is None:
                        continue
                    ex = r["step_exec"][:, j]
                    steps.append(pd.DataFrame({"pool": key, "variant": variant, "regime": rname, "mech": m,
                                               "t": g["t"].to_numpy()[ex], "r_usd": r["step_protection"][ex, j].astype(float)}))
        print(f"  {tag} {key} done in {time.time() - t0:.0f}s", flush=True)
    summ = pd.concat(tables, ignore_index=True)
    daily = pd.concat(dailies, ignore_index=True)
    return summ, daily, (pd.concat(steps, ignore_index=True) if steps else None)


def check_as_frozen(summ: pd.DataFrame, tag: str) -> str:
    """The as-frozen rows must equal the E2 rows of the same tag."""
    f = reporting.out_dir("e2") / f"e2_summary_{tag}.parquet"       # the same run root as E8's (RESULTS_RUN)
    if not f.exists():
        return "no E2 output for this tag"
    e2s = pd.read_parquet(f)
    a = summ[summ["family"] == "as_frozen"]
    m = a.merge(e2s, on=["pool", "variant", "regime", "mech", "lam", "gamma", "dmult", "phi"], suffixes=("", "_e2"))
    missing = sorted(set(a["pool"]) - set(e2s["pool"]))
    if missing:                                          # the final E2 delay grid covers ETH/USDC only
        a = a[~a["pool"].isin(missing)]
    assert len(m) == len(a), f"{tag}: {len(a) - len(m)} as-frozen rows without an E2 counterpart"
    bad = [c for c in COMPARE if not np.array_equal(m[c].to_numpy(), m[c + "_e2"].to_numpy(), equal_nan=True)]
    assert not bad, f"{tag}: as-frozen rows differ from E2 in {bad}"
    return f"{len(m)} as-frozen rows bit-identical to {f.relative_to(ROOT).as_posix()}" + (
        f"; no E2 rows for {', '.join(missing)} in this tag" if missing else "")


def bootstrap(daily: pd.DataFrame, B: int, seed: int) -> pd.DataFrame:
    """Paired day-block bootstrap (same days for every configuration), as e2_bootstrap.py; one table per regime."""
    rows = []
    for (pool, regime), g in daily.groupby(["pool", "regime"]):
        days = np.sort(g["day"].unique())
        idx = np.random.default_rng(seed).integers(0, len(days), size=(B, len(days)))
        for mech, h in g.groupby("mech"):
            m = h.set_index("day").reindex(days)[BOOT_COLS].fillna(0.0).to_numpy()
            tot, bs = m.sum(0), m[idx].sum(1)
            for name, f in {"execution_rate": lambda s: s[..., 3] / np.maximum(s[..., 2], 1),
                            "violation_rate": lambda s: s[..., 4] / np.maximum(s[..., 2], 1),
                            "etw_mean": lambda s: s[..., 1] / np.maximum(s[..., 0], 1),
                            "protection_usd": lambda s: s[..., 5]}.items():
                lo, hi = np.quantile(f(bs), [0.025, 0.975])
                rows.append({"pool": pool, "regime": regime, "mech": mech, "metric": name, "point": float(f(tot)),
                             "ci_lo": float(lo), "ci_hi": float(hi), "n_days": len(days)})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CFG))
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    ap.add_argument("--confirm-frozen", action="store_true")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--only", default=None, help="comma-separated tags to run (default: all)")
    a = ap.parse_args()
    cfg, cfg2 = load_cfgs(a.config)
    out = reporting.out_dir("e8")
    if a.calibrate:
        calibrate(cfg, cfg2)
        return
    if "final" in cfg2:                                  # not frozen yet: validation only, gas checked against the gas config
        if a.split != "valid" or a.freeze:
            raise SystemExit("the final-rule E8 config runs on the validation months only until it is frozen")
        gj = json.loads((ROOT / "config" / "gas_block_scope.json").read_text())["hook_overhead_gas"]
        assert (cfg2["hook_overhead_gas"], cfg2["hook_overhead_gas_charged"], cfg2["hook_overhead_gas_charged_empty_vault"]) == (
            gj["first_swap_of_block"], gj["charged_swap"], gj["charged_swap_empty_vault_sensitivity"])
    else:
        assert reporting.config_hash(cfg2) == reporting.frozen_path("e2").read_text().strip(), "E2 config is not the frozen one"
    hc = hashed_cfg(cfg, cfg2)
    if a.freeze:
        assert hc["beta_cal"] is not None, "run --calibrate first"
        reporting.freeze("e8", hc)
        print("config frozen:", reporting.config_hash(hc)[:12])
        return
    reporting.guard_split("e8", a.split, a.confirm_frozen, hc)
    cal = json.loads(cal_path().read_text())["pools"] if cal_path().exists() else None
    prior = e2.PRIOR[a.split]
    runs = [(cfg["ideal"]["lag"], cfg["ideal"]["cadence"], cfg["ideal"]["regimes"], a.split)]
    for d in cfg["delayed"]["lags"]:
        for k in cfg["delayed"]["cadences"]:
            runs.append((d, k, cfg["delayed"]["regimes"], f"{a.split}_lag{d}" + (f"_k{k}" if k != 1 else "") + "_med"))
    only = set(a.only.split(",")) if a.only else None
    checks = {}
    for d, k, regimes, tag in runs:
        if only and tag not in only:
            continue
        summ, daily, steps = run_tag(cfg, cfg2, a.split, d, k, regimes, cal, prior, tag)
        checks[tag] = check_as_frozen(summ, tag)
        print("  check:", checks[tag], flush=True)
        summ.to_parquet(out / f"e8_summary_{tag}.parquet", index=False)
        daily.to_parquet(out / f"e8_daily_{tag}.parquet", index=False)
        if steps is not None:
            steps.to_parquet(out / f"e8_steps_{tag}.parquet", index=False)
        bs = bootstrap(daily, cfg2["hook_gas_rule"]["bootstrap_B"], cfg["seed"])
        reporting.write_table(bs, out / "tables" / f"e8_bootstrap_{tag}", {"point": "{:.4g}", "ci_lo": "{:.4g}", "ci_hi": "{:.4g}"})
        cols = ["pool", "regime", "family", "mech", "execution_rate", "participation_violation_rate", "cap_violation_rate", "etw_mean",
                "protection_usd", "searcher_net_usd", "recapture_rate", "eps_S_used_median_usd"]
        reporting.write_table(summ[cols], out / "tables" / f"e8_summary_{tag}",
                              {"execution_rate": "{:.4f}", "participation_violation_rate": "{:.4f}", "cap_violation_rate": "{:.4f}",
                               "etw_mean": "{:.3e}", "protection_usd": "{:,.0f}", "searcher_net_usd": "{:,.0f}", "recapture_rate": "{:.4f}"})
        reporting.write_manifest("e8", hc, [CACHE / "aligned" / f"{kk}.parquet" for kk in cfg["pools"]], a.split,
                                 {"hook_lag_min": d, "cadence_min": k, "as_frozen_check": checks[tag]}, tag=tag)
    print(json.dumps(checks, indent=1))


if __name__ == "__main__":
    main()
