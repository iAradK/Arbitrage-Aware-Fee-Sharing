#!/usr/bin/env python3
"""eps_rel per reference, calibrated on observed price-correcting swaps (validation months only; test months refused).

For every reference the experiments use, eps_rel is set once per day at 00:00 UTC to the rolling 7-day 95th percentile of
THAT reference's relative error |P / P_hat - 1| at the observed price-correcting swaps stamped strictly before that time,
rounded up to whole ppb, and held for the day. P = the benchmark at the swap (Experiment C's pi_bench), P_hat = the
reference the hook would hold:
  lag<d>          the benchmark delayed by d minutes, d in {0, 1, 5, 10, 30, 60} (the aligned data's p_ref_lag_<d>, times
                  the variant's trailing offset, as eC.load_swaps builds pi_hook for d = 1); d = 0 is the benchmark itself
  pyth            ETH/USDC only: the last Pyth on-chain ETH/USD price stamped strictly before the swap (E4's pyth_ref),
                  without a staleness check
  pyth_amax<A>    the same, only swaps whose Pyth price is at most A seconds old (A in {60, 300, 3600}); a stale price is
                  invalid and the hook fails open, so those swaps are not used
One-sided variant (E8): ceil-ppb of max(P95 of the direction-signed error sign(d0) * (1 - P / P_hat), 0), d0 the trader's
token0 change, so that S_hat - S = (P_hat - P) d0 > 0 counts as an overestimate. The E5 scenarios value trades at the
exact reference (eps = 0 by construction; reference "lag0"). Price-correcting, the window, quantile and history rule are
those of eps_observed_calibration.py; the history before the first validation day is the last 8 days of the training
split's correcting swaps of the same reference.

Also per reference:
  fitted once     one P95 over all validation correcting swaps, ceil ppb (E4's "eps fitted once on validation")
  coverage        share of errors <= the eps in force: on the observed swaps (all with a valid reference, and the
                  correcting ones) and on the baseline-feasible candidates of E2's baseline replay at k = 1, 5, 15 with the
                  hook's reference lagged by d (lag references; Pyth: the d = 0 replay's candidates with a valid price)
Checks: d = 0 gives eps = 0 every day; for d = 1 the old error definition |P_hat / P - 1| reproduces
eps_observed_daily_valid.csv (two-sided) exactly when that file is present under the same run root or results/final_valid.

  RESULTS_RUN=<name> python experiments/eps_reference_calibration.py [--workers W]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

import e2_sequential_replay as e2  # noqa: E402
import eQ4_prop_buffer as q4  # noqa: E402
from common import minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, DATA, POOLS, RESULTS  # noqa: E402

OUT = reporting.run_root() / "eps_reference"
LAGS = (0, 1, 5, 10, 30, 60)
PYTH_AMAX = (60, 300, 3600)
CADENCES = (1, 5, 15)
COVER_VARIANTS = [("eth_usdc_005", "raw"), ("eth_wbtc_030", "raw"), ("eth_wbtc_030", "corr24h"),
                  ("eth_wsteth_001", "raw"), ("eth_wsteth_001", "corr24h")]   # the variants E4 / E8 run with a lag


def e2_variants(cfg2: dict) -> list:
    return [(k, v) for k in cfg2["pools"] for v in cfg2["variants"][k]]


def day_starts(t) -> np.ndarray:
    return np.asarray(t).astype("datetime64[D]").astype("datetime64[ns]")


def load_pyth() -> pd.DataFrame:
    pe = pd.read_csv(DATA / "data" / "pyth" / "onchain_ETH_USD_events.csv.gz", usecols=["block_time", "price_usd"])
    pe["t"] = pd.to_datetime(pe["block_time"], utc=True).dt.as_unit("ns")
    return pe.sort_values("t").reset_index(drop=True)


def pyth_at(pyth: pd.DataFrame, t) -> tuple[np.ndarray, np.ndarray]:
    """Last Pyth price stamped strictly before t and its age in seconds (E4's pyth_ref)."""
    tt, pt = pd.DatetimeIndex(t), pd.DatetimeIndex(pyth["t"])
    ref = mg.asof_before(pt, pyth["price_usd"].to_numpy(), tt)
    idx = np.clip(pt.searchsorted(tt, side="left") - 1, 0, None)
    age = (tt - pt[idx]).total_seconds().to_numpy()
    return ref, np.where(np.isfinite(ref), age, np.inf)


def swaps(key: str, var: str, split: str, refs: dict) -> pd.DataFrame:
    """Observed swaps of a split: time, correcting flag, and per reference P_hat (NaN if invalid)."""
    pool = POOLS[key]
    al = mg.load_aligned(key)
    al = al[al["split"] == split].sort_values(["block", "log_index"]).reset_index(drop=True)
    w = mg.VARIANTS[var]
    off = np.exp(np.nan_to_num(al[f"offset_{w}"].to_numpy(), nan=0.0)) if w else 1.0
    P = al["p_ref"].to_numpy(float) * off
    pre, post = al["price_pre"].to_numpy(float), al["price"].to_numpy(float)
    ok = np.isfinite(P) & (P > 0) & np.isfinite(pre) & np.isfinite(post)
    with np.errstate(invalid="ignore"):
        move, gap = np.sign(post - pre), np.sign(P - pre)
    out = pd.DataFrame({"t": q4.naive_ns(al["timestamp"]), "P": P, "ok": ok, "correcting": ok & (move != 0) & (gap != 0) & (move == gap),
                        "sgn_d0": -np.sign(al["amount0"].to_numpy(float))})     # trader's token0 change = -amount0
    for name, spec in refs.items():
        if spec[0] == "lag":
            out[name] = al[f"p_ref_lag_{spec[1]}"].to_numpy(float) * off
        else:                                           # ("pyth", A or None)
            ph, age = pyth_at(spec[2], al["timestamp"])
            out[name] = np.where(age <= (spec[1] if spec[1] is not None else np.inf), ph, np.nan)
    del pool
    return out


def errors(df: pd.DataFrame, ref: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """|P / P_hat - 1|, the signed error, the old |P_hat / P - 1|, and validity, per swap."""
    P, Ph = df["P"].to_numpy(), df[ref].to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        valid = df["ok"].to_numpy() & np.isfinite(Ph) & (Ph > 0)
        e = np.where(valid, np.abs(P / Ph - 1), np.nan)
        s = np.where(valid, df["sgn_d0"].to_numpy() * (1 - P / Ph), np.nan)
        e_old = np.where(valid, np.abs(Ph / P - 1), np.nan)
    return e, s, e_old, valid


def daily(hist_t, hist_e, hist_ok, seed_t, seed_e, days, cfg2, floor0=False) -> tuple[np.ndarray, np.ndarray]:
    v = q4.rolling_q(hist_t, hist_e, hist_ok, seed_t, seed_e, days, cfg2)
    if floor0:
        v = np.maximum(v, 0.0)
    return np.ceil(v * 1e9).astype(np.int64), v


def references(key: str, var: str, pyth) -> dict:
    refs = {f"lag{d}": ("lag", d) for d in LAGS}
    if key == "eth_usdc_005" and var == "raw" and pyth is not None:
        refs["pyth"] = ("pyth", None, pyth)
        refs.update({f"pyth_amax{A}": ("pyth", A, pyth) for A in PYTH_AMAX})
    return refs


def calibrate(args) -> dict:
    key, var, cfg2, with_pyth = args
    t0 = time.time()
    pyth = load_pyth() if with_pyth else None
    refs = references(key, var, pyth)
    va, tr = swaps(key, var, "valid", refs), swaps(key, var, "train", refs)
    W = np.timedelta64(int((cfg2["eps_window_days"] + 1) * 86400e9), "ns")
    tr = tr[tr["t"] >= tr["t"].max() - W]
    days = np.unique(day_starts(va["t"].to_numpy()))
    day_rows, once_rows, cov_rows = [], [], []
    for ref in refs:
        e, s, e_old, valid = errors(va, ref)
        es, ss, es_old, vs = errors(tr, ref)
        okv, oks = valid & va["correcting"].to_numpy(), vs & tr["correcting"].to_numpy()
        t_v, t_s = va["t"].to_numpy(), tr["t"].to_numpy()
        ppb, v = daily(t_v, e, okv, t_s[oks], es[oks], days, cfg2)
        ppb_s, _ = daily(t_v, s, okv, t_s[oks], ss[oks], days, cfg2, floor0=True)
        ppb_old, _ = daily(t_v, e_old, okv, t_s[oks], es_old[oks], days, cfg2)
        hi = np.searchsorted(t_v[okv], days, "left")
        lo = np.searchsorted(t_v[okv], days - np.timedelta64(int(cfg2["eps_window_days"] * 86400e9), "ns"), "left")
        day_rows.append(pd.DataFrame({"pool": key, "variant": var, "reference": ref, "day": days, "eps_rel_ppb": ppb,
                                      "eps_rel_signed_ppb": ppb_s, "eps_rel_old_def_ppb": ppb_old, "eps_rel_unrounded": v,
                                      "n_valid_correcting_in_window": hi - lo}))
        once = int(math.ceil(float(np.quantile(e[okv], cfg2["eps_quantile"])) * 1e9)) if okv.any() else 0
        once_rows.append({"pool": key, "variant": var, "reference": ref, "eps_fitted_once_ppb": once,
                          "n_valid_correcting": int(okv.sum()), "n_valid": int(valid.sum()), "n_swaps": len(va)})
        idx = np.searchsorted(days, day_starts(t_v))
        eps_at = ppb[np.clip(idx, 0, len(days) - 1)] / 1e9
        for qname, m in (("observed swaps (valid reference)", valid), ("observed price-correcting swaps", okv)):
            cov_rows.append({"pool": key, "variant": var, "reference": ref, "query_set": qname, "n": int(m.sum()),
                             "eps_median_bp": float(np.median(eps_at[m])) * 1e4 if m.any() else np.nan,
                             "coverage": float(np.mean(e[m] <= eps_at[m])) if m.any() else np.nan,
                             "err_p95_bp": float(np.quantile(e[m], 0.95)) * 1e4 if m.any() else np.nan})
    print(f"{key} {var}: calibrated {len(refs)} references in {time.time() - t0:.0f}s", flush=True)
    return {"days": pd.concat(day_rows, ignore_index=True), "once": once_rows, "cov": cov_rows}


def replay_coverage(args) -> list:
    """Coverage of each lag reference's daily eps on the baseline-feasible candidates of E2's baseline replay at cadence
    k with the hook's reference lagged by d (Pyth: the d = 0 replay with the Pyth reference)."""
    key, var, d, k, cfg2, days_tab, with_pyth = args
    pool = POOLS[key]
    R = mg.r_regimes_usd(key, var, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
    g = mg.build_grid(key, "valid", var, lags=(0, d))
    C0 = e2.make_configs(cfg2).iloc[[0]].reset_index(drop=True)
    r = e2.simulate(g, pool, C0, cfg2, R, 0.0, d, k)
    bf = r["cand"]["bf"].to_numpy(bool)
    t = q4.naive_ns(g["t"])[bf]
    P = g["p_ref_0"].to_numpy()[bf]
    out = []
    refs = [(f"lag{d}", g[f"p_ref_{d}"].to_numpy()[bf])]
    if with_pyth and d == 0:
        pyth = load_pyth()
        ph, age = pyth_at(pyth, pd.DatetimeIndex(g["t"])[bf])
        refs += [("pyth", ph)] + [(f"pyth_amax{A}", np.where(age <= A, ph, np.nan)) for A in PYTH_AMAX]
    for ref, Ph in refs:
        tab = days_tab[(days_tab.pool == key) & (days_tab.variant == var) & (days_tab.reference == ref)]
        s = pd.Series(tab["eps_rel_ppb"].to_numpy(float), index=pd.DatetimeIndex(tab["day"]))
        eps = s.reindex(pd.DatetimeIndex(day_starts(t))).to_numpy() / 1e9
        with np.errstate(invalid="ignore", divide="ignore"):
            e = np.abs(P / Ph - 1)
        m = np.isfinite(e) & np.isfinite(eps)
        out.append({"pool": key, "variant": var, "reference": ref, "query_set": f"replay candidates k = {k}", "n": int(m.sum()),
                    "eps_median_bp": float(np.median(eps[m])) * 1e4 if m.any() else np.nan,
                    "coverage": float(np.mean(e[m] <= eps[m])) if m.any() else np.nan,
                    "err_p95_bp": float(np.quantile(e[m], 0.95)) * 1e4 if m.any() else np.nan})
    return out


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    ap.add_argument("--workers", type=int, default=9)
    a = ap.parse_args()
    if a.split != "valid":
        raise SystemExit("validation-only: the test months are refused")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    cfg2 = reporting.load_config(ROOT / "experiments" / "configs" / "e2.yml")
    pvs = e2_variants(cfg2)
    with ProcessPoolExecutor(a.workers) as ex:
        res = list(ex.map(calibrate, [(k, v, cfg2, (k, v) == ("eth_usdc_005", "raw")) for k, v in pvs]))
    days = pd.concat([x["days"] for x in res], ignore_index=True)
    once = pd.DataFrame([r for x in res for r in x["once"]])
    cov = [r for x in res for r in x["cov"]]
    jobs = [(k, v, d, kk, cfg2, days, (k, v) == ("eth_usdc_005", "raw")) for k, v in COVER_VARIANTS for d in LAGS for kk in CADENCES]
    with ProcessPoolExecutor(a.workers) as ex:
        for rows in ex.map(replay_coverage, jobs):
            cov += rows
    cov = pd.DataFrame(cov)
    days.to_csv(OUT / "eps_ref_daily_valid.csv", index=False)
    once.to_csv(OUT / "eps_ref_fitted_once_valid.csv", index=False)
    cov.to_csv(OUT / "eps_ref_coverage_valid.csv", index=False)
    summ = days.groupby(["pool", "variant", "reference"]).agg(eps_median_ppb=("eps_rel_ppb", "median"), eps_min_ppb=("eps_rel_ppb", "min"),
                                                             eps_max_ppb=("eps_rel_ppb", "max"),
                                                             eps_signed_median_ppb=("eps_rel_signed_ppb", "median")).reset_index()
    summ = summ.merge(once, on=["pool", "variant", "reference"])
    reporting.write_table(summ, OUT / "tables" / "eps_ref_summary_valid")
    # checks
    chk = []
    z = days[days.reference == "lag0"]
    chk.append({"check": "lag0 (the benchmark itself) gives eps = 0 every day, two-sided and one-sided",
                "ok": bool((z["eps_rel_ppb"] == 0).all() and (z["eps_rel_signed_ppb"] == 0).all())})
    for root in (reporting.run_root(), RESULTS / "final_valid"):
        f = root / "eps_observed" / "eps_observed_daily_valid.csv"
        if f.exists():
            old = pd.read_csv(f)
            old["day"] = pd.to_datetime(old["day"]).astype("datetime64[ns]")
            m = old.merge(days[days.reference == "lag1"][["pool", "variant", "day", "eps_rel_old_def_ppb"]], on=["pool", "variant", "day"])
            chk.append({"check": f"lag1 with the old definition |P_hat/P - 1| reproduces {f.relative_to(ROOT).as_posix()} "
                                 f"({len(m)} of {len(old)} pool-days matched)",
                        "ok": bool(len(m) == len(old) and (m["eps_rel_ppb"] == m["eps_rel_old_def_ppb"]).all())})
            break
    checks = pd.DataFrame(chk)
    checks.to_csv(OUT / "eps_ref_checks_valid.csv", index=False)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
    mf = {"experiment": "eps_reference", "split": "valid", "git_commit": head + ("+dirty" if dirty else ""), "uncommitted": dirty,
          "parameters": {"error": "|P / P_hat - 1|", "signed": "sign(d0) * (1 - P / P_hat), floored at 0",
                         "update": "daily at 00:00 UTC, errors strictly before, rounded up to whole ppb", "lags_min": list(LAGS),
                         "pyth_amax_s": list(PYTH_AMAX), "cadences": list(CADENCES), "eps_window_days": cfg2["eps_window_days"],
                         "eps_quantile": cfg2["eps_quantile"], "seed": "last eps_window_days + 1 days of the train split"},
          "checks": json.loads(checks.to_json(orient="records")),
          "inputs": {p.relative_to(ROOT).as_posix(): sha(p) for p in
                     [CACHE / "aligned" / f"{k}.parquet" for k in cfg2["pools"]] + [DATA / "data" / "pyth" / "onchain_ETH_USD_events.csv.gz"]},
          "outputs": {p.name: sha(p) for p in sorted(OUT.glob("eps_ref_*_valid.csv"))}}
    (OUT / "manifest_valid.json").write_text(json.dumps(mf, indent=1, default=str))
    with pd.option_context("display.width", 250, "display.max_columns", 30, "display.max_rows", 300):
        print(checks.to_string(index=False))
        print(summ.to_string(index=False))
    assert checks["ok"].all(), "a check failed"
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
