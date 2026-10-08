#!/usr/bin/env python3
"""E2: sequential endogenous replay (participation and price correction).

One step per minute. A persistent pool price is tracked per (mechanism, parameter) configuration; depth comes from
E1 (known strictly before the minute). At each step a searcher picks the correction q that maximises its
post-transfer payoff (smallest correction on ties) and executes if that payoff covers its reservation payoff R.
The hook charges a transfer computed from the reference lagged by `hook_lag_min`; payoffs use the benchmark bar.

v2 (DECISIONS V5, V6, V8): --cadence k lets the searcher act only every k minutes; eps_S for the buffered rules is either
"fixed" (one P95 fitted on the validation months, v1) or "rolling" (P95 of |S_hat - S| over the baseline's candidates in
a trailing window, strictly earlier, seeded with the preceding split); per-day aggregates are written for the day-block
bootstrap (e2_daily_<tag>.parquet). --median-only restricts the run to the median reservation regime.

v3 (DECISIONS W2): with `nohook_baseline: true` the run adds `baseline_nohook`, which pays no hook overhead, and writes
e2_nohook_steps_<tag>.parquet for experiments/e2_hook_gas.py.

  python experiments/e2_sequential_replay.py --split valid
  python experiments/e2_sequential_replay.py --freeze
  python experiments/e2_sequential_replay.py --split test --confirm-frozen
"""
from __future__ import annotations

import argparse
import itertools
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import cpmm, minutegrid as mg, reporting  # noqa: E402
from common.mechanism import argmax_smallest  # noqa: E402
from common.pools import CACHE, POOLS  # noqa: E402

CFG = ROOT / "experiments" / "configs" / "e2.yml"
PRIOR = {"valid": "train", "test": "valid"}
BASELINE, STATIC, UNCON, RULE = 0, 1, 2, 3
DYNFEE, MEVTAX = 4, 5          # E8 baselines (oracle-deviation dynamic fee, MEV tax under competitive bidding)
DYNFEE_MAX = 0.5               # cap on the dynamic fee (a fee this high already blocks every correction in the data)


def make_configs(cfg: dict) -> pd.DataFrame:
    """Mechanism list (DECISIONS D1). `dmult` scales eps_S(d) into the buffer delta. `gas` is the gas amount each
    configuration pays per action: every configuration pays the hook overhead except `baseline_nohook` (DECISIONS W2).
    Config 0 stays the baseline with hook gas, since eps_S and the candidate log are taken from it."""
    rows = [dict(mech="baseline", kind=BASELINE, lam=0.0, gamma=0.0, dmult=0.0, phi=0.0)]
    if cfg.get("nohook_baseline", False):
        rows.append(dict(mech="baseline_nohook", kind=BASELINE, lam=0.0, gamma=0.0, dmult=0.0, phi=0.0))
    for f in cfg["static_fee_rates"]:
        rows.append(dict(mech=f"static_{f * 100:.2f}pct", kind=STATIC, lam=0.0, gamma=0.0, dmult=0.0, phi=f))
    for lam in cfg["lambdas"]:
        rows.append(dict(mech="unconstrained", kind=UNCON, lam=lam, gamma=0.0, dmult=0.0, phi=0.0))
        rows.append(dict(mech="cap_gamma0", kind=RULE, lam=lam, gamma=0.0, dmult=0.0, phi=0.0))
    for name, dm in [("retained", 0.0), ("buffered_1eps", 1.0), ("buffered_2eps", 2.0)]:
        for lam, gam in itertools.product(cfg["lambdas"], cfg["gammas"]):
            rows.append(dict(mech=name, kind=RULE, lam=lam, gamma=gam, dmult=dm, phi=0.0))
    C = pd.DataFrame(rows)
    C["gas"] = np.where(C["mech"] == "baseline_nohook", cfg["gas_units"], cfg["gas_units"] + cfg["hook_overhead_gas"]).astype(float)
    return C


