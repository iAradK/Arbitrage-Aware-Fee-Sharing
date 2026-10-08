#!/usr/bin/env python3
"""eps_rel calibrated on observed price-correcting swaps (validation months only; the test months are refused).

The hook holds eps_rel as an admin-set whole-ppb value (epsilonRelPpb). This calibration sets it once per day, at
00:00 UTC, to the rolling 7-day 95th percentile of the relative reference error |P_hat / P - 1| at the observed
price-correcting swaps stamped strictly before that time, rounded up to whole ppb, and holds it for the day.

  P_hat   the hook's reference at the swap: Experiment C's pi_hook (the reference one minute earlier, eC.load_swaps)
  P       the benchmark at the swap: Experiment C's pi_bench
  price-correcting   the swap moves the pool price toward the benchmark: sign(price - price_pre) =
          sign(pi_bench - price_pre) != 0 (pool prices from the aligned data, before and after the swap). The counts
          also give the subset that ends closer to the benchmark than it started (no overshoot past the far side), and
          Experiment C's arbitrage flag.
The window, quantile and history rule are E2's (eps_window_days = 7, eps_quantile = 0.95, all history if fewer than 20
errors in the window: q4.rolling_q, as Q4 and Q5's daily variant). The history before the first validation day is
the last eps_window_days + 1 days of the preceding (training) split, as Q4's price_seed seeds its series.

Compared, at the same query points, with the two candidate-based calibrations:
  k1   rolling p95 over the baseline-feasible candidates of the k = 1, d = 1 baseline replay (Q4's observed-swap series,
       ~28 bp), per query and in the daily whole-ppb form;
  k15  the same over the k = 15, d = 1 replay's candidates (the Q4 harness's series, Q6a's eps, 12.8 bp at the
       opportunities), per query and daily.
Query sets: observed swaps with a valid reference, the price-correcting ones, the k = 1 and k = 15 replay candidates
(their own errors), and the k = 15 replay's executed corrections (the buffered set's opportunities). Coverage = share
of the query set's errors <= eps in force at the query.

Checks: the k1 per-query series equals the stored Q4 eps series (results/eQ4, read-only copy); the k15 series at the
executed corrections equals the stored Q4 cross-block opportunities' eps; the daily value is whole ppb and never
uses an error stamped at or after its day's 00:00 UTC.

  python experiments/eps_observed_calibration.py [--workers W] [--variants eC|e2]
(RESULTS_RUN=<name> writes to results/<name>/eps_observed instead of results/eps_observed.)
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
import eC_same_block_replay as eC  # noqa: E402
import eQ4_prop_buffer as q4  # noqa: E402
import eQ4_splitting as q4s  # noqa: E402
from common import minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS, RESULTS  # noqa: E402

OUT = reporting.run_root() / "eps_observed"          # results/eps_observed, or results/<RESULTS_RUN>/eps_observed
POOL_VARIANTS = eC.POOL_VARIANTS                      # --variants eC (default): ETH/USDC raw, ETH/WBTC corr24h and raw


def e2_pool_variants() -> list:
    """--variants e2: every pool variant of the E2 replays (experiments/configs/e2.yml), same method."""
    cfg = reporting.load_config(ROOT / "experiments" / "configs" / "e2.yml")
    return [(k, v) for k in cfg["pools"] for v in cfg["variants"][k]]


def observed(key: str, var: str, split: str) -> pd.DataFrame:
    """Experiment C's swaps of a split with the reference error and the price-correcting flags."""
    al = eC.load_swaps(key, var, split)
    pre, post, P, Ph = (al[c].to_numpy(float) for c in ("price_pre", "price", "pi_bench", "pi_hook"))
    ok = np.isfinite(P) & np.isfinite(Ph) & (P > 0) & (Ph > 0) & np.isfinite(pre) & np.isfinite(post)
    with np.errstate(invalid="ignore", divide="ignore"):
        err = np.abs(Ph / P - 1)
        move, gap = np.sign(post - pre), np.sign(P - pre)
    corr = ok & (move != 0) & (gap != 0) & (move == gap)
    closer = corr & (np.abs(post - P) < np.abs(pre - P))
    return pd.DataFrame({"t": q4.naive_ns(al["timestamp"]), "err": np.where(ok, err, np.nan), "valid_ref": ok,
                         "correcting": corr, "ends_closer": closer, "arb": al["arb"].to_numpy(bool),
                         "block": al["block"].to_numpy(), "log_index": al["log_index"].to_numpy()})


def day_starts(t) -> np.ndarray:
    return np.asarray(t).astype("datetime64[D]").astype("datetime64[ns]")


