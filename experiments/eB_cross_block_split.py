#!/usr/bin/env python3
"""Experiment B: how much recovery survives when an arbitrager splits a correction across consecutive blocks, with
the hook's reference, the benchmark and kappa moving as they would. Validation months by default.

Opportunities. The executed corrections of two E2 replays (median R, lambda 0.75, gamma 0.02, as the E2 headline):
  ideal     retained margin, cadence k = 1, reference delay d = 0
  buffered  delta = eps_S (rolling, as E2), k = 15, d = 1
for ETH/USDC (raw) and ETH/WBTC (raw and 24-hour offset). The frozen e2_sequential_replay.simulate() is re-executed in
memory on [baseline, rule]; a recorder around cpmm.full_correction_net captures the rule path's pool state at every
action step (no file is modified), and the recomputed totals are asserted equal to the stored E2 summaries.

Blocks. Opportunity at grid minute t: blocks 0..N-1 are the first N chain blocks with timestamp >= t (cache/block_gas;
about 12 s apart). At block b the benchmark pi_b and the hook reference rho_b are the minute grid's lookahead-free values
at floor(block time) (p_ref_0 and p_ref_d, 1-minute bars: they change only at minute boundaries), and the hook's
kappa_b = 180,214 gas x (base fee_b + tau_hat) x ETH + R + delta (Eq. 9; delta = the rolling eps_S at t for the buffered
rule, fixed over the split). Each block is its own scope: piece b is charged F(S_hat_b; kappa_b) with S_hat_b its
surplus at rho_b (integer reference, USD in WAD).

Plan (start-time information only: the pool state, pi_0, the locked reference rho_0, kappa_0 and the gas price of block
0, all assumed to stay). The arbitrager picks the correction size (E2's grid of 200 fractions of the full correction
to pi_0) and l <= N pieces: the remainder S_hat - (l-1)(kappa_0 - 1 unit) in block 0 and l-1 pieces of kappa_0 - 1 unit
in blocks 1..l-1, which minimises the summed charge for given l (F is 0 up to kappa and concave above). Planned payoff:
S - C_0 - F(remainder) - (l-1) c_extra, with C_0 = 180,214 gas (E2's transaction) and c_extra the uncharged new-block
transaction of config/gas_block_scope.json; the plan executes if it reaches R. Ties go to the smaller correction, then
fewer pieces. N = 1 is the unsplit baseline.

Execution. The plan is fixed (input amounts per block) and valued ex post: true surplus at pi_b, charge at rho_b and
kappa_b, gas at each block's gas price, with 169,213 gas for an extra transaction that pays a charge. A second mode,
"constant", values the same plan at the start values (pi_0, rho_0, kappa_0, gas price 0) for every block: the bracket of
results/eA/eA_fu2_block_scope.csv, on the same opportunities.

Each opportunity is evaluated on its own (no other trader acts during the split; the next replay step is not
re-simulated). Opportunities whose 75-block horizon would leave the split's months are dropped for every N.

  python experiments/eB_cross_block_split.py                        # validation months
  python experiments/eB_cross_block_split.py --split test --confirm-frozen
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
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
from common import cpmm, fixedpoint as fp, minutegrid as mg, reporting  # noqa: E402
from common.mechanism import argmax_smallest  # noqa: E402
from common.pools import CACHE, POOLS, RESULTS  # noqa: E402

OUT = RESULTS / "eB"
# GAS_BLOCK_SCOPE_CONFIG overrides the path (e.g. to reproduce a result made with an earlier version of the file).
GAS_CFG = Path(os.environ.get("GAS_BLOCK_SCOPE_CONFIG", ROOT / "config" / "gas_block_scope.json"))
NS = (1, 2, 5, 75)
POOL_VARIANTS = [("eth_usdc_005", "raw"), ("eth_wbtc_030", "raw"), ("eth_wbtc_030", "corr24h")]
RULES = {"ideal": ("retained", 1, 0), "buffered": ("buffered_1eps", 15, 1)}   # mechanism, cadence k, delay d
BRACKET = RESULTS / "eA" / "eA_fu2_block_scope.csv"
KAPPA_CONV = ("basefee", "gasprice")   # primary: the hook's Eq. 9; sensitivity: E2's K_hat at the block's gas price
WAD = fp.WAD


# ------------------------------------------------------------------ replay with pool-state capture
def replay(pool_key, variant, mech, k, d, split, cfg2, lam, gam):
    """Frozen simulate() on [baseline, rule]; returns the grid, the rule's action steps with their pre-action pool
    state, executed flags and transfers, the rolling eps_S, R and the configuration table."""
    C_all = e2.make_configs(cfg2)
    j = C_all.index[(C_all["mech"] == mech) & (C_all["lam"] == lam) & (C_all["gamma"] == gam)][0]
    C = C_all.loc[[0, j]].reset_index(drop=True)
    R_usd = mg.r_regimes_usd(pool_key, variant, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
    seed = e2.eps_seed(pool_key, variant, R_usd, d, k, e2.PRIOR[split], cfg2)
    g = mg.build_grid(pool_key, split, variant, lags=(0, d))
    rec = []
    orig = cpmm.full_correction_net

    def recorder(x, y, fee, pi):                     # same result; records the state every configuration sees
        rec.append((np.array(x, dtype=float), np.array(y, dtype=float)))
        return orig(x, y, fee, pi)

    cpmm.full_correction_net = recorder
    try:
        r = e2.simulate(g, POOLS[pool_key], C, cfg2, R_usd, np.nan, d, k, seed)
    finally:
        cpmm.full_correction_net = orig
    ok = np.isfinite(g["p_ref_0"].to_numpy()) & np.isfinite(g[f"p_ref_{d}"].to_numpy()) & np.isfinite(g["L"].to_numpy()) \
        & np.isfinite(g["gas_wei"].to_numpy())
    act = np.flatnonzero(ok & (np.arange(len(g)) % k == 0))
    assert len(rec) == len(act), f"recorded {len(rec)} states for {len(act)} action steps"
    xs = np.array([x[1] for x, _ in rec])
    ys = np.array([y[1] for _, y in rec])
    eps = eA.rolling_eps(r, g, seed, k, d, cfg2)
    steps = pd.DataFrame({"i": act, "x": xs, "y": ys})
    steps["executed"] = r["step_exec"][act, 1]
    steps["r_e2_usd"] = r["step_protection"][act, 1].astype(float)
    steps["eps_usd"] = eps[act]
    return g, steps, r["table"].iloc[1], R_usd, C.iloc[1]


# ------------------------------------------------------------------ plan and execution of one opportunity
def F_float(a, kappa, lam, gam):
    return np.minimum(lam * a, (1 - gam) * np.maximum(a - kappa, 0.0))


def plan(o, N, G, lam, gam, R, tol):
    """Best (size index, pieces) under start-time information. Returns None if no plan reaches R."""
    fr = np.arange(1, G + 1) / G
    n = o["n0"] * fr
    tr = cpmm.trade(o["x"], o["y"], o["fee"], o["dirn"], n)
    S = np.maximum(o["pi0"] * tr["d0"] + tr["d1"], 0.0) * o["usd0"]
    Sh = np.maximum(o["rho0"] * tr["d0"] + tr["d1"], 0.0) * o["usd0"]
    piece = o["kappa0"] - o["unit0"]
    ls = np.arange(1, N + 1)
    rem = Sh[:, None] - (ls[None, :] - 1) * piece
    valid = (rem > 0) | (ls[None, :] == 1)
    P = S[:, None] - o["C0"] - F_float(np.maximum(rem, 0.0), o["kappa0"], lam, gam) - (ls[None, :] - 1) * o["cext0"]
    P = np.where(valid & (P >= R - tol), P, -np.inf)
    if not np.isfinite(P).any():
        return None
    idx = int(argmax_smallest(P.ravel()))
    jj, li = divmod(idx, N)
    return {"j": jj, "l": li + 1, "n": n[jj], "S": S[jj], "Sh": Sh[jj], "rem": max(rem[jj, li], 0.0), "planned": P[jj, li]}


def execute(o, p, blocks, mode, Fint, gas, lam, gam):
    """Realised outcome of plan p over its pieces. blocks: dict of per-block arrays (pi, rho, usd, ethusd, kappa, gp)."""
    l = p["l"]
    piece = o["kappa0"] - o["unit0"]
    targets = (p["rem"] + piece * np.arange(l)) / o["usd0"]          # cumulative S_hat at rho_0, numeraire
    cum = np.empty(l)
    if l > 1:
        cum[:-1] = cpmm.smallest_net_for_surplus(o["x"], o["y"], o["fee"], o["dirn"], p["n"], o["rho0"], targets[:-1])
    cum[-1] = p["n"]
    assert np.all(np.diff(cum) >= 0), "cumulative inputs must increase"
    tr = cpmm.trade(o["x"], o["y"], o["fee"], o["dirn"], cum)
    d0 = np.diff(np.concatenate([[0.0], tr["d0"]]))
    d1 = np.diff(np.concatenate([[0.0], tr["d1"]]))
    b = np.arange(l)
    if mode == "constant":
        pi, rho, usd, eth = (np.full(l, o[k]) for k in ("pi0", "rho0", "usd0", "ethusd0"))
        kap, gp = np.full(l, o["kappa0"]), np.full(l, o["gp0"])
    else:
        pi, rho, usd, eth, kap, gp = (blocks[k][b] for k in ("pi", "rho", "usd", "ethusd", "kappa", "gp"))
    S = (pi * d0 + d1) * usd
    Sh = np.maximum(rho * d0 + d1, 0.0) * usd
    ch = np.array([Fint(fp.to_wad(s), fp.to_wad(kk)) for s, kk in zip(Sh, kap)], dtype=float) / WAD
    gas_b = np.where(b == 0, gas["first"], np.where(ch > 0, gas["extra_charged"], gas["extra"]))
    cost = gas_b * gp * 1e-18 * eth
    price_after = tr["y"] / tr["x"]
    return {"charge": ch.sum(), "payoff": (S - ch - cost).sum(), "S": S.sum(), "cost": cost.sum(),
            "n_pieces_charged": int((ch > 0).sum()), "price_after": price_after}


def twe(p0, price_after, pi, dt):
    """Time-weighted |log(pool / benchmark)| over the horizon: block b's interval holds the pool after its piece."""
    H = len(dt)
    p = np.full(H, p0)
    m = min(len(price_after), H)
    if m:
        p[:m] = price_after[:m]
        p[m:] = price_after[m - 1]
    return float(np.sum(dt * np.abs(np.log(p / pi[:H]))) / np.sum(dt))