def simulate(g: pd.DataFrame, pool, C: pd.DataFrame, cfg: dict, R_usd: float, eps_usd: float, d: int,
             k: int = 1, eps_seed: pd.DataFrame | None = None, eps_rel: np.ndarray | None = None,
             eps_rel_signed: np.ndarray | None = None) -> dict:
    """Vectorised over configurations; sequential over minutes. The searcher acts every k minutes. If eps_seed is given
    (columns t, err_usd: baseline candidates of the preceding split), eps_S is rolling: at each step the P95 of |S_hat - S|
    over baseline candidates (seed plus this run's config 0) in the trailing eps_window_days, strictly earlier.
    Each configuration pays its own gas amount C["gas"]. If `baseline_nohook` is present, every action step of its path
    also logs feasibility with and without the hook overhead on that same state (DECISIONS W2).

    Optional columns of C (E8; absent in E2, which then runs exactly as frozen):
      gas_ch  gas paid when the configuration charges (r > 0), e.g. hook overhead with settlement; default `gas`
      ghat    gas amount in the hook's K_hat; default `gas`
      buf     "abs" (P95 of |S_hat - S|, E2) or "signed" (one-sided: P95 of S_hat - S, floored at 0); default "abs";
              "prop" (final rule): no delta; the hook's surplus is the buffered bracket
              [S_hat - dmult * eps_rel[t] * |net token1 change|]^+ (eps_rel: one value per minute, the daily calibration);
              "prop1s" (E8): the same with the one-sided eps_rel_signed (P95 of the direction-signed relative error)
      beta    DYNFEE: fee = pool fee + beta * |log(P_pool / P_hat)|, set from the pre-swap state; LPs keep the excess fee
      tax_t   MEVTAX: competing searchers bid their margin, so the tax takes t/(1+t) of S - C - R"""
    T, nC, G = len(g), len(C), cfg["grid_points"]
    fee = pool.fee
    fr = np.arange(1, G + 1) / G
    kind, lam, gam, phi = (C[c].to_numpy() for c in ("kind", "lam", "gamma", "phi"))
    dm = C["dmult"].to_numpy()
    gas_u = C["gas"].to_numpy(dtype=float)
    ext_gas = "gas_ch" in C or "ghat" in C
    gas_ch = C["gas_ch"].to_numpy(dtype=float) if "gas_ch" in C else gas_u
    gas_hat = C["ghat"].to_numpy(dtype=float) if "ghat" in C else gas_u
    signed = (C["buf"].to_numpy() == "signed") if "buf" in C else np.zeros(nC, dtype=bool)
    prop1s = (C["buf"].to_numpy() == "prop1s") if "buf" in C else np.zeros(nC, dtype=bool)
    prop = ((C["buf"].to_numpy() == "prop") if "buf" in C else np.zeros(nC, dtype=bool)) | prop1s
    assert not (prop & ~prop1s).any() or (eps_rel is not None and len(eps_rel) == len(g)), "buf = prop needs eps_rel per minute"
    assert not prop1s.any() or (eps_rel_signed is not None and len(eps_rel_signed) == len(g)), "buf = prop1s needs eps_rel_signed"
    beta = C["beta"].to_numpy(dtype=float) if "beta" in C else np.zeros(nC)
    tax_t = C["tax_t"].to_numpy(dtype=float) if "tax_t" in C else np.zeros(nC)
    has_dyn = bool((kind == DYNFEE).any())
    nh = np.flatnonzero(C["mech"].to_numpy() == "baseline_nohook")
    jn = int(nh[0]) if len(nh) else -1
    nh_act, nh_f150, nh_f180 = np.zeros(T, dtype=bool), np.zeros(T, dtype=bool), np.zeros(T, dtype=bool)
    nh_S, nh_C, nh_Ch = np.full(T, np.nan), np.full(T, np.nan), np.full(T, np.nan)   # USD, on the no-hook path
    pib_a, pih_a = g["p_ref_0"].to_numpy(), g[f"p_ref_{d}"].to_numpy()
    L_a, gas_a, en_a, usd_a = g["L"].to_numpy(), g["gas_wei"].to_numpy(), g["eth_in_num"].to_numpy(), g["usd_per_num"].to_numpy()
    p = np.full(nC, np.nan)
    pre_err = np.full((T, nC), np.nan, dtype=np.float32)
    post_err = np.full((T, nC), np.nan, dtype=np.float32)
    acc = {k: np.zeros(nC) for k in ("n_bf", "n_exec", "n_viol_q0", "n_capviol", "sum_r", "sum_pi", "sum_S", "sum_corr", "sum_lp_bp")}
    step_r = np.zeros((T, nC), dtype=np.float32)
    step_exec = np.zeros((T, nC), dtype=bool)          # executed corrections per step and configuration
    cS, cSh, cbf = np.zeros(T), np.zeros(T), np.zeros(T, dtype=bool)     # config 0 (baseline) candidates, USD
    tq = cfg["eps_quantile"]
    rolling = eps_seed is not None
    if rolling:
        W = np.timedelta64(int(cfg["eps_window_days"] * 86400e9), "ns")
        st = pd.DatetimeIndex(eps_seed["t"])
        st = st.tz_convert(None) if st.tz is not None else st           # an empty seed may come back tz-naive
        h_t = np.concatenate([st.values.astype("datetime64[ns]"), np.zeros(T, dtype="datetime64[ns]")])
        h_e = np.concatenate([eps_seed["err_usd"].to_numpy(dtype=float), np.zeros(T)])
        if signed.any():                                 # signed history S_hat - S of the same candidates (E8)
            h_s = np.concatenate([eps_seed["err_signed_usd"].to_numpy(dtype=float), np.zeros(T)])
        n_h, lo = len(eps_seed), 0
    tt = pd.DatetimeIndex(g["t"]).tz_convert(None).values
    day = pd.DatetimeIndex(g["t"]).floor("D")
    days, day_ix = np.unique(day.values, return_inverse=True)
    nd = len(days)
    dacc = {k_: np.zeros((nd, nC)) for k_ in ("n_steps", "sum_err", "n_bf", "n_exec", "n_viol_q0", "sum_r", "sum_pi", "sum_lp_bp")}
    eps_used = np.full(T, np.nan)
    for i in range(T):
        pib, pih, L = pib_a[i], pih_a[i], L_a[i]
        if not (np.isfinite(pib) and np.isfinite(pih) and np.isfinite(L) and np.isfinite(gas_a[i])):
            continue
        if np.isnan(p[0]):
            p[:] = pib                                   # initialise at the first reference price
        di = day_ix[i]
        if i % k:                                        # searcher only acts every k minutes; the error still accrues
            e_ = np.abs(np.log(p / pib))
            pre_err[i] = e_
            post_err[i] = e_
            dacc["n_steps"][di] += 1
            dacc["sum_err"][di] += e_
            continue
        if rolling:                                      # eps_S from strictly earlier baseline candidates
            while lo < n_h and h_t[lo] < tt[i] - W:
                lo += 1
            win = h_e[lo:n_h]
            eps_now = float(np.quantile(win, tq)) if len(win) >= 20 else (float(np.quantile(h_e[:n_h], tq)) if n_h else 0.0)
            if signed.any():
                ws = h_s[lo:n_h] if len(win) >= 20 else h_s[:n_h]
                eps_now_s = max(float(np.quantile(ws, tq)), 0.0) if len(ws) else 0.0
        else:
            eps_now = eps_usd
            eps_now_s = eps_usd
        eps_used[i] = eps_now
        usd = usd_a[i]
        Cst = mg.gas_cost_num(gas_a[i], en_a[i], gas_u)   # per configuration
        R = R_usd / usd
        K = Cst + R
        x, y = L / np.sqrt(p), L * np.sqrt(p)
        pre_err[i] = np.abs(np.log(p / pib))
        post_err[i] = pre_err[i]
        dacc["n_steps"][di] += 1
        dacc["sum_err"][di] += pre_err[i]
        dirn, n0 = cpmm.full_correction_net(x, y, fee, pib)
        t0 = cpmm.trade(x, y, fee, dirn, n0)
        S0 = np.maximum(pib * t0["d0"] + t0["d1"], 0.0)
        bf = (n0 > 0) & (S0 - Cst >= R)
        acc["n_bf"] += bf
        if jn >= 0:                                      # same state, with and without the hook overhead
            Ch = float(mg.gas_cost_num(gas_a[i], en_a[i], cfg["hook_overhead_gas"]))
            nh_act[i], nh_f150[i] = True, bf[jn]
            nh_f180[i] = (n0[jn] > 0) & (S0[jn] - Cst[jn] - Ch >= R)
            nh_S[i], nh_C[i], nh_Ch[i] = S0[jn] * usd, Cst[jn] * usd, Ch * usd
        cS[i], cbf[i] = S0[0] * usd, bf[0]
        cSh[i] = max(pih * t0["d0"][0] + t0["d1"][0], 0.0) * usd
        if rolling and bf[0]:
            h_t[n_h], h_e[n_h] = tt[i], abs(cSh[i] - cS[i])
            if signed.any():
                h_s[n_h] = cSh[i] - cS[i]
            n_h += 1
        dacc["n_bf"][di] += bf
        if not bf.any():
            continue
        ix = np.flatnonzero(bf)
        if has_dyn:                                      # the dynamic fee is set from the pre-swap state; feasibility above stays at the pool fee
            fee_c = np.where(kind == DYNFEE, np.minimum(fee + beta * np.abs(np.log(p / pih)), DYNFEE_MAX), fee)
            dirn_c, n0_c = cpmm.full_correction_net(x, y, fee_c, pib)
            fee_g = fee_c[ix, None]
        else:
            dirn_c, n0_c, fee_g = dirn, n0, fee
        n = n0_c[ix, None] * fr[None, :]
        tr = cpmm.trade(x[ix, None], y[ix, None], fee_g, dirn_c[ix, None], n)
        S = np.maximum(pib * tr["d0"] + tr["d1"], 0.0)
        Sh = np.maximum(pih * tr["d0"] + tr["d1"], 0.0)
        eps_c = np.where(signed[ix], eps_now_s, eps_now) if signed.any() else eps_now
        delta = (eps_c * dm[ix] / usd)[:, None]
        Kh = (mg.gas_cost_num(gas_a[i], en_a[i], gas_hat) + R) if ext_gas else K      # the hook's K_hat
        if prop.any():                                   # final rule: the buffer is inside the bracket, delta = 0
            pr = prop[ix, None]
            e_row = np.where(prop1s[ix], eps_rel_signed[i] if prop1s.any() else 0.0, eps_rel[i] if (prop & ~prop1s).any() else 0.0)
            Sh_r = np.where(pr, np.maximum(Sh - dm[ix, None] * e_row[:, None] * np.abs(tr["d1"]), 0.0), Sh)
            delta = np.where(pr, 0.0, delta)
        else:
            Sh_r = Sh
        rule = np.minimum(lam[ix, None] * Sh_r, (1 - gam[ix, None]) * np.maximum(Sh_r - Kh[ix, None] - delta, 0.0))
        k_ = kind[ix, None]
        r = np.where(k_ == STATIC, phi[ix, None] * tr["notional"], np.where(k_ == UNCON, lam[ix, None] * Sh, np.where(k_ == RULE, rule, 0.0)))
        if has_dyn:                                      # LPs keep the fee above the pool fee; S is reported net of the pool fee only
            # the fee is paid in the input token: token1 when buying token0, token0 (valued at the benchmark) when selling
            r_dyn = (fee_g - fee) * tr["gross_in"] * np.where(dirn_c[ix, None] < 0, pib, 1.0)
            dyn = k_ == DYNFEE
            r = np.where(dyn, r_dyn, r)
            S = np.where(dyn, S + r_dyn, S)
        if (kind == MEVTAX).any():
            tx = tax_t[ix, None]
            r = np.where(k_ == MEVTAX, tx / (1 + tx) * np.maximum(S - Cst[ix, None] - R, 0.0), r)
        if ext_gas:                                      # a charged swap pays the settlement gas (E8)
            Cg = np.where(r > 0, mg.gas_cost_num(gas_a[i], en_a[i], gas_ch)[ix, None], Cst[ix, None])
        else:
            Cg = Cst[ix, None]
        Pi = S - Cg - r
        tol = 1e-9 * max(1.0, R)
        feas = Pi >= R - tol
        anyf = feas.any(axis=1)
        j = argmax_smallest(np.where(feas, Pi, -np.inf))
        rr = np.arange(len(ix))
        full = G - 1
        acc["n_viol_q0"][ix] += Pi[:, full] < R - tol
        dacc["n_viol_q0"][di, ix] += Pi[:, full] < R - tol
        cap = (1 - gam[ix]) * np.maximum(S[:, full] - (Cg[:, full] + R if ext_gas else K[ix]), 0.0)
        acc["n_capviol"][ix] += (r[:, full] > cap + 1e-9 * np.maximum(1.0, cap)) & (kind[ix] != BASELINE)
        ex = ix[anyf]
        jj, rj = j[anyf], rr[anyf]
        acc["n_exec"][ex] += 1
        acc["sum_r"][ex] += r[rj, jj] * usd
        acc["sum_pi"][ex] += Pi[rj, jj] * usd
        acc["sum_S"][ex] += S[rj, jj] * usd
        step_r[i, ex] = r[rj, jj] * usd
        step_exec[i, ex] = True
        hodl = (x[ex] * pib + y[ex]) * usd
        acc["sum_lp_bp"][ex] += (r[rj, jj] - S[rj, jj]) * usd / hodl * 1e4
        dacc["n_exec"][di, ex] += 1
        dacc["sum_r"][di, ex] += r[rj, jj] * usd
        dacc["sum_pi"][di, ex] += Pi[rj, jj] * usd
        dacc["sum_lp_bp"][di, ex] += (r[rj, jj] - S[rj, jj]) * usd / hodl * 1e4
        p_new = tr["y"][rj, jj] / tr["x"][rj, jj]
        acc["sum_corr"][ex] += np.abs(np.log(p_new / p[ex]))
        p[ex] = p_new
        post_err[i] = np.abs(np.log(p / pib))
    v = np.isfinite(pre_err[:, 0])
    res = C[["mech", "lam", "gamma", "dmult", "phi"]].copy()
    bfc = np.maximum(acc["n_bf"], 1)
    res["n_steps"], res["n_baseline_feasible"], res["n_executed"] = int(v.sum()), acc["n_bf"].astype(int), acc["n_exec"].astype(int)
    res["execution_rate"] = acc["n_exec"] / bfc
    res["participation_violation_rate"] = acc["n_viol_q0"] / bfc
    res["cap_violation_rate"] = acc["n_capviol"] / bfc
    pe = pre_err[v].astype(float)
    res["etw_mean"], res["etw_p95"], res["etw_p99"] = pe.mean(0), np.quantile(pe, 0.95, axis=0), np.quantile(pe, 0.99, axis=0)
    res["etw_post_mean"] = post_err[v].astype(float).mean(0)
    res["protection_usd"], res["searcher_net_usd"], res["gross_surplus_usd"] = acc["sum_r"], acc["sum_pi"], acc["sum_S"]
    res["executed_correction"] = acc["sum_corr"]
    res["mean_lp_vs_hodl_bp"] = acc["sum_lp_bp"] / bfc      # per baseline-feasible step; 0 when not executed
    res["recapture_rate"] = np.where(acc["sum_S"] > 0, acc["sum_r"] / np.where(acc["sum_S"] > 0, acc["sum_S"], 1), np.nan)
    res["eps_S_used_median_usd"] = float(np.nanmedian(eps_used)) if np.isfinite(eps_used).any() else np.nan
    daily = [pd.DataFrame({"day": days, "cfg": j, **{k_: v[:, j] for k_, v in dacc.items()}}) for j in range(nC)]
    return {"table": res, "step_protection": step_r, "step_exec": step_exec, "etw": pe, "daily": pd.concat(daily, ignore_index=True),
            "cand": pd.DataFrame({"t": g["t"], "S_usd": cS, "S_hat_usd": cSh, "bf": cbf}),
            "nohook": pd.DataFrame({"t": g["t"], "gas_wei": gas_a, "act": nh_act, "S_usd": nh_S, "C_usd": nh_C,
                                    "C_hook_usd": nh_Ch, "f150": nh_f150, "f180": nh_f180}) if jn >= 0 else None}


