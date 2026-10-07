#!/usr/bin/env python3
"""Q5: calibration of the relative proportional buffer eps_rel x P_hat x |Delta_X| (validation months only; the test
months are refused). Harness: Q4 (experiments/eQ4_prop_buffer.py, eQ4_splitting.py), block scope V1, tau_hat = 3 gwei.

Variants of eps_rel (all from E2's rolling procedure: candidates of the baseline replay strictly earlier within 7 days,
seeded with the end of the preceding split; all history while fewer than 20):
  p95       rolling 95th percentile of |P_hat / P - 1| (current; reproduces Q4's eps_P_rel, asserted)
  p975      rolling 97.5th percentile
  p99       rolling 99th percentile
  dev_p95   deviation-conditioned: candidates bucketed by the pre-swap deviation |log(P_pool / P_hat)|, bucket edges at the
            50th, 80th and 95th percentiles of the training months' baseline-feasible candidates (k = 1, d = 1 baseline
            replay of the training months); the rolling 95th percentile within the bucket of the swap's own deviation
Extensions (--variants grid / dev; written under --tag, the base outputs are not touched):
  grid      p955, p96, p965, p97, p98, p985: further rolling quantiles
  dev       floor plus deviation term, with dev = |log(P_pool / P_hat)| at the opening of the block's scope (pool price
            before the block's first swap, the reference locked then) and m x fee the deviation threshold:
              max_c{c}_m{m}  eps = max(eps_p95, c * (dev - m * fee)^+)
              add_c{c}_m{m}  eps = eps_p95 + c * (dev - m * fee)^+
            In Experiment B's harness each block of a split recomputes dev from the pool price before its piece and that
            block's reference (eps_p95 stays the opportunity's).
Reported per variant:
  observed swaps (Experiment C's validation swaps): block V1 transfer with and without the 294 ETH block, tipped
    violations (a), spillover (b), and coverage of overestimation (S_hat - S <= buffer) on the standalone-feasible swaps
    (margin >= 0; 1,038 in ETH/USDC);
  Experiment B's harness, buffered rule (E2 k = 15, d = 1 corrections): unsplit transfer and surviving share at
    N = 2, 5, 75 (dynamic reference, sum-weighted and mean of ratios).
Checks: p95 reproduces Q4's eps_P_rel series, its observed-swap V1 charges and its cross-block prop_rel rows exactly.

  python experiments/eQ5_buffer_calibration.py
"""
from __future__ import annotations

import argparse
import copy
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
import eA_strategic_split as eA  # noqa: E402
import eB_cross_block_split as eB  # noqa: E402
import eC_netting_variants as q2  # noqa: E402
import eC_same_block_replay as eC  # noqa: E402
import eQ4_prop_buffer as q4  # noqa: E402
import eQ4_splitting as q4s  # noqa: E402
from common import cpmm, fixedpoint as fp, minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS, RESULTS  # noqa: E402

OUT = RESULTS / "eQ5"
WAD = fp.WAD
TAU = q4.TAU_Q4_WEI
D = 1
BASE = {"p95": ("q", 0.95), "p975": ("q", 0.975), "p99": ("q", 0.99), "dev_p95": ("bucket", 0.95)}
GRID = {f"p{str(q)[2:]}": ("q", q) for q in (0.955, 0.96, 0.965, 0.97, 0.98, 0.985)}
DEV = {f"{form}_c{c:g}_m{m:g}": (form, 0.95, c, m) for form in ("max", "add") for c in (0.25, 0.5, 1.0) for m in (2, 4, 6)}
GROUPS = {"base": BASE, "grid": GRID, "dev": DEV}
VARIANTS = dict(BASE)                                       # the variants of this run (set in main)
EDGE_Q = (0.50, 0.80, 0.95)
POOL_VARIANTS = q4.POOL_VARIANTS
NS = (1, 2, 5, 75)