# ------------------------------------------------------------------ one pool, one rule
def run(pool_key, variant, rule, split, cfg2, gas, tau, Fint, lam, gam, blocks_all):
    mech, k, d = RULES[rule]
    t0 = time.time()
    g, steps, tab, R_usd, cfg_row = replay(pool_key, variant, mech, k, d, split, cfg2, lam, gam)
    pool = POOLS[pool_key]
    gs = pd.DatetimeIndex(g["t"]).tz_convert("UTC").tz_localize(None).as_unit("ns")     # naive UTC, like the block times
    gmin = gs[0]
    ex = steps[steps["executed"]].reset_index(drop=True)
    bts = blocks_all["ts"]
    Nmax = max(NS)
    G = cfg2["grid_points"]
    gas_u = cfg2["gas_units"] + cfg2["hook_overhead_gas"]
    tol = 1e-9 * max(1.0, R_usd)
    pref0, prefd = g["p_ref_0"].to_numpy(), g[f"p_ref_{d}"].to_numpy()
    usd_g, eth_g = g["usd_per_num"].to_numpy(), g["eth_in_num"].to_numpy()
    rows, dropped = [], 0
    for o_ in ex.itertuples():
        t = gs[o_.i]
        bi = int(np.searchsorted(bts, t.value, "left"))
        if bi + Nmax >= len(bts):
            dropped += 1
            continue
        bidx = np.arange(bi, bi + Nmax + 1)                       # one extra block for the last interval's length
        mins = ((pd.DatetimeIndex(bts[bidx]).floor("min") - gmin) // pd.Timedelta(minutes=1)).to_numpy()
        if mins[-1] >= len(g) or mins[0] < 0:
            dropped += 1                                          # horizon leaves the split's months
            continue
        mb = mins[:-1]
        pi, rho = pref0[mb], prefd[mb]
        usd, ethusd = usd_g[mb], eth_g[mb] * usd_g[mb]
        if not (np.isfinite(pi).all() and np.isfinite(rho).all() and np.isfinite(usd).all()):
            dropped += 1
            continue
        delta = (o_.eps_usd if np.isfinite(o_.eps_usd) else 0.0) * float(cfg_row["dmult"])
        bf, gp = blocks_all["base_fee"][bidx[:-1]], blocks_all["gas_price"][bidx[:-1]]
        dt = np.diff(bts[bidx]).astype(float) / 1e9
        dirn, n0 = cpmm.full_correction_net(o_.x, o_.y, pool.fee, pi[0])
        p_pool0 = o_.y / o_.x
        trf = cpmm.trade(o_.x, o_.y, pool.fee, dirn, n0)
        S_full = float(np.maximum(pi[0] * trf["d0"] + trf["d1"], 0.0)) * usd[0]
        C0 = gas["first"] * gp[0] * 1e-18 * ethusd[0]
        bfeas = bool(n0 > 0 and S_full - C0 >= R_usd - tol)            # baseline: zero transfer, same transaction gas
        for kconv in KAPPA_CONV:
            # basefee: the hook's Eq. 9 (block base fee + tau_hat); gasprice: E2's K_hat (the block's gas price)
            kappa = gas_u * ((bf + tau) if kconv == "basefee" else gp) * 1e-18 * ethusd + R_usd + delta
            blocks = {"pi": pi, "rho": rho, "usd": usd, "ethusd": ethusd, "kappa": kappa, "gp": gp}
            o = {"x": o_.x, "y": o_.y, "fee": pool.fee, "dirn": int(dirn), "n0": float(n0), "pi0": pi[0], "rho0": rho[0],
                 "usd0": usd[0], "ethusd0": ethusd[0], "kappa0": kappa[0], "gp0": gp[0],
                 "unit0": float(eA.unit_usd(pool_key, usd[0])), "C0": C0,
                 "cext0": gas["extra"] * gp[0] * 1e-18 * ethusd[0]}
            base = {"pool": pool_key, "variant": variant, "rule": rule, "kappa": kconv, "t": t,
                    "block0": int(blocks_all["block"][bi]), "baseline_feasible": bfeas, "r_e2_usd": o_.r_e2_usd,
                    "kappa0_usd": kappa[0], "delta_usd": delta, "rho0_over_pi0": rho[0] / pi[0], "pi_move_75": pi[-1] / pi[0] - 1}
            plans = {N: (plan(o, N, G, lam, gam, R_usd, tol) if n0 > 0 else None) for N in NS}
            p1 = plans[1]
            for mode in ("dynamic", "constant"):
                e1 = execute(o, p1, blocks, mode, Fint, gas, lam, gam) if p1 else None
                for N in NS:
                    pN = plans[N]
                    eN = e1 if N == 1 else (execute(o, pN, blocks, mode, Fint, gas, lam, gam) if pN else None)
                    piN = pi[:N]
                    rows.append({**base, "mode": mode, "N": N,
                                 "executed_1": p1 is not None, "executed_N": pN is not None,
                                 "l_star": pN["l"] if pN else 0, "size_frac": (pN["j"] + 1) / G if pN else 0.0,
                                 "charge_1": e1["charge"] if e1 else 0.0, "charge_N": eN["charge"] if eN else 0.0,
                                 "payoff_1": e1["payoff"] if e1 else 0.0, "payoff_N": eN["payoff"] if eN else 0.0,
                                 "planned_N": pN["planned"] if pN else np.nan, "gas_N": eN["cost"] if eN else 0.0,
                                 "n_pieces_charged": eN["n_pieces_charged"] if eN else 0,
                                 "twe_1": twe(p_pool0, e1["price_after"] if e1 else np.array([]), piN, dt[:N]),
                                 "twe_N": twe(p_pool0, eN["price_after"] if eN else np.array([]), piN, dt[:N])})
    out = pd.DataFrame(rows)
    info = {"pool": pool_key, "variant": variant, "rule": rule, "mech": mech, "k": k, "d": d, "R_usd": R_usd,
            "n_action_steps": len(steps), "n_executed_e2": len(ex), "n_dropped_horizon": dropped,
            "n_opportunities": len(ex) - dropped, "e2_protection_recomputed": float(tab["protection_usd"]),
            "e2_n_executed_recomputed": int(tab["n_executed"])}
    print(f"{pool_key} {variant} {rule}: {info['n_opportunities']} opportunities ({dropped} dropped) in {time.time() - t0:.0f}s", flush=True)
    return out, info


def summarize(df):
    q = lambda x, p: float(np.quantile(x, p)) if len(x) else np.nan  # noqa: E731
    rows = []
    keys = ["pool", "variant", "rule", "kappa", "mode", "N"]
    for key, x in df.groupby(keys, sort=False):
        c1, cN = x["charge_1"], x["charge_N"]
        m = c1 > 0
        split = x["l_star"] > 1
        either = x["executed_1"] | x["executed_N"]
        both = x["executed_1"] & x["executed_N"]
        gain = x["payoff_N"] - x["payoff_1"]
        prof = either & (gain > 1e-9)
        bf = x["baseline_feasible"]
        below = lambda p: p < x["R_usd"] - 1e-9 * np.maximum(1, x["R_usd"])  # noqa: E731
        viol_noexec = bf & ~x["executed_N"]
        viol_payoff = bf & x["executed_N"] & below(x["payoff_N"])
        viol_1 = bf & (~x["executed_1"] | below(x["payoff_1"]))
        viol_new = (viol_noexec | viol_payoff) & ~viol_1                  # caused by the split, not present unsplit
        rows.append(dict(zip(keys, key)) | {
            "n_opportunities": len(x), "n_charged_1": int(m.sum()), "transfer_1_usd": c1.sum(), "transfer_N_usd": cN.sum(),
            "surviving_share_sum_weighted": cN.sum() / c1.sum() if c1.sum() > 0 else np.nan,
            "surviving_share_mean_ratio": float((cN[m] / c1[m]).mean()) if m.any() else np.nan,
            "share_split_planned": float(split.mean()), "l_star_median_split": q(x.loc[split, "l_star"], 0.5),
            "l_star_max": int(x["l_star"].max()),
            "share_split_profitable_ex_post": float(prof.mean()),
            "share_profitable_of_planned_splits": float(prof[split].mean()) if split.any() else np.nan,
            "share_split_loses_ex_post": float((split & (gain < -1e-9)).mean()),
            "gain_median_usd_all": q(gain, 0.5), "gain_median_usd_splits": q(gain[split], 0.5), "gain_sum_usd": gain.sum(),
            "twe_1_bp": 1e4 * x["twe_1"].mean(), "twe_N_bp": 1e4 * x["twe_N"].mean(),
            "extra_twe_bp": 1e4 * (x["twe_N"] - x["twe_1"]).mean(),
            "extra_twe_rel": x["twe_N"].sum() / x["twe_1"].sum() - 1 if x["twe_1"].sum() > 0 else np.nan,
            # like for like: opportunities executed both unsplit and split (the mean above also contains opportunities
            # that only the split executes, whose pool the unsplit run leaves uncorrected for the whole horizon)
            "n_both_executed": int(both.sum()),
            "extra_twe_bp_both_executed": 1e4 * (x["twe_N"] - x["twe_1"])[both].mean() if both.any() else np.nan,
            "extra_twe_bp_median_both_executed": 1e4 * q((x["twe_N"] - x["twe_1"])[both], 0.5),
            "extra_twe_rel_both_executed": (x["twe_N"][both].sum() / x["twe_1"][both].sum() - 1) if both.any() else np.nan,
            "n_baseline_feasible": int(bf.sum()), "violations": int((viol_noexec | viol_payoff).sum()),
            "violation_rate": float((viol_noexec | viol_payoff).sum() / bf.sum()) if bf.any() else np.nan,
            "violations_not_executed": int(viol_noexec.sum()), "violations_payoff_below_R": int(viol_payoff.sum()),
            "violations_unsplit": int(viol_1.sum()), "violations_new_from_split": int(viol_new.sum()),
            "violation_rate_new_from_split": float(viol_new.sum() / bf.sum()) if bf.any() else np.nan})
    return pd.DataFrame(rows)


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    ap.add_argument("--confirm-frozen", action="store_true")
    a = ap.parse_args()
    cfg2 = reporting.load_config(ROOT / "experiments" / "configs" / "e2.yml")
    if a.split == "test":
        if not a.confirm_frozen:
            raise SystemExit("--split test requires --confirm-frozen (experiment B runs on the validation months)")
        reporting.guard_split("e2", "test", True, cfg2)
    gas_cfg = json.load(open(GAS_CFG))
    gas = {"first": cfg2["gas_units"] + cfg2["hook_overhead_gas"], "extra": gas_cfg["additional_transaction_new_block_gas"]["uncharged"],
           "extra_charged": gas_cfg["additional_transaction_new_block_gas"]["charged"]}
    lam, gam = cfg2["headline"]["lambda"], cfg2["headline"]["gamma"]
    tau = eA.tau_hat_wei()
    Fint = eA.Fint(lam, gam)
    bg = pd.read_parquet(CACHE / "block_gas.parquet", columns=["block", "timestamp", "base_fee_wei", "gas_price_wei"])
    bg = bg.sort_values("block").reset_index(drop=True)
    blocks_all = {"ts": pd.DatetimeIndex(bg["timestamp"]).tz_convert("UTC").as_unit("ns").asi8, "block": bg["block"].to_numpy(),
                  "base_fee": bg["base_fee_wei"].to_numpy(float), "gas_price": bg["gas_price_wei"].to_numpy(float)}
    assert np.all(np.diff(blocks_all["ts"]) >= 0)
    OUT.mkdir(parents=True, exist_ok=True)
    tag = a.split
    per, infos = [], []
    for key, var in POOL_VARIANTS:
        for rule in RULES:
            df, info = run(key, var, rule, a.split, cfg2, gas, tau, Fint, lam, gam, blocks_all)
            df["R_usd"] = info["R_usd"]
            per.append(df)
            infos.append(info)
    per = pd.concat(per, ignore_index=True)
    lim = mg.SPLIT_RANGE[a.split][1].tz_convert("UTC").tz_localize(None)
    assert (pd.DatetimeIndex(per["t"]) < lim).all(), "an opportunity lies outside the split"
    summ = summarize(per)
    per.to_csv(OUT / f"eB_opportunities_{tag}.csv.gz", index=False)
    summ.to_csv(OUT / f"eB_summary_{tag}.csv", index=False)

    # checks: recomputed E2 totals equal the stored summaries
    checks = []
    for info in infos:
        mech, k, d = RULES[info["rule"]]
        st_tag = f"{a.split}_lag{d}" + (f"_k{k}" if k != 1 else "") + "_med"
        st = pd.read_parquet(RESULTS / "e2" / f"e2_summary_{st_tag}.parquet")
        st = st[(st.pool == info["pool"]) & (st.variant == info["variant"]) & (st.regime == "median") & (st.mech == mech)
                & (st.lam == lam) & (st.gamma == gam)].iloc[0]
        c = {**info, "stored_tag": st_tag, "e2_protection_stored": float(st["protection_usd"]), "e2_n_executed_stored": int(st["n_executed"])}
        c["exact"] = c["e2_protection_recomputed"] == c["e2_protection_stored"] and c["e2_n_executed_recomputed"] == c["e2_n_executed_stored"]
        assert c["exact"], c
        checks.append(c)
    pd.DataFrame(checks).to_csv(OUT / f"eB_checks_{tag}.csv", index=False)

    # comparison with the constant-reference bracket of Experiment A follow-up 2 (test months, ETH/USDC)
    br = pd.read_csv(BRACKET)
    br = br[(br["pool"] == "eth_usdc_005") & br["part"].str.startswith("2") & (br["aggregation"] == "sum-weighted")]
    comp = []
    for rule in RULES:
        for N in (5, 75):
            b = br[(br["rule"] == rule) & (br["l_cap"] == f"l<={N} blocks")]
            assert len(b) == 1, (rule, N, len(b))
            s = summ[(summ.pool == "eth_usdc_005") & (summ.rule == rule) & (summ.N == N)].set_index(["kappa", "mode"])
            row = {"rule": rule, "N": N, "bracket_eA_fu2_test": float(b["share"].iloc[0])}
            for kc in KAPPA_CONV:
                for md in ("constant", "dynamic"):
                    row[f"{md}_valid_kappa_{kc}"] = float(s.loc[(kc, md), "surviving_share_sum_weighted"])
            comp.append(row)
    pd.DataFrame(comp).to_csv(OUT / f"eB_bracket_comparison_{tag}.csv", index=False)

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
    inputs = ([CACHE / "aligned" / f"{k_}.parquet" for k_, _ in POOL_VARIANTS] + [CACHE / "block_gas.parquet", GAS_CFG, BRACKET]
              + [RESULTS / "e1" / f"depth_{k_}.parquet" for k_, _ in POOL_VARIANTS]
              + sorted((ROOT / "data" / "data" / "binance").glob("*_1m.csv.gz"))
              + [RESULTS / "e2" / f"e2_summary_{c['stored_tag']}.parquet" for c in checks] + [Path(__file__).resolve()])
    m = {"experiment": "eB", "split": a.split, "git_commit": head, "uncommitted": dirty,
         "note": "data/data/binance/*_1m.csv.gz are the main checkout's current klines (uncommitted there); their hashes are listed",
         "inputs": {Path(p).relative_to(ROOT).as_posix(): sha(p) for p in dict.fromkeys(inputs)},
         "linked_from_main_checkout": ["cache", "results/e1", "results/e2", "data/data/lido"],
         "parameters": {"N": list(NS), "rules": {r: dict(zip(["mech", "k", "d"], v)) for r, v in RULES.items()},
                        "pool_variants": POOL_VARIANTS, "lambda": lam, "gamma": gam, "R": "median regime",
                        "grid_points": cfg2["grid_points"], "kappa_gas_units": cfg2["gas_units"] + cfg2["hook_overhead_gas"],
                        "tau_hat_wei": tau, "gas": gas, "gas_config": str(GAS_CFG.relative_to(ROOT)),
                        "e2_config_sha256": reporting.config_hash(cfg2)},
         "checks": checks, "outputs": {p.name: sha(p) for p in sorted(OUT.glob(f"eB_*_{tag}*"))}}
    (OUT / f"manifest_{tag}.json").write_text(json.dumps(m, indent=1, default=str))
    with pd.option_context("display.width", 250, "display.max_columns", 60):
        print(summ.to_string())
        print(pd.DataFrame(comp).to_string())


if __name__ == "__main__":
    main()