def final_configs(cfg: dict, settlement: str | None = None) -> pd.DataFrame:
    """make_configs for the final rule (cfg["final"], experiments/configs/e2_final.yml): the buffered rules use the
    proportional net buffer (buf = "prop"); every hooked configuration pays hook_overhead_gas when uncharged and
    gas_units + the charged figure when it charges, and the hook's g_hat is that charged figure (DECISIONS I3)."""
    C = make_configs(cfg)
    st = settlement or cfg["final"]["settlement"]
    charged = cfg["hook_overhead_gas_charged"] if st == "charged" else cfg["hook_overhead_gas_charged_empty_vault"]
    nohook = (C["mech"] == "baseline_nohook").to_numpy()
    g_ch = float(cfg["gas_units"] + charged)
    C["gas_ch"] = np.where(nohook, C["gas"], g_ch)
    C["ghat"] = np.where(nohook, C["gas"], g_ch)
    C["buf"] = np.where(C["mech"].str.startswith("buffered").to_numpy(), "prop", "abs")
    return C


def final_eps_rel(cfg: dict, pool_key: str, variant: str, g: pd.DataFrame, source: str | None = None,
                  col: str = "eps_rel_ppb", reference: str | None = None) -> np.ndarray:
    """eps_rel in force at each minute of the grid: the daily whole-ppb observed-swap calibration of the pool variant
    (`source` relative to the run root, default cfg["final"]["eps_source"]; `col` the ppb column). A per-reference file
    (column `reference`, eps_reference_calibration.py) needs `reference`, e.g. "lag1" for the benchmark delayed 1 minute."""
    f = reporting.run_root() / (source or cfg["final"]["eps_source"])
    tab = pd.read_csv(f)
    tab = tab[(tab["pool"] == pool_key) & (tab["variant"] == variant)]
    if "reference" in tab:
        assert reference is not None, f"{f} is per reference: name one"
        tab = tab[tab["reference"] == reference]
    assert len(tab), f"no daily eps for {pool_key} {variant} in {f}"
    ppb = pd.Series(tab[col].to_numpy(np.int64), index=pd.DatetimeIndex(pd.to_datetime(tab["day"])).tz_localize(None))
    day = pd.DatetimeIndex(g["t"]).tz_convert(None).floor("D")
    v = ppb.reindex(day).to_numpy(dtype=float)
    assert np.isfinite(v).all(), f"days without a daily eps for {pool_key} {variant}"
    return v / 1e9


