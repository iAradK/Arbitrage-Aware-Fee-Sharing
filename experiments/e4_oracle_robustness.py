#!/usr/bin/env python3
"""E4: robustness to oracle delay and required-payoff misspecification.

Candidates come from a persistent baseline replay (one full price-correcting candidate per searcher action, executed
when baseline-feasible), so they are identical across all oracle conditions. True surplus S values the candidate's
flows at the benchmark bar (last completed 1-minute bar). The hook estimate S_hat values the same flows at that series
lagged by d minutes. eps_K(eta) = |eta| K per candidate. Buffers tried: delta = 0, eps_S(d), eps_S(d) + eps_K(eta).

v2 (DECISIONS V5, V6):
- searcher cadence k: the searcher acts every k minutes (k in cadences_min). With k = 1 and d >= 1 the pool has already
  been corrected to the bar that the lagged reference shows, which hides most of the opportunity; k > 1 separates this
  replay artefact from the effect of the delay.
- eps_S(d) calibrations: "valid->test" (one P95 fitted on the validation months, v1), "<split> (in-sample)", and
  "rolling": P95 of |S_hat - S| over baseline-feasible candidates in a trailing window of eps_window_days, strictly
  before each candidate (seeded with the preceding split, so it is lookahead-free). The window is chosen on the
  validation months by coverage (share of candidates with |S_hat - S| <= eps closest to the nominal 95%).

  python experiments/e4_oracle_robustness.py --split valid
  python experiments/e4_oracle_robustness.py --freeze
  python experiments/e4_oracle_robustness.py --split test --confirm-frozen
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import cpmm, minutegrid as mg, plotstyle, reporting  # noqa: E402
from common.mechanism import transfer_est  # noqa: E402
from common.plotstyle import POOL_LABEL  # noqa: E402
from common.pools import CACHE, DATA, POOLS  # noqa: E402

CFG = ROOT / "experiments" / "configs" / "e4.yml"
PRIOR = {"valid": "train", "test": "valid"}


def baseline_candidates(pool_key: str, split: str, variant: str, lags, gas_units: float, R_usd: float, k: int = 1,
                        tail_days: float | None = None) -> pd.DataFrame:
    """Persistent baseline replay; the searcher acts every k minutes. Returns one row per baseline-feasible action with
    the full-correction candidate and all lagged references. tail_days keeps only the last days of the split (seed data)."""
    pool = POOLS[pool_key]
    g = mg.build_grid(pool_key, split, variant, lags=lags)
    if tail_days is not None:
        g = g[g["t"] >= g["t"].iloc[-1] - pd.Timedelta(days=tail_days)].reset_index(drop=True)
    T = len(g)
    fee = pool.fee
    pib = g["p_ref_0"].to_numpy()
    L, gas, en, usd = (g[c].to_numpy() for c in ("L", "gas_wei", "eth_in_num", "usd_per_num"))
    d0, d1, notl, bf, Cst = (np.zeros(T) for _ in range(5))
    p = np.nan
    for i in range(T):
        if not (np.isfinite(pib[i]) and np.isfinite(L[i]) and np.isfinite(gas[i])):
            continue
        if np.isnan(p):
            p = pib[i]
        if i % k:
            continue                                        # searcher only acts every k minutes
        x, y = L[i] / np.sqrt(p), L[i] * np.sqrt(p)
        dirn, n0 = cpmm.full_correction_net(x, y, fee, pib[i])
        if n0 <= 0:
            continue
        t = cpmm.trade(x, y, fee, dirn, n0)
        S0 = max(pib[i] * float(t["d0"]) + float(t["d1"]), 0.0)
        C = float(mg.gas_cost_num(gas[i], en[i], gas_units))
        R = R_usd / usd[i]
        Cst[i] = C
        if S0 - C >= R:
            bf[i] = 1
            d0[i], d1[i], notl[i] = float(t["d0"]), float(t["d1"]), float(t["notional"])
            p = float(t["y"]) / float(t["x"])
    out = g[["t"] + [f"p_ref_{d}" for d in lags]].copy()
    out["d0"], out["d1"], out["notional"], out["C"], out["usd"] = d0, d1, notl, Cst, usd
    out["gas_wei"], out["en"] = gas, en                     # actual gas price and ETH in numeraire (final rule)
    bg = pd.read_parquet(CACHE / "block_gas.parquet", columns=["timestamp", "base_fee_wei"])
    bts = pd.DatetimeIndex(bg["timestamp"]).tz_convert("UTC").as_unit("ns")
    out["base_fee_wei"] = mg.asof_before(bts, bg["base_fee_wei"].to_numpy(float), pd.DatetimeIndex(g["t"]))
    out["R"] = R_usd / usd
    out["bf"] = bf > 0
    out["S"] = np.maximum(out["p_ref_0"] * out["d0"] + out["d1"], 0.0)
    return out[out["bf"]].reset_index(drop=True)


def s_hat(c: pd.DataFrame, d: int, ref: np.ndarray | None = None) -> np.ndarray:
    pr = c[f"p_ref_{d}"].to_numpy() if ref is None else ref
    return np.maximum(pr * c["d0"].to_numpy() + c["d1"].to_numpy(), 0.0)


def abs_err_usd(c: pd.DataFrame, sh: np.ndarray) -> np.ndarray:
    return np.abs(sh - c["S"].to_numpy()) * c["usd"].to_numpy()


def eps_usd(c: pd.DataFrame, sh: np.ndarray, q: float) -> float:
    return float(np.quantile(abs_err_usd(c, sh), q)) if len(c) else 0.0


def rolling_eps(prior: pd.DataFrame, sh_prior: np.ndarray, cur: pd.DataFrame, sh_cur: np.ndarray, days: float, q: float,
                min_n: int = 20) -> np.ndarray:
    """Per candidate of `cur`: q-quantile of |S_hat - S| (USD) over candidates in the trailing `days`, strictly earlier
    (seeded with `prior`). Falls back to the expanding quantile when the window holds fewer than min_n candidates."""
    if len(cur) == 0:
        return np.zeros(0)
    s = pd.Series(np.concatenate([abs_err_usd(prior, sh_prior), abs_err_usd(cur, sh_cur)]),
                  index=pd.DatetimeIndex(np.concatenate([prior["t"].to_numpy(), cur["t"].to_numpy()])))
    order = np.argsort(s.index.values, kind="stable")
    s = s.iloc[order]
    roll = s.rolling(pd.Timedelta(days=days), closed="left", min_periods=min_n).quantile(q)
    expd = s.expanding().quantile(q).shift(1)
    v = roll.fillna(expd).fillna(0.0).to_numpy()
    back = np.empty_like(order)
    back[order] = np.arange(len(order))
    return v[back][len(prior):]


def evaluate(c: pd.DataFrame, sh: np.ndarray, eps, eta: float, delta_kind: str, lam: float, gam: float,
             fail_open: np.ndarray | None = None, final: dict | None = None) -> dict:
    """fail_open marks candidates whose reference is older than A_max: the hook applies r = 0 to them (Section 4.3).
    final (e4_final.yml): eps is eps_rel per candidate; see evaluate_final."""
    if final is not None:
        return evaluate_final(c, sh, eps, eta, delta_kind, lam, gam, fail_open, final)
    S, C, R, usd = (c[k].to_numpy() for k in ("S", "C", "R", "usd"))
    K = C + R
    Kh = (1 + eta) * K
    eS = np.asarray(eps, dtype=float) / usd
    eK = abs(eta) * K
    delta = {"0": np.zeros(len(c)), "eps_S": eS + 0 * S, "eps_S+eps_K": eS + eK}[delta_kind]
    r = transfer_est(sh, Kh, lam, gam, delta)
    if fail_open is not None:
        r = np.where(fail_open, 0.0, r)
    viol = (S - C - r) < R - 1e-9 * np.maximum(1.0, R)
    tot_S = S.sum()
    return {"n": len(c), "violation_rate": float(viol.mean()) if len(c) else np.nan,
            "recapture_nominal": float(r.sum() / tot_S) if tot_S > 0 else np.nan,
            "recapture_effective": float(r[~viol].sum() / tot_S) if tot_S > 0 else np.nan, "transfer_usd": float((r * usd).sum()),
            "eps_S_mean_usd": float(np.mean(eps)) if len(c) else np.nan}


def evaluate_final(c, sh, eps_rel, eta, delta_kind, lam, gam, fail_open, final) -> dict:
    """The final rule. The hook's surplus is the bracket [S_hat - eps_rel * |d1|]^+ ("0": eps_rel = 0); delta = 0, or
    |eta| K_hat0 for "eps_S+eps_K"; K_hat = (1 + eta) K_hat0 with K_hat0 = g_hat (base fee + tau_hat) ETH + R and g_hat
    the charged gas. A charged correction pays the charged gas at the actual gas price, an uncharged one the uncharged."""
    S, C, R, usd = (c[k].to_numpy() for k in ("S", "C", "R", "usd"))
    e = np.zeros(len(c)) if delta_kind == "0" else np.asarray(eps_rel, dtype=float) * np.ones(len(c))
    shb = np.maximum(np.maximum(sh, 0.0) - e * np.abs(c["d1"].to_numpy()), 0.0)
    gw, en, bf = c["gas_wei"].to_numpy(), c["en"].to_numpy(), c["base_fee_wei"].to_numpy()
    Kh0 = mg.gas_cost_num(bf + final["tau_hat_wei"], en, final["g_hat"]) + R
    delta = np.abs(eta) * Kh0 if delta_kind == "eps_S+eps_K" else np.zeros(len(c))
    r = transfer_est(shb, (1 + eta) * Kh0, lam, gam, delta)
    if fail_open is not None:
        r = np.where(fail_open, 0.0, r)
    Cg = np.where(r > 0, mg.gas_cost_num(gw, en, final["g_hat"]), C)
    viol = (S - Cg - r) < R - 1e-9 * np.maximum(1.0, R)
    tot_S = S.sum()
    return {"n": len(c), "violation_rate": float(viol.mean()) if len(c) else np.nan,
            "recapture_nominal": float(r.sum() / tot_S) if tot_S > 0 else np.nan,
            "recapture_effective": float(r[~viol].sum() / tot_S) if tot_S > 0 else np.nan, "transfer_usd": float((r * usd).sum()),
            "eps_rel_median_bp": float(np.median(e)) * 1e4 if len(c) else np.nan}


def final_eps(cfg: dict, key: str, variant: str, t) -> np.ndarray:
    """eps_rel in force at each candidate time: the daily whole-ppb observed-swap calibration."""
    tab = pd.read_csv(reporting.run_root() / cfg["final"]["eps_source"])
    tab = tab[(tab["pool"] == key) & (tab["variant"] == variant)]
    ppb = pd.Series(tab["eps_rel_ppb"].to_numpy(float), index=pd.DatetimeIndex(pd.to_datetime(tab["day"])).tz_localize(None))
    day = pd.DatetimeIndex(t)
    day = (day.tz_convert(None) if day.tz is not None else day).floor("D")
    v = ppb.reindex(day).to_numpy()
    assert np.isfinite(v).all(), f"days without a daily eps for {key} {variant}"
    return v / 1e9


def fitted_once_eps(key: str, variant: str, q: float) -> float:
    """One eps_rel fitted on the validation months: P95 of |P_hat / P - 1| over all observed price-correcting swaps."""
    sys.path.insert(0, str(ROOT / "experiments"))
    import eps_observed_calibration as epc
    va = epc.observed(key, variant, "valid")
    err = va.loc[va["correcting"], "err"].to_numpy()
    return float(np.ceil(np.quantile(err, q) * 1e9) / 1e9) if len(err) else 0.0


def pyth_ref(c: pd.DataFrame, pyth: pd.DataFrame, cut: pd.Timestamp):
    tt, pt = pd.DatetimeIndex(c["t"]), pd.DatetimeIndex(pyth["t"])
    ref = mg.asof_before(pt, pyth["price_usd"].to_numpy(), tt)
    idx = np.clip(pt.searchsorted(tt, side="left") - 1, 0, None)
    age = (tt - pt[idx]).total_seconds().to_numpy()
    ok = np.isfinite(ref) & np.asarray(tt < cut)
    return c[ok].reset_index(drop=True), ref[ok], age[ok]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CFG))
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    ap.add_argument("--confirm-frozen", action="store_true")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--pools", default=None)
    a = ap.parse_args()
    cfg = reporting.load_config(a.config)
    out = reporting.out_dir("e4")
    if a.freeze:
        reporting.freeze("e4", cfg)
        print("config frozen:", reporting.config_hash(cfg)[:12])
        return
    fin = None
    if "final" in cfg:                                   # not frozen yet: validation months only
        if a.split != "valid":
            raise SystemExit("the final-rule E4 config runs on the validation months only until it is frozen")
        fin = {"tau_hat_wei": float(cfg["final"]["tau_hat_wei"]), "g_hat": float(cfg["gas_units"] + cfg["hook_overhead_gas_charged"])}
    else:
        reporting.guard_split("e4", a.split, a.confirm_frozen, cfg)
    lags = tuple(cfg["delays_min"])
    keys = a.pools.split(",") if a.pools else cfg["pools"]
    q, W = cfg["eps_quantile"], cfg["eps_window_days"]
    seed_days = max(cfg["eps_window_grid_days"]) + 1
    rows, eps_rows, cov_rows = [], [], []
    pyth = None
    if cfg["pyth"]["enabled"]:
        pe = pd.read_csv(DATA / "data" / "pyth" / "onchain_ETH_USD_events.csv.gz", usecols=["block_time", "price_usd", "staleness_s"])
        pe["t"] = pd.to_datetime(pe["block_time"], utc=True).dt.as_unit("ns")
        pyth = pe.sort_values("t").reset_index(drop=True)
    cut = pd.Timestamp(cfg["pyth"]["last_event_cutoff"])
    fixed_name = "valid->" + a.split if a.split == "test" else "valid (in-sample)"
    for key in keys:
        for variant in cfg["variants"][key]:
            regimes = mg.r_regimes_usd(key, variant, cfg["gas_units"], cfg["r_quantiles"])
            R_usd = regimes[cfg["r_regime"]]
            gu = cfg["gas_units"] + cfg["hook_overhead_gas"]
            for k in cfg["cadences_min"]:
                ksuf = "" if k == 1 else f"_k{k}"
                cand = {s: baseline_candidates(key, s, variant, lags, gu, R_usd, k) for s in (["valid"] if a.split == "valid" else ["valid", "test"])}
                eps_once = fitted_once_eps(key, variant, q) if fin is not None else None
                prior = cand["valid"] if a.split == "test" else baseline_candidates(key, "train", variant, lags, gu, R_usd, k, tail_days=seed_days)
                c = cand[a.split]
                for d in lags:
                    scen = f"lag{d}{ksuf}"
                    eps_val = eps_usd(cand["valid"], s_hat(cand["valid"], d), q)
                    eps_ins = eps_usd(c, s_hat(c, d), q)
                    sh = s_hat(c, d)
                    eps_roll = rolling_eps(prior, s_hat(prior, d), c, sh, W, q)
                    eps_rows.append({"pool": key, "variant": variant, "scenario": scen, "cadence_min": k, "delay_min": d,
                                     "eps_S_valid_usd": eps_val, "eps_S_in_sample_usd": eps_ins,
                                     "eps_S_rolling_median_usd": float(np.median(eps_roll)) if len(c) else np.nan,
                                     "R_usd": R_usd, "n_cand_valid": len(cand["valid"]), "n_cand_eval": len(c)})
                    if a.split == "valid" and d > 0 and fin is None:   # window selection by coverage (validation only)
                        err = abs_err_usd(c, sh)
                        for Wg in cfg["eps_window_grid_days"]:
                            e = rolling_eps(prior, s_hat(prior, d), c, sh, Wg, q)
                            cov_rows.append({"pool": key, "variant": variant, "cadence_min": k, "delay_min": d, "window_days": Wg,
                                             "coverage": float((err <= e).mean()) if len(c) else np.nan, "n": len(c)})
                    calib = [(fixed_name, eps_val), ("rolling", eps_roll)]
                    if a.split == "test":
                        calib.append(("test (in-sample)", eps_ins))
                    if fin is not None:                  # eps_rel: the daily observed-swap calibration ("rolling") and one fit
                        calib = [(fixed_name, np.full(len(c), eps_once)), ("rolling", final_eps(cfg, key, variant, c["t"]))]
                    for cname, eps in calib:
                        for eta in cfg["etas"]:
                            for dk in ("0", "eps_S", "eps_S+eps_K"):
                                m = evaluate(c, sh, eps, eta, dk, cfg["lam"], cfg["gamma"], final=fin)
                                rows.append({"pool": key, "variant": variant, "scenario": scen, "cadence_min": k, "delay_min": d,
                                             "calibration": cname, "eta": eta, "delta": dk,
                                             "eps_S_usd": float(np.median(eps)) if np.ndim(eps) else eps, **m})
                # real on-chain Pyth reference (ETH/USDC only, until the events end)
                if pyth is not None and key == "eth_usdc_005" and variant == "raw":
                    cv, rv, _ = pyth_ref(cand["valid"], pyth, cut)
                    ce, re_, ae = pyth_ref(c, pyth, cut)
                    cp, rp, _ = pyth_ref(prior, pyth, cut)
                    eps_val = eps_usd(cv, s_hat(cv, 0, rv), q)
                    eps_roll = rolling_eps(cp, s_hat(cp, 0, rp), ce, s_hat(ce, 0, re_), W, q)
                    scen = f"pyth_onchain{ksuf}"
                    eps_rows.append({"pool": key, "variant": variant, "scenario": scen, "cadence_min": k, "delay_min": np.nan,
                                     "eps_S_valid_usd": eps_val, "eps_S_in_sample_usd": eps_usd(ce, s_hat(ce, 0, re_), q),
                                     "eps_S_rolling_median_usd": float(np.median(eps_roll)) if len(ce) else np.nan, "R_usd": R_usd,
                                     "n_cand_valid": len(cv), "n_cand_eval": len(ce), "median_ref_age_s": float(np.median(ae)) if len(ae) else np.nan})
                    cal_p = [(fixed_name, eps_val), ("rolling", eps_roll)]
                    if fin is not None:
                        cal_p = [(fixed_name, np.full(len(ce), eps_once)), ("rolling", final_eps(cfg, key, variant, ce["t"]))]
                    for cname, eps in cal_p:
                        for eta in cfg["etas"]:
                            for dk in ("0", "eps_S", "eps_S+eps_K"):
                                m = evaluate(ce, s_hat(ce, 0, re_), eps, eta, dk, cfg["lam"], cfg["gamma"], final=fin)
                                rows.append({"pool": key, "variant": variant, "scenario": scen, "cadence_min": k, "delay_min": np.nan,
                                             "calibration": cname, "eta": eta, "delta": dk,
                                             "eps_S_usd": float(np.median(eps)) if np.ndim(eps) else eps, **m})
                    # v3 (DECISIONS W5): staleness check A_max. A price older than A_max is invalid and the hook fails open
                    # (r = 0). "all" counts fail-open candidates with zero transfer; "valid_price" keeps only fresh ones.
                    _, _, ap_ = pyth_ref(prior, pyth, cut)
                    _, _, av_ = pyth_ref(cand["valid"], pyth, cut)
                    she = s_hat(ce, 0, re_)
                    for A in cfg["pyth"]["max_age_s_grid"]:
                        fresh, fresh_p, fresh_v = ae <= A, ap_ <= A, av_ <= A
                        cvA = cv[fresh_v].reset_index(drop=True)
                        eps_val_A = eps_usd(cvA, s_hat(cvA, 0, rv[fresh_v]), q)
                        cpA = cp[fresh_p].reset_index(drop=True)
                        ceA = ce[fresh].reset_index(drop=True)
                        eps_roll_A = np.zeros(len(ce))
                        eps_roll_A[fresh] = rolling_eps(cpA, s_hat(cpA, 0, rp[fresh_p]), ceA, she[fresh], W, q)
                        scenA = f"pyth_onchain_amax{A}{ksuf}"
                        eps_rows.append({"pool": key, "variant": variant, "scenario": scenA, "cadence_min": k, "delay_min": np.nan,
                                         "eps_S_valid_usd": eps_val_A, "eps_S_rolling_median_usd": float(np.median(eps_roll_A[fresh])) if fresh.any() else np.nan,
                                         "R_usd": R_usd, "n_cand_valid": int(fresh_v.sum()), "n_cand_eval": len(ce), "max_age_s": A,
                                         "fail_open_share": float(1 - fresh.mean()) if len(ce) else np.nan,
                                         "fail_open_share_valid_months": float(1 - fresh_v.mean()) if len(av_) else np.nan})
                        cal_A = [(fixed_name, np.full(len(ce), eps_val_A)), ("rolling", eps_roll_A)]
                        if fin is not None:
                            cal_A = [(fixed_name, np.full(len(ce), eps_once)), ("rolling", final_eps(cfg, key, variant, ce["t"]))]
                        for cname, eps in cal_A:
                            for eta in cfg["etas"]:
                                for dk in ("0", "eps_S", "eps_S+eps_K"):
                                    base = {"pool": key, "variant": variant, "scenario": scenA, "cadence_min": k, "delay_min": np.nan,
                                            "calibration": cname, "eta": eta, "delta": dk, "max_age_s": A,
                                            "fail_open_share": float(1 - fresh.mean()) if len(ce) else np.nan}
                                    m = evaluate(ce, she, eps, eta, dk, cfg["lam"], cfg["gamma"], fail_open=~fresh, final=fin)
                                    rows.append({**base, "conditioning": "all", "eps_S_usd": float(np.median(eps[fresh])) if fresh.any() else np.nan, **m})
                                    if fresh.any():
                                        m = evaluate(ceA, she[fresh], eps[fresh], eta, dk, cfg["lam"], cfg["gamma"], final=fin)
                                        rows.append({**base, "conditioning": "valid_price", "eps_S_usd": float(np.median(eps[fresh])), **m})
                print(key, variant, f"k={k}", "done", {s: len(v) for s, v in cand.items()}, "prior", len(prior), flush=True)

    summ = pd.DataFrame(rows)
    tag = a.split
    summ.to_csv(out / f"e4_summary_{tag}.csv", index=False)
    reporting.write_table(pd.DataFrame(eps_rows), out / "tables" / f"e4_eps_{tag}")
    if cov_rows:
        cov = pd.DataFrame(cov_rows)
        cov.to_csv(out / "tables" / "e4_window_coverage_valid.csv", index=False)
        # criterion: candidate-weighted mean |coverage - q| over pools, variants, cadences and delays d > 0 (small pools with
        # 29-350 candidates would otherwise dominate an unweighted mean)
        cov["gap"] = (cov["coverage"] - q).abs()
        sel = cov.groupby("window_days").apply(lambda g: pd.Series({"weighted_abs_coverage_gap": np.average(g["gap"], weights=g["n"]),
                                                                    "weighted_coverage": np.average(g["coverage"], weights=g["n"])})).reset_index()
        reporting.write_table(sel, out / "tables" / "e4_window_selection_valid", {"weighted_abs_coverage_gap": "{:.4f}", "weighted_coverage": "{:.4f}"})
        print(sel.to_string(index=False), "\nchosen by coverage:", int(sel.loc[sel["weighted_abs_coverage_gap"].idxmin(), "window_days"]),
              "| configured eps_window_days:", W)
    # sanity check: at delta = 0, eta = 0, k = 1 violations should rise with d
    chk = summ[(summ["delta"] == "0") & (summ["eta"] == 0) & summ["scenario"].str.startswith("lag") & (summ["cadence_min"] == 1)
               & (summ["calibration"] == "rolling")]
    notes = []
    for (pk, v), g in chk.groupby(["pool", "variant"]):
        g = g.sort_values("delay_min")
        mono = bool((np.diff(g["violation_rate"].to_numpy()) >= -1e-12).all())
        notes.append({"pool": pk, "variant": v, "monotone_in_d": mono, **{f"d{int(r.delay_min)}": round(r.violation_rate, 4) for r in g.itertuples()}})
    pd.DataFrame(notes).to_csv(out / "tables" / f"e4_sanity_delta0_{tag}.csv", index=False)
    figs(summ, out, tag, cfg)
    ins = [CACHE / "aligned" / f"{k}.parquet" for k in keys]
    if fin is not None:
        ins += [reporting.run_root() / cfg["final"]["eps_source"], ROOT / "config" / "gas_block_scope.json", Path(a.config).resolve()]
    reporting.write_manifest("e4", cfg, ins, a.split, {"final": cfg.get("final"), "results_run": reporting.RESULTS_RUN})
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(pd.DataFrame(notes).to_string(index=False))
        s = summ[(summ["eta"] == 0) & (summ["delay_min"].isin([0, 5, 60])) & (summ["calibration"] == "rolling") & (summ["pool"] == "eth_usdc_005")]
        print(s[["pool", "variant", "cadence_min", "delay_min", "delta", "n", "violation_rate", "recapture_nominal", "recapture_effective", "eps_S_usd"]].round(4).to_string(index=False))


def figs(summ: pd.DataFrame, out: Path, tag: str, cfg: dict):
    plotstyle.apply()
    labs = (("0", r"$\delta=0$"), ("eps_S", r"$\delta=\varepsilon_S$"), ("eps_S+eps_K", r"$\delta=\varepsilon_S+\varepsilon_K$"))
    s = summ[(summ["calibration"] == "rolling") & summ["scenario"].str.startswith("lag")]
    s1 = s[s["cadence_min"] == 1]
    pools = [(k, v) for k in cfg["pools"] for v in cfg["variants"][k] if ((s1["pool"] == k) & (s1["variant"] == v)).any()]
    fig, axs = plt.subplots(2, len(pools), figsize=(2.4 * len(pools), 4.2), squeeze=False)
    for j, (k, v) in enumerate(pools):
        d = s1[(s1["pool"] == k) & (s1["variant"] == v) & (s1["eta"] == 0)]
        for dk, lab in labs:
            g = d[d["delta"] == dk].sort_values("delay_min")
            axs[0, j].plot(g["delay_min"], g["violation_rate"] * 100, marker="o", ms=3, label=lab)
            axs[1, j].plot(g["delay_min"], g["recapture_effective"] * 100, marker="o", ms=3)
        axs[0, j].set_title(POOL_LABEL[k] + ("" if v == "raw" else f" ({v})"), fontsize=7)
        axs[1, j].set_xlabel("oracle delay $d$ (min)")
    axs[0, 0].set_ylabel("participation violations (%)")
    axs[1, 0].set_ylabel("effective recapture (%)")
    axs[0, 0].legend(frameon=False, fontsize=6)
    fig.tight_layout()
    reporting.savefig(fig, out / "figures" / f"e4_delay_{tag}")
    plt.close(fig)
    # eta axis at d = 5, k = 1
    e = s1[(s1["delay_min"] == 5)]
    fig, axs = plt.subplots(1, len(pools), figsize=(2.4 * len(pools), 2.6), squeeze=False)
    for j, (k, v) in enumerate(pools):
        d = e[(e["pool"] == k) & (e["variant"] == v)]
        for dk, lab in labs:
            g = d[d["delta"] == dk].sort_values("eta")
            axs[0, j].plot(g["eta"] * 100, g["violation_rate"] * 100, marker="o", ms=3, label=lab)
        axs[0, j].set_title(POOL_LABEL[k] + ("" if v == "raw" else f" ({v})"), fontsize=7)
        axs[0, j].set_xlabel(r"required-payoff error $\eta$ (%)")
    axs[0, 0].set_ylabel("participation violations (%)")
    axs[0, 0].legend(frameon=False, fontsize=6)
    fig.tight_layout()
    reporting.savefig(fig, out / "figures" / f"e4_eta_d5_{tag}")
    plt.close(fig)
    # cadence: ETH/USDC, eta = 0, violations and effective recapture against d for each searcher cadence k
    c = s[(s["pool"] == "eth_usdc_005") & (s["eta"] == 0)]
    fig, axs = plt.subplots(1, 2, figsize=(6.0, 2.6))
    for k, g in c.groupby("cadence_min"):
        for dk, ls in (("0", "-"), ("eps_S", "--")):
            h = g[g["delta"] == dk].sort_values("delay_min")
            lab = f"$k={k}$, " + (r"$\delta=0$" if dk == "0" else r"$\delta=\varepsilon_S$")
            axs[0].plot(h["delay_min"], h["violation_rate"] * 100, marker="o", ms=3, ls=ls, label=lab)
            axs[1].plot(h["delay_min"], h["recapture_effective"] * 100, marker="o", ms=3, ls=ls)
    axs[0].set_ylabel("participation violations (%)")
    axs[1].set_ylabel("effective recapture (%)")
    for ax in axs:
        ax.set_xlabel("oracle delay $d$ (min)")
    axs[0].legend(frameon=False, fontsize=6)
    fig.tight_layout()
    reporting.savefig(fig, out / "figures" / f"e4_cadence_{tag}")
    plt.close(fig)


if __name__ == "__main__":
    main()