def recorded_simulate(g, key, C, cfg2, R_usd, d, k, seed):
    """e2.simulate with the pre-action pool state of every configuration at every action step."""
    rec, orig = [], cpmm.full_correction_net

    def recorder(x, y, fee, pi):
        rec.append((np.array(x, dtype=float), np.array(y, dtype=float)))
        return orig(x, y, fee, pi)

    cpmm.full_correction_net = recorder
    try:
        r = e2.simulate(g, POOLS[key], C, cfg2, R_usd, np.nan if seed is not None else 0.0, d, k, seed)
    finally:
        cpmm.full_correction_net = orig
    ok = np.isfinite(g["p_ref_0"].to_numpy()) & np.isfinite(g[f"p_ref_{d}"].to_numpy()) & np.isfinite(g["L"].to_numpy()) \
        & np.isfinite(g["gas_wei"].to_numpy())
    act = np.flatnonzero(ok & (np.arange(len(g)) % k == 0))
    assert len(rec) == len(act)
    pool_p = np.full((len(g), len(C)), np.nan)
    pool_p[act] = np.array([y / x for x, y in rec])
    recorded_simulate.xy = {int(i): (x, y) for i, (x, y) in zip(act, rec)}   # exact reserves, as Q4's replay uses them
    return r, act, pool_p


def candidates(g, r, pool_p, d):
    """Baseline (config 0) candidates: time, bf flag, relative price error and pre-swap deviation |log(P_pool / P_hat)|."""
    P, Ph = g["p_ref_0"].to_numpy(), g[f"p_ref_{d}"].to_numpy()
    return pd.DataFrame({"t": q4.naive_ns(g["t"]), "bf": r["cand"]["bf"].to_numpy(bool), "err_rel": np.abs(Ph / P - 1),
                         "dev": np.abs(np.log(pool_p[:, 0] / Ph))})


def seed_candidates(key, variant, R_usd, d, k, split, cfg2):
    """As q4.price_seed (last eps_window_days + 1 days of `split`), with the candidates' deviation."""
    gv = mg.build_grid(key, split, variant, lags=(0, d))
    gv = gv[gv["t"] >= gv["t"].iloc[-1] - pd.Timedelta(days=cfg2["eps_window_days"] + 1)].reset_index(drop=True)
    C0 = e2.make_configs(cfg2).iloc[[0]].reset_index(drop=True)
    r, act, pp = recorded_simulate(gv, key, C0, cfg2, R_usd, d, k, None)
    c = candidates(gv, r, pp, d)
    s = c[c["bf"]].reset_index(drop=True)
    # the seed rows q4.price_seed would produce, plus the error columns of e2.eps_seed
    S, Sh = r["cand"]["S_usd"].to_numpy(), r["cand"]["S_hat_usd"].to_numpy()
    bf = c["bf"].to_numpy()
    s["err_usd"], s["err_signed_usd"] = np.abs(Sh - S)[bf], (Sh - S)[bf]
    s["t_utc"] = pd.DatetimeIndex(gv["t"]).tz_convert("UTC")[bf]
    return s


def bucket_edges(key, variant, R_usd, cfg2):
    """Deviation edges from the training months' baseline-feasible candidates (k = 1, d = 1 baseline replay)."""
    g = mg.build_grid(key, "train", variant, lags=(0, D))
    C0 = e2.make_configs(cfg2).iloc[[0]].reset_index(drop=True)
    r, act, pp = recorded_simulate(g, key, C0, cfg2, R_usd, D, 1, None)
    c = candidates(g, r, pp, D)
    dv = c.loc[c["bf"] & np.isfinite(c["dev"]), "dev"]
    return np.quantile(dv, EDGE_Q), len(dv)


def dev_eps(spec, floor, dev, fee):
    """A deviation variant's eps from its floor (the rolling quantile) and the scope-opening deviation."""
    form, _, c, m = spec
    term = c * np.maximum(np.nan_to_num(dev, nan=0.0) - m * fee, 0.0)
    return np.maximum(floor, term) if form == "max" else floor + term