def eps_seed(pool_key: str, variant: str, R: float, d: int, k: int, split: str, cfg: dict) -> pd.DataFrame:
    """Baseline candidates (t, |S_hat - S| USD) of the last days of `split`, used to seed the rolling eps_S."""
    gv = mg.build_grid(pool_key, split, variant, lags=(0, d))
    gv = gv[gv["t"] >= gv["t"].iloc[-1] - pd.Timedelta(days=cfg["eps_window_days"] + 1)].reset_index(drop=True)
    C0 = make_configs(cfg).iloc[[0]].reset_index(drop=True)
    c = simulate(gv, POOLS[pool_key], C0, cfg, R, 0.0, d, k)["cand"]
    c = c[c["bf"]]
    # keep the tz-aware dtype even when no candidate is baseline-feasible (to_numpy() on an empty tz-aware column
    # yields a tz-naive index; seen for ETH/wstETH raw in the last days of March 2026, DECISIONS H7)
    return pd.DataFrame({"t": pd.DatetimeIndex(c["t"]).tz_convert("UTC"), "err_usd": (c["S_hat_usd"] - c["S_usd"]).abs().to_numpy(),
                         "err_signed_usd": (c["S_hat_usd"] - c["S_usd"]).to_numpy()})    # signed: E8 one-sided buffer


