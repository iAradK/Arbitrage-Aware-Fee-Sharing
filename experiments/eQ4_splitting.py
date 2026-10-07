#!/usr/bin/env python3
"""Q4, parts 3 and 4: splitting under the fixed buffer (i) and the proportional buffer (ii), on the executed corrections
of E2's buffered rule (delta = eps_S rolling, k = 15, d = 1) in the validation months, rebuilt with their pool state by
Experiment B's harness (the test months are refused).

Rules (tau_hat = 3 gwei in K_hat; kappa_b = 180,214 gas x (base fee_b + tau_hat) x ETH + R, the hook's Eq. 9):
  fixed     kappa_b + delta, delta = eps_S at the opportunity (Experiment B's buffered rule);
  prop_abs  kappa_b, each transaction charged on [S_hat - eps_P |Delta_X|]^+, eps_P (absolute) at the opportunity;
  prop_rel  the same with eps_rel x P_hat x |Delta_X|.
With token Y as numeraire, [S_hat - eps |Delta_X|]^+ = [rho' . Delta]^+ with rho' = rho - direction x eps (abs) or
rho (1 - direction x eps_rel): the proportional buffer is the surplus at a reference shifted against the trade, so
Experiment B's plan() and execute() apply unchanged with rho replaced by rho'. eps_S and eps_P come from the buffered
replay's own candidates (eQ4_prop_buffer.eps_series, E2's rolling procedure).

3a. Within one block, block scope V1: the unsplit correction (Experiment B's N = 1 plan under the rule) split into
    l = 2..cap transactions of its first block, cap = floor(gas_limit / 185,180) (Experiment A), two families (equal
    inputs; the rule's cheapest pieces of kappa - 1 unit after the remainder), each piece a transaction through
    ScopedHookReference(accumulation="tx_clip", buffer=...) with integer deltas (cumulative amounts rounded, then
    differenced, so the pieces sum to the whole). Survival = split total / unsplit charge.
3b. Experiment A's optimal split under the transaction scope with the block cap (eA_followup.saving_path / best_l),
    on the buffered surplus of the unsplit correction, at the start values.
4.  Experiment B's cross-block harness, N = 2, 5, 75, block V1 (one transaction per block), dynamic and constant
    modes; surviving shares sum-weighted and mean of per-correction ratios.
Check: the fixed rule with Experiment B's tau_hat (0.05 gwei) reproduces eB_opportunities_valid.csv.gz (buffered,
kappa = basefee) exactly.

  python experiments/eQ4_splitting.py
"""
from __future__ import annotations

import argparse
import json
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
import eA_followup as fu  # noqa: E402
import eA_strategic_split as eA  # noqa: E402
import eB_cross_block_split as eB  # noqa: E402
import eQ4_prop_buffer as q4  # noqa: E402
from common import cpmm, data_io, fixedpoint as fp, minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS, RESULTS  # noqa: E402

OUT = RESULTS / "eQ4"
WAD = fp.WAD
RULE = ("buffered_1eps", 15, 1)
RULES = {"fixed": "none", "prop_abs": "abs", "prop_rel": "rel"}
NS = (1, 2, 5, 75)
TAU_Q4_WEI = q4.TAU_Q4_WEI