def eps_variants(cand, seed, query_t, query_dev, edges, cfg2, fee, floors=None) -> dict:
    """eps_rel per variant at the query times (dev_p95: in the bucket of each query's own deviation; the deviation
    variants: at the query's own deviation). `floors`, if given, receives each deviation variant's floor."""
    out = {}
    st = q4.naive_ns(seed["t_utc"])
    cache = {}
    for name, spec in VARIANTS.items():
        kind, qv = spec[0], spec[1]
        c = copy.deepcopy(cfg2)
        c["eps_quantile"] = qv
        if kind in ("q", "max", "add"):
            if qv not in cache:
                cache[qv] = q4.rolling_q(cand["t"].to_numpy(), cand["err_rel"].to_numpy(), cand["bf"].to_numpy(),
                                         st, seed["err_rel"].to_numpy(), query_t, c)
            out[name] = cache[qv] if kind == "q" else dev_eps(spec, cache[qv], query_dev, fee)
            if kind != "q" and floors is not None:
                floors[name] = cache[qv]
            continue
        cb = np.digitize(np.nan_to_num(cand["dev"].to_numpy(), nan=0.0), edges)
        sb = np.digitize(np.nan_to_num(seed["dev"].to_numpy(), nan=0.0), edges)
        qb = np.digitize(np.nan_to_num(query_dev, nan=0.0), edges)
        v = np.full(len(query_t), np.nan)
        for b in range(len(edges) + 1):
            m = qb == b
            if m.any():
                v[m] = q4.rolling_q(cand["t"].to_numpy(), cand["err_rel"].to_numpy(), cand["bf"].to_numpy() & (cb == b),
                                    st[sb == b], seed["err_rel"].to_numpy()[sb == b], query_t[m], c)
        out[name] = v
    return out