def eps_table(pool_key: str, variant: str, regimes: dict, d: int, cfg: dict, k: int = 1) -> dict:
    """eps_S(d) in USD per reservation regime: P95 of |S_hat - S| over baseline-feasible candidates of the baseline
    replay on the validation months (same state distribution the mechanisms face, DECISIONS D9)."""
    pool = POOLS[pool_key]
    gv = mg.build_grid(pool_key, cfg["eps_calibration_split"], variant, lags=(0, d))
    C0 = make_configs(cfg).iloc[[0]].reset_index(drop=True)
    out = {}
    for name, R in regimes.items():
        c = simulate(gv, pool, C0, cfg, R, 0.0, d, k)["cand"]
        m = c["bf"]
        out[name] = float((c["S_hat_usd"] - c["S_usd"]).abs()[m].quantile(cfg["eps_quantile"])) if m.any() else 0.0
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CFG))
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    ap.add_argument("--confirm-frozen", action="store_true")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--pools", default=None)
    ap.add_argument("--lag", type=int, default=None, help="override hook_lag_min (sensitivity)")
    ap.add_argument("--cadence", type=int, default=1, help="searcher acts every k minutes (v2)")
    ap.add_argument("--median-only", action="store_true", help="only the median reservation regime (v2 cadence runs)")
    a = ap.parse_args()
    cfg = reporting.load_config(a.config)
    out = reporting.out_dir("e2")
    if a.freeze:
        reporting.freeze("e2", cfg)
        print("config frozen:", reporting.config_hash(cfg)[:12])
        return
    reporting.guard_split("e2", a.split, a.confirm_frozen, cfg)
    d = a.lag if a.lag is not None else cfg["hook_lag_min"]
    gas_units = cfg["gas_units"] + cfg["hook_overhead_gas"]
    final = "final" in cfg
    if final and a.split != "valid":
        raise SystemExit("the final-rule config runs on the validation months only until it is frozen")
    if final:                                            # the config's gas must be the gas config's measured figures
        import json
        gj = json.loads((ROOT / "config" / "gas_block_scope.json").read_text())["hook_overhead_gas"]
        assert (cfg["hook_overhead_gas"], cfg["hook_overhead_gas_charged"], cfg["hook_overhead_gas_charged_empty_vault"]) == (
            gj["first_swap_of_block"], gj["charged_swap"], gj["charged_swap_empty_vault_sensitivity"]), "e2_final.yml gas != config/gas_block_scope.json"
    C = final_configs(cfg) if final else make_configs(cfg)
    keys = a.pools.split(",") if a.pools else cfg["pools"]
    tables, eps_rows, dailies, nohook_steps = [], [], [], []
    k = a.cadence
    for key in keys:
        pool = POOLS[key]
        for variant in cfg["variants"][key]:
            t0 = time.time()
            regimes = mg.r_regimes_usd(key, variant, cfg["gas_units"], cfg["r_quantiles"])   # observed swaps paid no hook overhead
            if a.median_only:
                regimes = {"median": regimes["median"]}
            eps = eps_table(key, variant, regimes, d, cfg, k) if cfg["eps_mode"] == "fixed" else {n_: np.nan for n_ in regimes}
            g = mg.build_grid(key, a.split, variant, lags=(0, d))
            er = final_eps_rel(cfg, key, variant, g, reference=f"lag{d}") if final else None   # the replay's reference
            for rname, R_usd in regimes.items():
                seed = eps_seed(key, variant, R_usd, d, k, PRIOR[a.split], cfg) if cfg["eps_mode"] == "rolling" else None
                r = simulate(g, pool, C, cfg, R_usd, eps[rname], d, k, seed, eps_rel=er)
                if r["nohook"] is not None:
                    nhk = r["nohook"][r["nohook"]["act"]].drop(columns="act")
                    nhk.insert(0, "R_usd", R_usd)
                    nhk.insert(0, "regime", rname)
                    nhk.insert(0, "variant", variant)
                    nhk.insert(0, "pool", key)
                    nohook_steps.append(nhk)
                dl = r["daily"].merge(C[["mech", "lam", "gamma", "dmult", "phi"]].reset_index().rename(columns={"index": "cfg"}), on="cfg")
                dl.insert(0, "regime", rname)
                dl.insert(0, "variant", variant)
                dl.insert(0, "pool", key)
                dailies.append(dl)
                t = r["table"]
                t.insert(0, "regime", rname)
                t.insert(0, "variant", variant)
                t.insert(0, "pool", key)
                eps_rep = eps[rname] if cfg["eps_mode"] == "fixed" else t["eps_S_used_median_usd"].iloc[0]
                t["R_usd"], t["eps_S_usd"], t["hook_lag_min"], t["cadence_min"], t["eps_mode"] = R_usd, eps_rep, d, k, cfg["eps_mode"]
                if final:
                    t["eps_rel_median_bp"] = float(np.median(er)) * 1e4
                tables.append(t)
                eps_rows.append({"pool": key, "variant": variant, "regime": rname, "R_usd": R_usd, "eps_S_usd": eps_rep, "eps_mode": cfg["eps_mode"],
                                 "lag_min": d, "cadence_min": k})
            print(f"{key} {variant} done in {time.time() - t0:.0f}s  R={ {k: round(v, 2) for k, v in regimes.items()} }", flush=True)
    summ = pd.concat(tables, ignore_index=True)
    tag = f"{a.split}" + (f"_lag{d}" if a.lag is not None else "") + (f"_k{k}" if k != 1 else "") + ("_med" if a.median_only else "")
    summ.to_parquet(out / f"e2_summary_{tag}.parquet", index=False)
    pd.concat(dailies, ignore_index=True).to_parquet(out / f"e2_daily_{tag}.parquet", index=False)
    if nohook_steps:                                     # input of experiments/e2_hook_gas.py
        pd.concat(nohook_steps, ignore_index=True).to_parquet(out / f"e2_nohook_steps_{tag}.parquet", index=False)
    reporting.write_table(pd.DataFrame(eps_rows), out / "tables" / f"e2_eps_and_R_{tag}")
    h = cfg["headline"]
    sel = summ[(summ["regime"] == "median") & (((summ["lam"] == h["lambda"]) | (summ["lam"] == 0)) & ((summ["gamma"] == h["gamma"]) | (summ["gamma"] == 0)))]
    cols = ["pool", "variant", "mech", "execution_rate", "participation_violation_rate", "cap_violation_rate", "etw_mean", "etw_p95",
            "protection_usd", "searcher_net_usd"]
    head = sel[cols].copy()
    reporting.write_table(head, out / "tables" / f"e2_headline_{tag}", {"execution_rate": "{:.3f}", "participation_violation_rate": "{:.3f}",
                          "cap_violation_rate": "{:.3f}", "etw_mean": "{:.2e}", "etw_p95": "{:.2e}", "protection_usd": "{:,.0f}", "searcher_net_usd": "{:,.0f}"})
    ins = [CACHE / "aligned" / f"{kk}.parquet" for kk in keys]
    extra = {"hook_lag_min": d, "cadence_min": k, "gas_units": gas_units, "eps_mode": cfg["eps_mode"]}
    if final:
        ins += [reporting.run_root() / cfg["final"]["eps_source"], ROOT / "config" / "gas_block_scope.json", Path(a.config).resolve()]
        extra.update({"config": Path(a.config).name, "final": cfg["final"], "hook_gas_uncharged": cfg["hook_overhead_gas"],
                      "hook_gas_charged": cfg["hook_overhead_gas_charged"], "results_run": reporting.RESULTS_RUN})
    reporting.write_manifest("e2", cfg, ins, a.split, extra, tag=tag)
    with pd.option_context("display.width", 250, "display.max_columns", 30, "display.max_rows", 200):
        print(head[head["variant"].isin(["raw", "corr24h"])].round(4).to_string(index=False))


if __name__ == "__main__":
    main()