def replay(key, variant, split, cfg2, lam, gam):
    """eB.replay for the buffered rule, also returning the simulate result and the seed (for eps_P)."""
    mech, k, d = RULE
    C_all = e2.make_configs(cfg2)
    j = C_all.index[(C_all["mech"] == mech) & (C_all["lam"] == lam) & (C_all["gamma"] == gam)][0]
    C = C_all.loc[[0, j]].reset_index(drop=True)
    R_usd = mg.r_regimes_usd(key, variant, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
    seed = q4.price_seed(key, variant, R_usd, d, k, e2.PRIOR[split], cfg2)
    g = mg.build_grid(key, split, variant, lags=(0, d))
    rec = []
    orig = cpmm.full_correction_net

    def recorder(x, y, fee, pi):
        rec.append((np.array(x, dtype=float), np.array(y, dtype=float)))
        return orig(x, y, fee, pi)

    cpmm.full_correction_net = recorder
    try:
        r = e2.simulate(g, POOLS[key], C, cfg2, R_usd, np.nan, d, k, seed[["t", "err_usd", "err_signed_usd"]])
    finally:
        cpmm.full_correction_net = orig
    es = q4.eps_series(r, g, seed, k, d, cfg2)
    act = np.flatnonzero(es["act"].to_numpy())
    assert len(rec) == len(act)
    eps_ref = eA.rolling_eps(r, g, seed, k, d, cfg2)
    assert np.array_equal(np.nan_to_num(es["eps_S"].to_numpy(), nan=-1), np.nan_to_num(eps_ref, nan=-1))
    steps = pd.DataFrame({"i": act, "x": [x[1] for x, _ in rec], "y": [y[1] for _, y in rec]})
    steps["executed"] = r["step_exec"][act, 1]
    steps["r_e2_usd"] = r["step_protection"][act, 1].astype(float)
    for c in ("eps_S", "eps_P_abs", "eps_P_rel"):
        steps[c] = es[c].to_numpy()[act]
    return g, steps, r["table"].iloc[1], R_usd, C.iloc[1]


def shifted(rho, dirn, buffer, eps):
    if buffer == "abs":
        return rho - dirn * eps
    if buffer == "rel":
        return rho * (1 - dirn * eps)
    return rho


# ------------------------------------------------------------------ 3a: within one block, integer reference
def within_block(job: dict) -> dict:
    """All splits l = 2..cap of the unsplit correction into transactions of one block (block scope V1, the rule's
    buffer). Returns the unsplit charge and the deviation range of the split totals (numeraire base units)."""
    x, y, fee, dirn, n = job["x"], job["y"], job["fee"], job["dirn"], job["n"]
    s0, s1 = job["s0"], job["s1"]
    ref_w = int(round(job["rho0"] * s0 / s1 * WAD))
    k_w = int(job["kappa0"] / job["usd0"] * s0)
    buffer = job["buffer"]
    eps_w = int(np.ceil(job["eps"] * s0 / s1 * WAD)) if buffer == "abs" else (int(np.ceil(job["eps"] * WAD)) if buffer == "rel" else 0)

    def charge(cum_n, block):
        tr = cpmm.trade(x, y, fee, dirn, np.asarray(cum_n, dtype=float))
        c0 = [int(round(v * s0)) for v in np.atleast_1d(tr["d1"])]      # hook token0 = numeraire = pool token1
        c1 = [int(round(v * s1)) for v in np.atleast_1d(tr["d0"])]
        h = fp.ScopedHookReference(k_w, 0, job["lam_bps"], job["gam_bps"], scope="block", accumulation="tx_clip",
                                   buffer=buffer, eps_wad=eps_w)
        tot, p0, p1 = 0, 0, 0
        for j, (a0, a1) in enumerate(zip(c0, c1)):
            tot += h.swap("p", block, j, a0 - p0, a1 - p1, ref_w, settle_token0=True)
            p0, p1 = a0, a1
        return tot

    base = charge([n], 0)
    dmin = dmax = n_nz = n_splits = n_pieces = 0
    min_tot = base
    piece = job["kappa0"] - job["unit0"]
    rho_eff = shifted(job["rho0"], dirn, buffer, job["eps"])
    Sh_eff = float(np.maximum(rho_eff * cpmm.trade(x, y, fee, dirn, n)["d0"] + cpmm.trade(x, y, fee, dirn, n)["d1"], 0.0)) * job["usd0"]
    lmax_i = min(job["cap"], int(np.ceil(Sh_eff / piece))) if Sh_eff > 0 and piece > 0 else 1
    for fam, lmax in (("equal", job["cap"]), ("cheapest", lmax_i)):
        for l in range(2, lmax + 1):
            if fam == "equal":
                cum = n * np.arange(1, l + 1) / l
            else:
                rem = Sh_eff - (l - 1) * piece
                if rem <= 0:
                    break
                targets = (rem + piece * np.arange(l - 1)) / job["usd0"]
                cum = np.append(cpmm.smallest_net_for_surplus(x, y, fee, dirn, n, rho_eff, targets), n)
                cum = np.maximum.accumulate(cum)
            tot = charge(cum, l)
            d = tot - base
            dmin, dmax, n_nz = min(dmin, d), max(dmax, d), n_nz + (d != 0)
            n_splits, n_pieces = n_splits + 1, n_pieces + l
            min_tot = min(min_tot, tot)
    return {"charge_1_units": base, "min_total_units": min_tot, "dev_min_units": dmin, "dev_max_units": dmax,
            "n_nonzero_dev": n_nz, "n_splits": n_splits, "n_pieces": n_pieces}


# ------------------------------------------------------------------ one pool
def run(key, variant, split, cfg2, gas, Fint, lam, gam, blocks_all, gl, eAgas, rules_tau):
    t0 = time.time()
    mech, k, d = RULE
    g, steps, tab, R_usd, cfg_row = replay(key, variant, split, cfg2, lam, gam)
    pool = POOLS[key]
    gs = pd.DatetimeIndex(g["t"]).tz_convert("UTC").tz_localize(None).as_unit("ns")
    gmin = gs[0]
    ex = steps[steps["executed"]].reset_index(drop=True)
    bts = blocks_all["ts"]
    Nmax, G = max(NS), cfg2["grid_points"]
    gas_u = cfg2["gas_units"] + cfg2["hook_overhead_gas"]
    tol = 1e-9 * max(1.0, R_usd)
    pref0, prefd = g["p_ref_0"].to_numpy(), g[f"p_ref_{d}"].to_numpy()
    usd_g, eth_g = g["usd_per_num"].to_numpy(), g["eth_in_num"].to_numpy()
    rows, jobs, fa, dropped = [], [], [], 0
    for o_ in ex.itertuples():
        t = gs[o_.i]
        bi = int(np.searchsorted(bts, t.value, "left"))
        if bi + Nmax >= len(bts):
            dropped += 1
            continue
        bidx = np.arange(bi, bi + Nmax + 1)
        mins = ((pd.DatetimeIndex(bts[bidx]).floor("min") - gmin) // pd.Timedelta(minutes=1)).to_numpy()
        if mins[-1] >= len(g) or mins[0] < 0:
            dropped += 1
            continue
        mb = mins[:-1]
        pi, rho = pref0[mb], prefd[mb]
        usd, ethusd = usd_g[mb], eth_g[mb] * usd_g[mb]
        if not (np.isfinite(pi).all() and np.isfinite(rho).all() and np.isfinite(usd).all()):
            dropped += 1
            continue
        bf, gp = blocks_all["base_fee"][bidx[:-1]], blocks_all["gas_price"][bidx[:-1]]
        dt = np.diff(bts[bidx]).astype(float) / 1e9
        dirn, n0 = cpmm.full_correction_net(o_.x, o_.y, pool.fee, pi[0])
        p_pool0 = o_.y / o_.x
        trf = cpmm.trade(o_.x, o_.y, pool.fee, dirn, n0)
        S_full = float(np.maximum(pi[0] * trf["d0"] + trf["d1"], 0.0)) * usd[0]
        C0 = gas["first"] * gp[0] * 1e-18 * ethusd[0]
        bfeas = bool(n0 > 0 and S_full - C0 >= R_usd - tol)
        cap = int(gl[int(blocks_all["block"][bi])] // eAgas["extra_tx_gas"])
        for rule, (buffer, tau) in rules_tau.items():
            delta = (o_.eps_S if np.isfinite(o_.eps_S) else 0.0) * float(cfg_row["dmult"]) if buffer == "none" else 0.0
            eps = {"abs": o_.eps_P_abs, "rel": o_.eps_P_rel}.get(buffer, 0.0)
            kappa = gas_u * (bf + tau) * 1e-18 * ethusd + R_usd + delta
            rho_e = shifted(rho, int(dirn), buffer, eps)
            blocks = {"pi": pi, "rho": rho_e, "usd": usd, "ethusd": ethusd, "kappa": kappa, "gp": gp}
            o = {"x": o_.x, "y": o_.y, "fee": pool.fee, "dirn": int(dirn), "n0": float(n0), "pi0": pi[0], "rho0": rho_e[0],
                 "usd0": usd[0], "ethusd0": ethusd[0], "kappa0": kappa[0], "gp0": gp[0],
                 "unit0": float(eA.unit_usd(key, usd[0])), "C0": C0, "cext0": gas["extra"] * gp[0] * 1e-18 * ethusd[0]}
            base = {"pool": key, "variant": variant, "rule": rule, "tau_hat_wei": tau, "t": t, "block0": int(blocks_all["block"][bi]),
                    "baseline_feasible": bfeas, "kappa0_usd": kappa[0], "delta_usd": delta, "eps": eps, "R_usd": R_usd}
            plans = {N: (eB.plan(o, N, G, lam, gam, R_usd, tol) if n0 > 0 else None) for N in NS}
            p1 = plans[1]
            for mode in ("dynamic", "constant"):
                e1 = eB.execute(o, p1, blocks, mode, Fint, gas, lam, gam) if p1 else None
                for N in NS:
                    pN = plans[N]
                    eN = e1 if N == 1 else (eB.execute(o, pN, blocks, mode, Fint, gas, lam, gam) if pN else None)
                    rows.append({**base, "mode": mode, "N": N, "executed_1": p1 is not None, "executed_N": pN is not None,
                                 "l_star": pN["l"] if pN else 0, "size_frac": (pN["j"] + 1) / G if pN else 0.0,
                                 "charge_1": e1["charge"] if e1 else 0.0, "charge_N": eN["charge"] if eN else 0.0,
                                 "payoff_1": e1["payoff"] if e1 else 0.0, "payoff_N": eN["payoff"] if eN else 0.0,
                                 "planned_N": pN["planned"] if pN else np.nan, "gas_N": eN["cost"] if eN else 0.0,
                                 "n_pieces_charged": eN["n_pieces_charged"] if eN else 0,
                                 "twe_1": eB.twe(p_pool0, e1["price_after"] if e1 else np.array([]), pi[:N], dt[:N]),
                                 "twe_N": eB.twe(p_pool0, eN["price_after"] if eN else np.array([]), pi[:N], dt[:N])})
            if p1 is not None and tau == TAU_Q4_WEI:
                # 3b: Experiment A's optimal split under the transaction scope with the block cap, start values
                ctx = eAgas["extra_tx_gas"] * gp[0] * 1e-18 * ethusd[0]
                st = eAgas["settle_gas"] * gp[0] * 1e-18 * ethusd[0]
                ch, sv = fu.saving_path(p1["Sh"], kappa[0], o["unit0"], ctx, st, Fint)
                lu, lc = fu.best_l(sv, None), fu.best_l(sv, cap)
                fa.append({"pool": key, "variant": variant, "rule": rule, "t": t, "cap": cap, "B_usd": p1["Sh"], "kappa0_usd": kappa[0],
                           "F_a_usd": float(ch[0]) / WAD, "l_star_uncapped": lu, "charge_uncapped_usd": float(ch[lu - 1]) / WAD,
                           "l_star_cap": lc, "charge_cap_usd": float(ch[lc - 1]) / WAD})
                # 3a job
                jobs.append({"pool": key, "variant": variant, "rule": rule, "t": t, "x": o_.x, "y": o_.y, "fee": pool.fee,
                             "dirn": int(dirn), "n": p1["n"], "s0": 10 ** pool.dec1, "s1": 10 ** pool.dec0, "rho0": rho[0],
                             "kappa0": kappa[0], "usd0": usd[0], "unit0": o["unit0"], "buffer": buffer, "eps": eps, "cap": cap,
                             "lam_bps": int(round(lam * 1e4)), "gam_bps": int(round(gam * 1e4)), "charge_1_usd": p1 and float(Fint(fp.to_wad(p1["Sh"]), fp.to_wad(kappa[0]))) / WAD})
    info = {"pool": key, "variant": variant, "R_usd": R_usd, "n_opportunities": len(ex) - dropped, "n_dropped": dropped,
            "e2_protection_recomputed": float(tab["protection_usd"]), "e2_n_executed_recomputed": int(tab["n_executed"])}
    print(f"{key} {variant}: {info['n_opportunities']} opportunities in {time.time() - t0:.0f}s", flush=True)
    return pd.DataFrame(rows), pd.DataFrame(fa), jobs, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    a = ap.parse_args()
    if a.split != "valid":
        raise SystemExit("Q4 is validation-only: the test months are refused")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    cfg2 = reporting.load_config(ROOT / "experiments" / "configs" / "e2.yml")
    gas_cfg = json.load(open(eB.GAS_CFG))
    gas = {"first": cfg2["gas_units"] + cfg2["hook_overhead_gas"], "extra": gas_cfg["additional_transaction_new_block_gas"]["uncharged"],
           "extra_charged": gas_cfg["additional_transaction_new_block_gas"]["charged"]}
    eAgas = eA.gas_constants()
    lam, gam = cfg2["headline"]["lambda"], cfg2["headline"]["gamma"]
    Fint = eA.Fint(lam, gam)
    tau_old = eA.tau_hat_wei()
    bg = pd.read_parquet(CACHE / "block_gas.parquet", columns=["block", "timestamp", "base_fee_wei", "gas_price_wei"]).sort_values("block")
    blocks_all = {"ts": pd.DatetimeIndex(bg["timestamp"]).tz_convert("UTC").as_unit("ns").asi8, "block": bg["block"].to_numpy(),
                  "base_fee": bg["base_fee_wei"].to_numpy(float), "gas_price": bg["gas_price_wei"].to_numpy(float)}
    blk = data_io.load_blocks()
    gl = dict(zip(blk["block"].astype(int), blk["gas_limit"].astype(float)))
    rules_tau = {"check_fixed_tau_eB": ("none", tau_old), **{r: (b, TAU_Q4_WEI) for r, b in RULES.items()}}
    per, fas, jobs, infos = [], [], [], []
    for key, var in eB.POOL_VARIANTS:
        df, fa, jb, info = run(key, var, "valid", cfg2, gas, Fint, lam, gam, blocks_all, gl, eAgas, rules_tau)
        per.append(df)
        fas.append(fa)
        jobs += jb
        infos.append(info)
    per = pd.concat(per, ignore_index=True)
    fa = pd.concat(fas, ignore_index=True)

    # check against Experiment B's stored buffered rows (kappa = basefee): every column it shares
    ebo = pd.read_csv(RESULTS / "eB" / "eB_opportunities_valid.csv.gz", float_precision="round_trip")
    ebo = ebo[(ebo.rule == "buffered") & (ebo.kappa == "basefee")].reset_index(drop=True)
    mine = per[per.rule == "check_fixed_tau_eB"].reset_index(drop=True)
    assert len(mine) == len(ebo), (len(mine), len(ebo))
    cols = ["N", "mode", "executed_1", "executed_N", "l_star", "charge_1", "charge_N", "payoff_1", "payoff_N"]
    exact = {c: bool((mine[c].to_numpy() == ebo[c].to_numpy()).all()) for c in cols}
    assert all(exact.values()), exact
    checks = [{"check": "fixed rule, tau_hat 0.05 gwei reproduces eB_opportunities_valid (buffered, basefee): " + ", ".join(cols),
               "ok": True, "n_rows": len(mine)}]
    checks += [{"check": "E2 buffered totals recomputed", **i} for i in infos]

    # 4. cross-block summary (Experiment B's summarize, per rule)
    q4rows = per[per.rule != "check_fixed_tau_eB"].copy()
    q4rows["kappa"] = "basefee"
    summ = []
    for rule, x in q4rows.groupby("rule", sort=False):
        s = eB.summarize(x.drop(columns="rule").assign(rule="buffered"))
        s.insert(3, "q4_rule", rule)
        summ.append(s)
    summ = pd.concat(summ, ignore_index=True)
    # 3b. optimal split under the transaction scope with the block cap
    rows3b = []
    for (rule, key, var), x in fa.groupby(["rule", "pool", "variant"], sort=False):
        m = x["F_a_usd"] > 0
        rows3b.append({"rule": rule, "pool": key, "variant": var, "n_corrections": len(x), "n_charged": int(m.sum()),
                       "original_usd": x["F_a_usd"].sum(),
                       "surviving_sum_weighted_cap": x["charge_cap_usd"].sum() / x["F_a_usd"].sum() if m.any() else np.nan,
                       "surviving_mean_of_ratios_cap": float((x["charge_cap_usd"][m] / x["F_a_usd"][m]).mean()) if m.any() else np.nan,
                       "surviving_sum_weighted_uncapped": x["charge_uncapped_usd"].sum() / x["F_a_usd"].sum() if m.any() else np.nan,
                       "surviving_mean_of_ratios_uncapped": float((x["charge_uncapped_usd"][m] / x["F_a_usd"][m]).mean()) if m.any() else np.nan,
                       "l_star_median_charged": float(x.loc[m, "l_star_cap"].median()) if m.any() else np.nan,
                       "l_star_max": int(x["l_star_cap"].max()), "cap_min": int(x["cap"].min()), "cap_max": int(x["cap"].max())})
    # 3a. within-block check, in parallel
    order = np.argsort([-j["n"] for j in jobs])
    with ProcessPoolExecutor() as exr:
        res = list(exr.map(within_block, [jobs[i] for i in order], chunksize=4))
    back = [None] * len(jobs)
    for i, r_ in zip(order, res):
        back[i] = r_
    wb = pd.concat([pd.DataFrame([{k_: v for k_, v in j.items() if k_ in ("pool", "variant", "rule", "t", "buffer", "cap")} for j in jobs]),
                    pd.DataFrame(back)], axis=1)
    rows3a = []
    for (rule, key, var), x in wb.groupby(["rule", "pool", "variant"], sort=False):
        c1 = x["charge_1_units"].astype(float)
        m = c1 > 0
        mt = x["min_total_units"].astype(float)
        rows3a.append({"rule": rule, "pool": key, "variant": var, "n_corrections": len(x), "n_charged": int(m.sum()),
                       "survival_sum_weighted": mt.sum() / c1.sum() if m.any() else np.nan,
                       "survival_mean_of_ratios": float((mt[m] / c1[m]).mean()) if m.any() else np.nan,
                       "n_splits": int(x["n_splits"].sum()), "n_pieces": int(x["n_pieces"].sum()),
                       "deviation_units_min": int(x["dev_min_units"].min()), "deviation_units_max": int(x["dev_max_units"].max()),
                       "n_splits_nonzero_deviation": int(x["n_nonzero_dev"].sum()),
                       "n_corrections_with_deviation": int((x["n_nonzero_dev"] > 0).sum()),
                       "unit": f"numeraire base unit (1e-{POOLS[key].dec1})"})
    s3a, s3b = pd.DataFrame(rows3a), pd.DataFrame(rows3b)
    summ.to_csv(OUT / "eQ4_crossblock_summary_valid.csv", index=False)
    q4rows.to_csv(OUT / "eQ4_crossblock_opportunities_valid.csv.gz", index=False)
    s3a.to_csv(OUT / "eQ4_within_block_check_valid.csv", index=False)
    wb.drop(columns=["t"]).assign(t=wb["t"].astype(str)).to_csv(OUT / "eQ4_within_block_per_correction_valid.csv.gz", index=False)
    s3b.to_csv(OUT / "eQ4_tx_scope_optimal_split_valid.csv", index=False)
    fa.to_csv(OUT / "eQ4_tx_scope_optimal_split_per_correction_valid.csv.gz", index=False)
    pd.DataFrame(checks).to_csv(OUT / "eQ4_checks_splitting_valid.csv", index=False)
    outs = sorted(p for p in OUT.glob("eQ4_*_valid.csv*") if any(s in p.name for s in ("crossblock", "within_block", "tx_scope", "checks_splitting")))
    params = {"experiment": "eQ4 parts 3-4", "split": "valid", "rule": RULE, "tau_hat_wei": TAU_Q4_WEI, "rules": RULES,
              "N": list(NS), "gas_eB": gas, "gas_eA": eAgas, "lambda": lam, "gamma": gam}
    inputs = [RESULTS / "eB" / "eB_opportunities_valid.csv.gz", eB.GAS_CFG, CACHE / "block_gas.parquet",
              ROOT / "experiments" / "configs" / "e2.yml", Path(__file__).resolve(), Path(eB.__file__).resolve(),
              Path(q4.__file__).resolve(), Path(fp.__file__).resolve()] \
        + [CACHE / "aligned" / f"{k_}.parquet" for k_ in ("eth_usdc_005", "eth_wbtc_030")] \
        + [RESULTS / "e1" / f"depth_{k_}.parquet" for k_ in ("eth_usdc_005", "eth_wbtc_030")] \
        + sorted((ROOT / "data" / "data" / "binance").glob("*_1m.csv.gz")) + sorted((ROOT / "data" / "data" / "gas").glob("bq-results-*.csv"))
    reporting.write_manifest("eQ4", params, inputs, "valid", {"parameters": params, "checks": checks,
                                                              "outputs": {p.name: eA.sha(p) for p in outs}},
                             tag="splitting_valid", latest=False)
    for sd in ("tables", "figures"):
        p_ = OUT / sd
        if p_.exists() and not any(p_.iterdir()):
            p_.rmdir()
    with pd.option_context("display.width", 250, "display.max_columns", 40, "display.max_rows", 300):
        print(s3a.to_string())
        print(s3b.to_string())
        print(summ[["pool", "variant", "q4_rule", "mode", "N", "n_charged_1", "transfer_1_usd", "transfer_N_usd",
                    "surviving_share_sum_weighted", "surviving_share_mean_ratio", "violations_new_from_split"]].to_string())
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