# ------------------------------------------------------------------ part A: observed swaps
def observed(key, var, cfg2, edges, sw, q4m, q4ch, checks):
    pool = POOLS[key]
    gas_units = cfg2["gas_units"] + cfg2["hook_overhead_gas"]
    lam_bps, gam_bps = int(round(cfg2["headline"]["lambda"] * 1e4)), int(round(cfg2["headline"]["gamma"] * 1e4))
    R_usd = mg.r_regimes_usd(key, var, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
    # k = 1 baseline replay of the validation months (as Q4 part 1), with the candidates' deviation
    seed = seed_candidates(key, var, R_usd, D, 1, e2.PRIOR["valid"], cfg2)
    g = mg.build_grid(key, "valid", var, lags=(0, D))
    C0 = e2.make_configs(cfg2).iloc[[0]].reset_index(drop=True)
    seed_e2 = pd.DataFrame({"t": seed["t_utc"], "err_usd": seed["err_usd"], "err_signed_usd": seed["err_signed_usd"]})
    r, act, pp = recorded_simulate(g, key, C0, cfg2, R_usd, D, 1, seed_e2)
    cand = candidates(g, r, pp, D)
    al = eC.load_swaps(key, var, "valid")
    minute = q4.naive_ns(pd.DatetimeIndex(al["timestamp"]).floor("min"))
    # eps per minute grid at action steps, looked up at the swap's minute; dev_p95 needs the swap's own deviation
    tt = cand["t"].to_numpy()
    actmask = np.zeros(len(g), bool)
    actmask[act] = True
    pos = np.searchsorted(tt, minute)
    assert np.all(tt[pos] == minute) and actmask[pos].all()
    dev_sw = np.abs(np.log(al["price_pre"].to_numpy() / al["pi_hook"].to_numpy()))
    eps = eps_variants(cand, seed, minute, dev_sw, edges, cfg2, pool.fee)
    stored = pd.read_csv(RESULTS / "eQ4" / f"eQ4_eps_series_{key}_{var}_valid.csv.gz", float_precision="round_trip")
    st_eps = stored.set_index(q4.naive_ns(pd.DatetimeIndex(stored["t"])))["eps_P_rel"].reindex(minute).to_numpy()
    ok = bool(np.array_equal(eps["p95"], st_eps))
    assert ok, (key, var, "p95 does not reproduce Q4's eps_P_rel")
    checks.append({"pool": key, "variant": var, "check": "p95 eps at the observed swaps == Q4 eps_P_rel", "ok": ok})
    usd = al["usd_per_num"].to_numpy()
    S = -(al["pi_bench"] * al["amount0"] + al["amount1"]).to_numpy() * usd
    Sh = -(al["pi_hook"] * al["amount0"] + al["amount1"]).to_numpy() * usd
    dx = np.abs(al["amount0"].to_numpy())
    to_usd = usd / 10 ** pool.dec1
    s0 = 10 ** pool.dec1
    df = sw[(sw.pool == key) & (sw.variant == var) & (sw.setting == "buffered_eps_d1")].reset_index(drop=True)
    assert (df["block"].to_numpy() == al["block"].to_numpy()).all()
    feas = df["margin_usd"].to_numpy() >= 0
    eS = df["delta_usd"].to_numpy()
    rows, per = [], []
    cov_S = float(((Sh - S)[feas] <= eS[feas]).mean())
    kc = np.floor(R_usd / usd * s0)
    for name, e in eps.items():
        assert np.isfinite(e).all()
        res = q4.replay_rule(al, key, kc, np.ceil(e * WAD), "rel", int(TAU), gas_units, lam_bps, gam_bps)
        x = q4.swap_frame(df, res, to_usd)
        if name == "p95":                                       # Q4's stored prop_rel charges
            st = q4ch[(q4ch.pool == key) & (q4ch.variant == var) & (q4ch.rule == "prop_rel")].reset_index(drop=True)
            ok = all((x[c].to_numpy() == st[c].to_numpy()).all() for c in ("r_tx_usd", "r_V1_usd", "r_standalone_usd"))
            assert ok, (key, var, "p95 does not reproduce Q4's prop_rel charges")
            checks.append({"pool": key, "variant": var, "check": "p95 observed-swap charges (tx, V1, standalone) == Q4 prop_rel", "ok": ok})
        buf = e * al["pi_hook"].to_numpy() * dx * usd
        cov = float(((Sh - S)[feas] <= buf[feas]).mean())
        out = {"pool": key, "variant": var, "eps_variant": name, "eps_median": float(np.median(e)),
               "buffer_median_usd_feasible": float(np.median(buf[feas])), "n_feasible": int(feas.sum()),
               "coverage_overestimation_feasible": cov, "coverage_eps_S_feasible": cov_S}
        for sample, sel in [("all", np.ones(len(x), bool)), ("ex_event", x["block"].to_numpy() != q2.EVENT_BLOCK)]:
            m = q2.metrics(x[sel].reset_index(drop=True), ["V1"])[0]
            out[f"transfer_V1_usd_{sample}"] = m["transfer_usd"]
            if sample == "all":
                out.update({"a_tipped_V1": m["a_n_tipped_violation"], "b_spillover_n_V1": m["b_n_spillover"],
                            "b_spillover_usd_V1": m["b_spillover_usd"]})
        rows.append(out)
        print(key, var, name, f"V1 transfer {out['transfer_V1_usd_all']:.0f} tipped {out['a_tipped_V1']} cov {cov:.3f}", flush=True)
    # reference rows: Q4's fixed buffer and transaction scope (stored)
    for rule in ("fixed", "prop_rel"):
        for acc in ("V1", "tx"):
            m = q4m[(q4m.pool == key) & (q4m.variant == var) & (q4m.rule == rule) & (q4m.accumulation == acc)]
            if len(m):
                a_ = m[m["sample"] == "all swaps"].iloc[0]
                checks.append({"pool": key, "variant": var, "check": f"Q4 stored {rule} {acc}", "ok": True,
                               "transfer_usd": a_["transfer_usd"], "a_tipped": int(a_["a_n_tipped_violation"])})
    return rows


# ------------------------------------------------------------------ part B: Experiment B's harness, buffered rule
def execute_dev(name, floors, jj, x0, y0, rho, dirn, fee):
    """eB.execute with a deviation variant's eps recomputed at each block of the split: the pool price before the
    block's piece against that block's reference. The pieces (and so the price path) depend only on the plan and
    rho0, so a first pass gives the path and a second pass the charges."""
    spec, floor = VARIANTS[name], floors[name][jj]

    def run(o, p, blocks, mode, Fint, gas, lam, gam):
        first = eB.execute(o, p, blocks, mode, Fint, gas, lam, gam)
        l = p["l"]
        if l == 1:
            return first
        pre = np.concatenate([[y0 / x0], first["price_after"][:-1]])
        e_b = dev_eps(spec, floor, np.abs(np.log(pre / rho[:l])), fee)
        b2 = dict(blocks)
        b2["rho"] = np.concatenate([rho[:l] * (1 - dirn * e_b), blocks["rho"][l:]])
        b2["rho"][0] = blocks["rho"][0]                         # block 0 as planned (the same value)
        out = eB.execute(o, p, b2, mode, Fint, gas, lam, gam)
        assert np.array_equal(out["price_after"], first["price_after"])
        return out
    return run


def crossblock(key, var, cfg2, edges, q4cb, checks, blocks_all, gas, Fint, lam, gam):
    mech, k, d = q4s.RULE
    C_all = e2.make_configs(cfg2)
    j = C_all.index[(C_all["mech"] == mech) & (C_all["lam"] == lam) & (C_all["gamma"] == gam)][0]
    C = C_all.loc[[0, j]].reset_index(drop=True)
    R_usd = mg.r_regimes_usd(key, var, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
    seed = seed_candidates(key, var, R_usd, d, k, e2.PRIOR["valid"], cfg2)
    seed_e2 = pd.DataFrame({"t": seed["t_utc"], "err_usd": seed["err_usd"], "err_signed_usd": seed["err_signed_usd"]})
    g = mg.build_grid(key, "valid", var, lags=(0, d))
    r, act, pp = recorded_simulate(g, key, C, cfg2, R_usd, d, k, seed_e2)
    xy = recorded_simulate.xy
    cand = candidates(g, r, pp, d)
    pool = POOLS[key]
    gs = pd.DatetimeIndex(g["t"]).tz_convert("UTC").tz_localize(None).as_unit("ns")
    ex = act[r["step_exec"][act, 1]]
    Ph = g[f"p_ref_{d}"].to_numpy()
    dev_rule = np.abs(np.log(pp[ex, 1] / Ph[ex]))              # the rule path's pre-action deviation
    floors = {}
    eps = eps_variants(cand, seed, cand["t"].to_numpy()[ex], dev_rule, edges, cfg2, pool.fee, floors)
    bts, Nmax, G = blocks_all["ts"], max(NS), cfg2["grid_points"]
    gas_u = cfg2["gas_units"] + cfg2["hook_overhead_gas"]
    tol = 1e-9 * max(1.0, R_usd)
    pref0, prefd = g["p_ref_0"].to_numpy(), g[f"p_ref_{d}"].to_numpy()
    usd_g, eth_g = g["usd_per_num"].to_numpy(), g["eth_in_num"].to_numpy()
    rows = []
    for jj, i in enumerate(ex):
        t = gs[i]
        bi = int(np.searchsorted(bts, t.value, "left"))
        if bi + Nmax >= len(bts):
            continue
        bidx = np.arange(bi, bi + Nmax + 1)
        mins = ((pd.DatetimeIndex(bts[bidx]).floor("min") - gs[0]) // pd.Timedelta(minutes=1)).to_numpy()
        if mins[-1] >= len(g) or mins[0] < 0:
            continue
        mb = mins[:-1]
        pi, rho = pref0[mb], prefd[mb]
        usd, ethusd = usd_g[mb], eth_g[mb] * usd_g[mb]
        if not (np.isfinite(pi).all() and np.isfinite(rho).all() and np.isfinite(usd).all()):
            continue
        bf, gp = blocks_all["base_fee"][bidx[:-1]], blocks_all["gas_price"][bidx[:-1]]
        x0, y0 = float(xy[int(i)][0][1]), float(xy[int(i)][1][1])
        dirn, n0 = cpmm.full_correction_net(x0, y0, pool.fee, pi[0])
        C0 = gas["first"] * gp[0] * 1e-18 * ethusd[0]
        kappa = gas_u * (bf + TAU) * 1e-18 * ethusd + R_usd
        for name, e in eps.items():
            e0 = e[jj]
            if name in floors:                                  # eps at block 0 from the opportunity's own pool state
                e0 = float(dev_eps(VARIANTS[name], floors[name][jj], np.abs(np.log((y0 / x0) / rho[0])), pool.fee))
            rho_e = q4s.shifted(rho, int(dirn), "rel", e0)
            blocks = {"pi": pi, "rho": rho_e, "usd": usd, "ethusd": ethusd, "kappa": kappa, "gp": gp}
            o = {"x": x0, "y": y0, "fee": pool.fee, "dirn": int(dirn), "n0": float(n0), "pi0": pi[0], "rho0": rho_e[0],
                 "usd0": usd[0], "ethusd0": ethusd[0], "kappa0": kappa[0], "gp0": gp[0],
                 "unit0": float(eA.unit_usd(key, usd[0])), "C0": C0, "cext0": gas["extra"] * gp[0] * 1e-18 * ethusd[0]}
            plans = {N: (eB.plan(o, N, G, lam, gam, R_usd, tol) if n0 > 0 else None) for N in NS}
            run = execute_dev(name, floors, jj, x0, y0, rho, int(dirn), pool.fee) if name in floors else eB.execute
            e1 = run(o, plans[1], blocks, "dynamic", Fint, gas, lam, gam) if plans[1] else None
            for N in NS:
                pN = plans[N]
                eN = e1 if N == 1 else (run(o, pN, blocks, "dynamic", Fint, gas, lam, gam) if pN else None)
                rows.append({"pool": key, "variant": var, "eps_variant": name, "t": t, "N": N, "eps": e0,
                             "executed_1": plans[1] is not None, "executed_N": pN is not None, "l_star": pN["l"] if pN else 0,
                             "charge_1": e1["charge"] if e1 else 0.0, "charge_N": eN["charge"] if eN else 0.0})
    out = pd.DataFrame(rows)
    # check: p95 reproduces Q4's stored prop_rel cross-block rows (dynamic)
    st = q4cb[(q4cb.pool == key) & (q4cb.variant == var) & (q4cb.rule == "prop_rel") & (q4cb["mode"] == "dynamic")].reset_index(drop=True)
    mine = out[out.eps_variant == "p95"].reset_index(drop=True)
    ok = len(st) == len(mine) and all((mine[c].to_numpy() == st[c].to_numpy()).all() for c in ("N", "l_star", "charge_1", "charge_N"))
    assert ok, (key, var, "p95 does not reproduce Q4's cross-block prop_rel rows")
    checks.append({"pool": key, "variant": var, "check": "p95 cross-block rows (N, l*, charges) == Q4 prop_rel dynamic", "ok": ok,
                   "n_rows": len(mine)})
    summ = []
    for (name, N), x in out.groupby(["eps_variant", "N"], sort=False):
        c1, cN = x["charge_1"], x["charge_N"]
        m = c1 > 0
        summ.append({"pool": key, "variant": var, "eps_variant": name, "N": N, "n_opportunities": len(x), "n_charged_1": int(m.sum()),
                     "unsplit_transfer_usd": c1.sum(), "surviving_sum_weighted": cN.sum() / c1.sum() if c1.sum() > 0 else np.nan,
                     "surviving_mean_ratio": float((cN[m] / c1[m]).mean()) if m.any() else np.nan})
    print(key, var, "cross-block done", flush=True)
    return summ


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    ap.add_argument("--variants", default="base", help="comma-separated groups (base, grid, dev) or variant names; "
                                                       "p95 is always included (the Q4 checks)")
    ap.add_argument("--parts", default="observed,crossblock")
    ap.add_argument("--tag", default="", help="output tag (required unless the base run)")
    a = ap.parse_args()
    if a.split != "valid":
        raise SystemExit("Q5 is validation-only: the test months are refused")
    allv = {**BASE, **GRID, **DEV}
    sel = {"p95": BASE["p95"]}
    for tok in a.variants.split(","):
        sel.update(GROUPS[tok] if tok in GROUPS else {tok: allv[tok]})
    VARIANTS.clear()
    VARIANTS.update(sel)
    parts = set(a.parts.split(","))
    if (a.variants != "base" or parts != {"observed", "crossblock"}) and not a.tag:
        raise SystemExit("--tag is required for anything but the base run (the base outputs are not overwritten)")
    sfx = f"_{a.tag}" if a.tag else ""
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    cfg2 = reporting.load_config(ROOT / "experiments" / "configs" / "e2.yml")
    lam, gam = cfg2["headline"]["lambda"], cfg2["headline"]["gamma"]
    Fint = eA.Fint(lam, gam)
    gas_cfg = json.load(open(eB.GAS_CFG))
    gas = {"first": cfg2["gas_units"] + cfg2["hook_overhead_gas"], "extra": gas_cfg["additional_transaction_new_block_gas"]["uncharged"],
           "extra_charged": gas_cfg["additional_transaction_new_block_gas"]["charged"]}
    bg = pd.read_parquet(CACHE / "block_gas.parquet", columns=["block", "timestamp", "base_fee_wei", "gas_price_wei"]).sort_values("block")
    blocks_all = {"ts": pd.DatetimeIndex(bg["timestamp"]).tz_convert("UTC").as_unit("ns").asi8, "block": bg["block"].to_numpy(),
                  "base_fee": bg["base_fee_wei"].to_numpy(float), "gas_price": bg["gas_price_wei"].to_numpy(float)}
    sw = pd.read_csv(RESULTS / "eC" / "eC_swaps_valid.csv.gz", float_precision="round_trip")
    q4m = pd.read_csv(RESULTS / "eQ4" / "eQ4_swaps_metrics_valid.csv")
    q4ch = pd.read_csv(RESULTS / "eQ4" / "eQ4_swaps_charges_valid.csv.gz", float_precision="round_trip")
    q4cb = pd.read_csv(RESULTS / "eQ4" / "eQ4_crossblock_opportunities_valid.csv.gz", float_precision="round_trip")
    checks, obs, cb, edges_rows = [], [], [], []
    for key, var in POOL_VARIANTS:
        R_usd = mg.r_regimes_usd(key, var, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
        edges, n_train = bucket_edges(key, var, R_usd, cfg2)
        edges_rows.append({"pool": key, "variant": var, **{f"edge_q{q:g}": e for q, e in zip(EDGE_Q, edges)}, "n_train_candidates": n_train})
        print(key, var, "edges", edges, f"{time.time() - t0:.0f}s", flush=True)
        if "observed" in parts:
            obs += observed(key, var, cfg2, edges, sw, q4m, q4ch, checks)
        if "crossblock" in parts:
            cb += crossblock(key, var, cfg2, edges, q4cb, checks, blocks_all, gas, Fint, lam, gam)
    obs, cb = pd.DataFrame(obs), pd.DataFrame(cb)
    outs = []
    for nm, frame in [(f"eQ5_observed_swaps{sfx}_valid.csv", obs), (f"eQ5_crossblock{sfx}_valid.csv", cb),
                      (f"eQ5_bucket_edges{sfx}_train.csv", pd.DataFrame(edges_rows)), (f"eQ5_checks{sfx}_valid.csv", pd.DataFrame(checks))]:
        if len(frame):
            frame.to_csv(OUT / nm, index=False)
            outs.append(OUT / nm)
    params = {"experiment": "eQ5", "split": "valid", "tau_hat_wei": TAU, "variants": VARIANTS, "edge_quantiles": EDGE_Q,
              "variants_arg": a.variants, "parts": sorted(parts), "tag": a.tag,
              "scope": "block V1 (tx_clip)", "buffer": "rel", "eps_d": D, "N": list(NS), "cross_block_rule": q4s.RULE}
    inputs = [RESULTS / "eC" / "eC_swaps_valid.csv.gz", RESULTS / "eQ4" / "eQ4_swaps_metrics_valid.csv",
              RESULTS / "eQ4" / "eQ4_swaps_charges_valid.csv.gz", RESULTS / "eQ4" / "eQ4_crossblock_opportunities_valid.csv.gz",
              CACHE / "block_gas.parquet", eB.GAS_CFG, ROOT / "experiments" / "configs" / "e2.yml", Path(__file__).resolve(),
              Path(q4.__file__).resolve(), Path(q4s.__file__).resolve(), Path(eB.__file__).resolve(), Path(fp.__file__).resolve()] \
        + [CACHE / "aligned" / f"{k_}.parquet" for k_ in ("eth_usdc_005", "eth_wbtc_030")] \
        + sorted((ROOT / "data" / "data" / "binance").glob("*_1m.csv.gz"))
    reporting.write_manifest("eQ5", params, inputs, "valid", {"parameters": params, "checks": checks,
                                                              "outputs": {p.name: eA.sha(p) for p in outs}}, tag=f"{a.tag}_valid" if a.tag else "valid", latest=False)
    for sd in ("tables", "figures"):
        p_ = OUT / sd
        if p_.exists() and not any(p_.iterdir()):
            p_.rmdir()
    with pd.option_context("display.width", 250, "display.max_columns", 40):
        print(obs.to_string())
        print(cb.to_string())
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