def daily(hist_t, hist_err, hist_ok, seed_t, seed_err, query_t, cfg2) -> tuple[np.ndarray, pd.DataFrame]:
    """The daily whole-ppb eps in force at each query time, and the day table (eps set at 00:00 UTC from the errors
    strictly before it)."""
    days = np.unique(day_starts(query_t))
    v = q4.rolling_q(hist_t, hist_err, hist_ok, seed_t, seed_err, days, cfg2)
    ppb = np.ceil(v * 1e9)
    tab = pd.DataFrame({"day": days, "eps_rel_ppb": ppb.astype(np.int64), "eps_rel_unrounded": v})
    idx = np.searchsorted(days, day_starts(query_t))
    return ppb[idx] / 1e9, tab


def replays(key: str, var: str, cfg2: dict, lam: float, gam: float) -> dict:
    """The k = 1 and k = 15 (d = 1) validation replays of E2 (as Q4 / Q6b): grid, simulate result, seed, eps series."""
    pool = POOLS[key]
    R_usd = mg.r_regimes_usd(key, var, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
    out = {}
    C_all = e2.make_configs(cfg2)
    mech, k15, d15 = q4s.RULE
    j = C_all.index[(C_all["mech"] == mech) & (C_all["lam"] == lam) & (C_all["gamma"] == gam)][0]
    for name, k, d, C in (("k1", 1, 1, C_all.iloc[[0]].reset_index(drop=True)),
                          ("k15", k15, d15, C_all.loc[[0, j]].reset_index(drop=True))):
        seed = q4.price_seed(key, var, R_usd, d, k, e2.PRIOR["valid"], cfg2)
        g = mg.build_grid(key, "valid", var, lags=(0, d))
        r = e2.simulate(g, pool, C, cfg2, R_usd, np.nan, d, k, seed[["t", "err_usd", "err_signed_usd"]])
        es = q4.eps_series(r, g, seed, k, d, cfg2)
        cb = r["cand"]["bf"].to_numpy(bool)
        P, Ph = g["p_ref_0"].to_numpy(), g[f"p_ref_{d}"].to_numpy()
        with np.errstate(invalid="ignore", divide="ignore"):
            err = np.abs(Ph / P - 1)
        o = {"t": q4.naive_ns(g["t"]), "err": err, "cb": cb & np.isfinite(err), "seed_t": q4.naive_ns(seed["t"]),
             "seed_err": seed["err_rel"].to_numpy(float), "es": es}
        if name == "k15":
            act = np.flatnonzero(es["act"].to_numpy())
            ex = act[r["step_exec"][act, 1].astype(bool)]
            o["opp_i"] = ex
        out[name] = o
    return out


def one(args) -> dict:
    key, var, cfg2, lam, gam = args
    t0 = time.time()
    tr, va = observed(key, var, "train"), observed(key, var, "valid")
    W = np.timedelta64(int((cfg2["eps_window_days"] + 1) * 86400e9), "ns")
    tr = tr[tr["t"] >= tr["t"].max() - W]
    sd = tr[tr["correcting"]]
    rp = replays(key, var, cfg2, lam, gam)
    k1, k15 = rp["k1"], rp["k15"]

    # query sets: (times, errors)
    vr = va[va["valid_ref"]]
    qs = {"observed swaps (valid reference)": (vr["t"].to_numpy(), vr["err"].to_numpy()),
          "observed price-correcting swaps": (vr.loc[vr["correcting"], "t"].to_numpy(), vr.loc[vr["correcting"], "err"].to_numpy()),
          "k1 replay candidates": (k1["t"][k1["cb"]], k1["err"][k1["cb"]]),
          "k15 replay candidates": (k15["t"][k15["cb"]], k15["err"][k15["cb"]]),
          "k15 executed corrections (opportunities)": (k15["t"][k15["opp_i"]], k15["err"][k15["opp_i"]])}
    # calibrations: name -> function(times) -> eps
    obs_hist = (va["t"].to_numpy(), va["err"].to_numpy(), va["correcting"].to_numpy(), sd["t"].to_numpy(), sd["err"].to_numpy())
    cal = {
        "observed daily (ppb)": lambda t: daily(*obs_hist, t, cfg2)[0],
        "observed rolling (per query)": lambda t: q4.rolling_q(*obs_hist, t, cfg2),
        "k1 rolling (per query)": lambda t: q4.rolling_q(k1["t"], k1["err"], k1["cb"], k1["seed_t"], k1["seed_err"], t, cfg2),
        "k1 daily (ppb)": lambda t: daily(k1["t"], k1["err"], k1["cb"], k1["seed_t"], k1["seed_err"], t, cfg2)[0],
        "k15 rolling (per query)": lambda t: q4.rolling_q(k15["t"], k15["err"], k15["cb"], k15["seed_t"], k15["seed_err"], t, cfg2),
        "k15 daily (ppb)": lambda t: daily(k15["t"], k15["err"], k15["cb"], k15["seed_t"], k15["seed_err"], t, cfg2)[0],
    }
    rows = []
    for cname, f in cal.items():
        for qname, (t, e) in qs.items():
            uq, inv = np.unique(t, return_inverse=True)
            eps = f(uq)[inv]
            rows.append({"pool": key, "variant": var, "calibration": cname, "query_set": qname, "n": len(t),
                         "eps_median_bp": float(np.median(eps)) * 1e4, "eps_p10_bp": float(np.quantile(eps, 0.1)) * 1e4,
                         "eps_p90_bp": float(np.quantile(eps, 0.9)) * 1e4, "coverage": float(np.mean(e <= eps)),
                         "err_p50_bp": float(np.median(e)) * 1e4, "err_p95_bp": float(np.quantile(e, 0.95)) * 1e4})
    # the day table an admin would set
    _, days = daily(*obs_hist, va["t"].to_numpy(), cfg2)
    wins = []
    ht = np.concatenate([sd["t"].to_numpy(), va.loc[va["correcting"], "t"].to_numpy()])
    for d in days["day"].to_numpy():
        hi = np.searchsorted(ht, d, "left")
        lo = np.searchsorted(ht, d - np.timedelta64(int(cfg2["eps_window_days"] * 86400e9), "ns"), "left")
        wins.append(hi - lo)
    days["n_errors_in_window"] = wins
    days.insert(0, "variant", var)
    days.insert(0, "pool", key)
    counts = {"pool": key, "variant": var, "n_swaps_valid": len(va), "n_valid_reference": int(va["valid_ref"].sum()),
              "n_price_correcting": int(va["correcting"].sum()), "n_correcting_ends_closer": int(va["ends_closer"].sum()),
              "n_correcting_overshoots": int((va["correcting"] & ~va["ends_closer"]).sum()),
              "n_eC_arb": int(va["arb"].sum()), "n_correcting_and_eC_arb": int((va["correcting"] & va["arb"]).sum()),
              "n_eC_arb_not_correcting": int((va["arb"] & ~va["correcting"]).sum()),
              "share_correcting": float(va["correcting"].sum() / va["valid_ref"].sum()),
              "n_seed_swaps_train": len(tr), "n_seed_correcting_train": len(sd),
              "seed_from": str(tr["t"].min()), "seed_to": str(tr["t"].max())}
    # checks
    chk = []
    st = RESULTS / "eQ4" / f"eQ4_eps_series_{key}_{var}_valid.csv.gz"
    if st.exists():
        s_ = pd.read_csv(st, float_precision="round_trip")
        chk.append({"check": f"k1 eps series = stored Q4 eps series ({key} {var})",
                    "ok": bool(np.array_equal(np.nan_to_num(s_["eps_P_rel"].to_numpy(), nan=-1),
                                              np.nan_to_num(k1["es"]["eps_P_rel"].to_numpy(), nan=-1)))})
    op = RESULTS / "eQ4" / "eQ4_crossblock_opportunities_valid.csv.gz"
    if op.exists():
        s_ = pd.read_csv(op, float_precision="round_trip")
        s_ = s_[(s_.pool == key) & (s_.variant == var) & (s_.rule == "prop_rel") & (s_.N == 1) & (s_["mode"] == "dynamic")]
        mine = pd.DataFrame({"t": k15["t"][k15["opp_i"]].astype(np.int64),
                             "eps_mine": k15["es"]["eps_P_rel"].to_numpy()[k15["opp_i"]]})
        mm = mine.merge(s_.assign(t=q4.naive_ns(pd.to_datetime(s_["t"])).astype(np.int64))[["t", "eps"]], on="t")
        chk.append({"check": f"k15 eps at the executed corrections = stored Q4 cross-block eps ({key} {var}); "
                             f"{len(mm)} of {len(s_)} stored opportunities matched", "ok": bool(len(mm) == len(s_) and
                                                                                             (mm["eps_mine"] == mm["eps"]).all())})
    ppb = days["eps_rel_ppb"].to_numpy()
    chk.append({"check": f"daily eps is whole ppb, and recomputing each day from errors strictly before 00:00 UTC gives the "
                         f"same value ({key} {var})",
                "ok": bool(np.array_equal(ppb, np.ceil(days["eps_rel_unrounded"].to_numpy() * 1e9)) and all(
                    math.ceil(float(np.quantile(win, cfg2["eps_quantile"])) * 1e9) == p
                    for win, p in ((_window(ht, np.concatenate([sd["err"].to_numpy(), va.loc[va["correcting"], "err"].to_numpy()]),
                                            d, cfg2), p) for d, p in zip(days["day"].to_numpy(), ppb)) if len(win) >= 20))})
    print(f"{key} {var}: {time.time() - t0:.0f}s", flush=True)
    return {"rows": rows, "days": days, "counts": counts, "checks": chk}


def _window(ht, he, d, cfg2):
    hi = np.searchsorted(ht, d, "left")
    lo = np.searchsorted(ht, d - np.timedelta64(int(cfg2["eps_window_days"] * 86400e9), "ns"), "left")
    return he[lo:hi]


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--variants", choices=["eC", "e2"], default="eC", help="eC: the three Experiment C pool variants; "
                    "e2: every E2 pool variant (adds ETH/WBTC corr6h, corr7d and the four ETH/wstETH variants)")
    a = ap.parse_args()
    pvs = POOL_VARIANTS if a.variants == "eC" else e2_pool_variants()
    if a.split != "valid":
        raise SystemExit("validation-only: the test months are refused")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    cfg2 = reporting.load_config(ROOT / "experiments" / "configs" / "e2.yml")
    lam, gam = cfg2["headline"]["lambda"], cfg2["headline"]["gamma"]
    with ProcessPoolExecutor(a.workers) as ex:
        res = list(ex.map(one, [(k, v, cfg2, lam, gam) for k, v in pvs]))
    summ = pd.DataFrame([r for x in res for r in x["rows"]])
    days = pd.concat([x["days"] for x in res], ignore_index=True)
    counts = pd.DataFrame([x["counts"] for x in res])
    checks = pd.DataFrame([c for x in res for c in x["checks"]])
    summ.to_csv(OUT / "eps_observed_summary_valid.csv", index=False)
    days.to_csv(OUT / "eps_observed_daily_valid.csv", index=False)
    counts.to_csv(OUT / "eps_observed_counts_valid.csv", index=False)
    checks.to_csv(OUT / "eps_observed_checks_valid.csv", index=False)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, capture_output=True,
                           text=True).stdout.splitlines()
    keys = sorted({k for k, _ in pvs})
    inputs = ([CACHE / "aligned" / f"{k}.parquet" for k in keys] + [CACHE / "block_gas.parquet"]
              + [RESULTS / "e1" / f"depth_{k}.parquet" for k in keys]
              + sorted((ROOT / "data" / "data" / "binance").glob("*_1m.csv.gz")) + sorted((ROOT / "data" / "data" / "gas").glob("bq-results-*.csv"))
              + [ROOT / "experiments" / "configs" / "e2.yml"] + sorted((RESULTS / "eQ4").glob("eQ4_*valid.csv.gz"))
              + [Path(p) for p in (__file__, q4.__file__, q4s.__file__, eC.__file__, e2.__file__)])
    mf = {"experiment": "eps_observed", "split": "valid", "git_commit": head + ("+dirty" if dirty else ""), "uncommitted": dirty,
          "inputs": {Path(p).relative_to(ROOT).as_posix(): sha(Path(p)) for p in dict.fromkeys(inputs) if Path(p).exists()},
          "linked_from_main_checkout": ["cache", "results/e1", "results/e7", "data/data/gas", "data/data/lido", "data/data/bq"],
          "copied_from_main_checkout": "results/eC/eC_swaps_valid.csv.gz, results/eQ4/eQ4_eps_series_*_valid.csv.gz, "
                                       "results/eQ4/eQ4_crossblock_opportunities_valid.csv.gz (read-only copies, checks only)",
          "parameters": {"eps_window_days": cfg2["eps_window_days"], "eps_quantile": cfg2["eps_quantile"],
                         "update": "daily at 00:00 UTC, errors strictly before, rounded up to whole ppb",
                         "price_correcting": "sign(price - price_pre) = sign(pi_bench - price_pre) != 0",
                         "error": "|pi_hook / pi_bench - 1|", "seed": "last eps_window_days + 1 days of the train split",
                         "pool_variants": pvs, "e2_config_sha256": reporting.config_hash(cfg2)},
          "checks": json.loads(checks.to_json(orient="records")),
          "outputs": {p.name: sha(p) for p in sorted(OUT.glob("eps_observed_*_valid.csv"))}}
    (OUT / "manifest_valid.json").write_text(json.dumps(mf, indent=1, default=str))
    with pd.option_context("display.width", 250, "display.max_columns", 30, "display.max_rows", 200):
        print(counts.to_string())
        print(checks.to_string())
        print(summ[["pool", "variant", "calibration", "query_set", "n", "eps_median_bp", "coverage", "err_p95_bp"]].to_string())
    assert checks["ok"].all(), "a check failed"
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
