#!/usr/bin/env python3
"""E6: fragmentation and reversal attacks on real arbitrage opportunities (cumulative watermark accounting).

Opportunity = a real swap whose pre-swap deviation from the (offset-corrected) reference exceeds the pool fee (2x for
USDC/USDT) and that moves the price toward the reference. The price-correcting part of the swap is replayed through the
calibrated CPMM (E1 depth known before the swap) and split into n fragments: equal input splits, adversarial splits (the
exact optimiser for the OLD independent-callback rule, from contracts/python/fragmentation_replay.py), and reversal paths
(forward, then an adverse trade, then recovery). Checks use the integer fixed-point reference.

  python experiments/e6_fragmentation.py --split valid
  python experiments/e6_fragmentation.py --freeze
  python experiments/e6_fragmentation.py --split test --confirm-frozen
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
sys.path.insert(0, str(ROOT / "contracts" / "python"))

from common import cpmm, fixedpoint as fp, mechanism as mech, minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS, RESULTS  # noqa: E402
from fragmentation_replay import adversarial_partition  # noqa: E402

CFG = ROOT / "experiments" / "configs" / "e6.yml"


def all_candidates(pool_key: str, variant: str, split: str, cfg: dict, R_usd: float) -> tuple[pd.DataFrame, int]:
    """Every swap meeting the opportunity definition, with the CPMM replay of its price-correcting part (no feasibility filter)."""
    pool = POOLS[pool_key]
    al = mg.load_aligned(pool_key)
    al = al[al["split"] == split].copy()
    w = mg.VARIANTS[variant]
    off = np.nan_to_num(al[f"offset_{w}"].to_numpy(), nan=0.0) if w else 0.0
    al["pi"] = al["p_ref"] * np.exp(off)
    al["pre_c"] = np.log(al["price_pre"] / al["pi"])
    al["post_c"] = np.log(al["price"] / al["pi"])
    thr = cfg["gap_multiple"][pool_key] * pool.fee
    c = al[(al["pre_c"].abs() > thr) & (al["post_c"].abs() < al["pre_c"].abs())].copy()
    n_raw = len(c)
    dep = pd.read_parquet(RESULTS / "e1" / f"depth_{pool_key}.parquet").set_index("timestamp")["L_trailing"]
    c["L"] = dep.reindex(pd.DatetimeIndex(c["timestamp"]).floor("min")).to_numpy()
    c = c[c["L"].notna()]
    # replay the price-correcting part of the real swap through the calibrated CPMM
    x = (c["L"] / np.sqrt(c["price_pre"])).to_numpy()
    y = (c["L"] * np.sqrt(c["price_pre"])).to_numpy()
    dirn, nstar = cpmm.full_correction_net(x, y, pool.fee, c["pi"].to_numpy())
    a0, a1 = c["amount0"].to_numpy(), c["amount1"].to_numpy()
    n_real = np.where(a1 > 0, a1, a0) * (1 - pool.fee)
    n_used = np.minimum(n_real, nstar)
    tr = cpmm.trade(x, y, pool.fee, dirn, n_used)
    S = np.maximum(c["pi"].to_numpy() * tr["d0"] + tr["d1"], 0.0)
    C = mg.gas_cost_num(c["gas_price_wei"].to_numpy(), c["eth_in_num"].to_numpy(), cfg["gas_units"] + cfg["hook_overhead_gas"])
    usd = c["usd_per_num"].to_numpy()
    R = R_usd / usd
    c = c.assign(x=x, y=y, dirn=dirn, n_used=n_used, S=S, C=C, R=R, S_usd=S * usd, C_usd=C * usd)
    return c, n_raw


def select_opportunities(pool_key: str, variant: str, split: str, cfg: dict, R_usd: float, rng) -> tuple[pd.DataFrame, dict]:
    c, n_raw = all_candidates(pool_key, variant, split, cfg, R_usd)
    feas = (c["n_used"] > 0) & (c["S"] - c["C"] >= c["R"])
    c = c[feas].reset_index(drop=True)
    info = {"candidates_definition": n_raw, "with_depth_and_baseline_feasible": len(c)}
    if len(c) == 0:
        return c, info
    q = pd.qcut(c["S_usd"].rank(method="first"), 4, labels=False) if len(c) >= 4 else pd.Series(0, index=c.index)
    per = int(np.ceil(cfg["n_opportunities"] / 4))
    pick = []
    for k in range(4):
        idx = c.index[q == k].to_numpy()
        pick += list(rng.choice(idx, min(per, len(idx)), replace=False)) if len(idx) else []
    c = c.loc[sorted(pick)].reset_index(drop=True)
    c["quartile"] = q.loc[sorted(pick)].to_numpy() + 1
    info["selected"] = len(c)
    return c, info


def path_prefix_surplus(o, fracs: np.ndarray, pool) -> np.ndarray:
    """Cumulative surplus A_j (numeraire) after forward input fractions of the price-correcting trade."""
    t = cpmm.trade(o.x, o.y, pool.fee, o.dirn, fracs * o.n_used)
    return np.maximum(o.pi * t["d0"] + t["d1"], 0.0)


def reversal_prefix_surplus(o, n: int, pool) -> np.ndarray:
    """Sequential CPMM path: forward fragments to the full correction, an adverse trade of half the input, then
    recovery fragments. Direction flips are executed on the evolving state, so fees make the path lossy."""
    n_f = max(1, n // 2)
    steps = [(int(o.dirn), o.n_used / n_f)] * n_f
    if n >= 2:
        steps.append((-int(o.dirn), 0.5 * o.n_used))            # adverse trade (net input in the opposite direction)
    rest = n - len(steps)
    steps += [(int(o.dirn), 0.5 * o.n_used / max(rest, 1))] * max(rest, 0)
    x, y = o.x, o.y
    d0 = d1 = 0.0
    A = []
    for dr, amt in steps[:n]:
        # adverse leg is sized in the opposite token, so convert its net input via the reference price
        if dr != int(o.dirn):
            amt = amt * (o.pi if int(o.dirn) < 0 else 1.0 / o.pi) * 1.0
        t = cpmm.trade(x, y, pool.fee, dr, amt)
        x, y = float(t["x"]), float(t["y"])
        d0 += float(t["d0"])
        d1 += float(t["d1"])
        A.append(max(o.pi * d0 + d1, 0.0))
    return np.array(A)


def to_int_prefix(A_num: np.ndarray, usd: float) -> list[int]:
    return [fp.to_wad(a * usd) for a in A_num]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CFG))
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    ap.add_argument("--confirm-frozen", action="store_true")
    ap.add_argument("--freeze", action="store_true")
    a = ap.parse_args()
    cfg = reporting.load_config(a.config)
    out = reporting.out_dir("e6")
    if a.freeze:
        reporting.freeze("e6", cfg)
        print("config frozen:", reporting.config_hash(cfg)[:12])
        return
    reporting.guard_split("e6", a.split, a.confirm_frozen, cfg)
    rng = np.random.default_rng(cfg["seed"])
    lam, gam, delta = fp.to_wad(cfg["lam"]), fp.to_wad(cfg["gamma"]), fp.to_wad(cfg["delta_usd"])
    gp = RESULTS / "e7" / "gas_profile.json"
    gas_src = "measured (results/e7/gas_profile.json)" if gp.exists() else "placeholder: old paper figures"
    g_first, g_extra = cfg["gas_first_call"], cfg["gas_per_extra_fragment"]
    if gp.exists():
        prof = json.load(open(gp))
        g_first, g_extra = prof["first_call_gas"], prof["extra_fragment_gas"]
    rows, opp_rows, det = [], [], []
    for key in cfg["pools"]:
        pool = POOLS[key]
        for variant in cfg["variants"][key]:
            R_usd = mg.r_regimes_usd(key, variant, cfg["gas_units"], cfg["r_quantiles"])[cfg["r_regime"]]
            opps, info = select_opportunities(key, variant, a.split, cfg, R_usd, rng)
            opp_rows.append({"pool": key, "variant": variant, "gap_multiple": cfg["gap_multiple"][key], **info,
                             "note": "all available used" if info.get("selected", 0) < cfg["n_opportunities"] else ""})
            print(key, variant, info, flush=True)
            for n in cfg["partitions"]:
                agg = {"n_opps": 0, "mono_exact_fail": 0, "rev_max_fail": 0, "rev_negative_charge": 0, "cor1_fail": 0, "opt_exact_fail": 0,
                       "indep_ratio_equal": [], "indep_ratio_opt": [], "n_rev": 0, "n_opt": 0, "float_int_gap": 0.0}
                for o in opps.itertuples():
                    usd, K_num = o.usd_per_num, o.C + o.R
                    K_usd = K_num * usd
                    K_w = fp.to_wad(K_usd)
                    fracs = np.arange(1, n + 1) / n
                    A_eq = path_prefix_surplus(o, fracs, pool) * usd                        # USD
                    A_w = [fp.to_wad(v) for v in A_eq]
                    rows_eq = fp.cumulative_charges(A_w, K_w, lam, gam, delta)
                    F_end = fp.transfer_wad(A_w[-1], K_w, lam, gam, delta)[0]
                    agg["n_opps"] += 1
                    monotone = all(A_w[i] <= A_w[i + 1] for i in range(len(A_w) - 1))
                    if monotone and sum(r["charge"] for r in rows_eq) != F_end:
                        agg["mono_exact_fail"] += 1
                    fl = mech.watermark_charges(A_eq, K_usd, cfg["lam"], cfg["gamma"], cfg["delta_usd"]).sum()
                    agg["float_int_gap"] = max(agg["float_int_gap"], abs(fl - F_end / fp.WAD))
                    # independent (old) rule on the equal split
                    s_eq = np.diff(np.concatenate([[0.0], A_eq]))
                    F_full = float(mech.F_scalar(A_eq[-1], K_usd, cfg["lam"], cfg["gamma"], cfg["delta_usd"]))
                    ind = float(mech.independent_charges(s_eq, K_usd, cfg["lam"], cfg["gamma"], cfg["delta_usd"]).sum())
                    if F_full > 0:
                        agg["indep_ratio_equal"].append(ind / F_full)
                    # Corollary 1: fragmentation never improves the arbitrageur payoff
                    frag_cost_usd = (n - 1) * g_extra * 1e-18 * o.gas_price_wei * o.eth_in_num * usd
                    pay_unsplit = A_eq[-1] - o.C_usd - F_full
                    pay_frag = A_eq[-1] - o.C_usd - frag_cost_usd - sum(r["charge"] for r in rows_eq) / fp.WAD
                    if pay_frag > pay_unsplit + 1e-9:
                        agg["cor1_fail"] += 1
                    # adversarial split (surplus shares) for n >= 2
                    if n >= 2 and A_eq[-1] >= n * cfg["min_fragment_usd"]:
                        parts, _ = adversarial_partition(float(A_eq[-1]), n, K_usd, cfg["lam"], cfg["gamma"], cfg["delta_usd"], cfg["min_fragment_usd"])
                        cum = np.cumsum(parts)
                        tgt = np.minimum(cum / usd, cum[-1] / usd)
                        nn = np.array([float(cpmm.smallest_net_for_surplus(o.x, o.y, pool.fee, o.dirn, o.n_used, o.pi, t)) for t in tgt])
                        A_opt = path_prefix_surplus(o, nn / o.n_used, pool) * usd
                        A_ow = [fp.to_wad(v) for v in A_opt]
                        r_opt = fp.cumulative_charges(A_ow, K_w, lam, gam, delta)
                        agg["n_opt"] += 1
                        if all(A_ow[i] <= A_ow[i + 1] for i in range(len(A_ow) - 1)) and sum(r["charge"] for r in r_opt) != fp.transfer_wad(A_ow[-1], K_w, lam, gam, delta)[0]:
                            agg["opt_exact_fail"] += 1
                        s_opt = np.diff(np.concatenate([[0.0], A_opt]))
                        F_o = float(mech.F_scalar(A_opt[-1], K_usd, cfg["lam"], cfg["gamma"], cfg["delta_usd"]))
                        if F_o > 0:
                            r_o = float(mech.independent_charges(s_opt, K_usd, cfg["lam"], cfg["gamma"], cfg["delta_usd"]).sum()) / F_o
                            agg["indep_ratio_opt"].append(min(r_o, agg["indep_ratio_equal"][-1] if F_full > 0 else r_o))   # adversary takes the better split
                    # reversal path (needs at least 2 fragments)
                    if n >= 2:
                        A_r = reversal_prefix_surplus(o, n, pool) * usd
                        A_rw = [fp.to_wad(v) for v in A_r]
                        r_rev = fp.cumulative_charges(A_rw, K_w, lam, gam, delta)
                        agg["n_rev"] += 1
                        if sum(r["charge"] for r in r_rev) != max(r["target"] for r in r_rev):
                            agg["rev_max_fail"] += 1
                        if any(r["charge"] < 0 for r in r_rev):
                            agg["rev_negative_charge"] += 1
                rows.append({"pool": key, "variant": variant, "n_fragments": n, **{k: v for k, v in agg.items() if not isinstance(v, list)},
                             "indep_over_cumulative_equal": float(np.mean(agg["indep_ratio_equal"])) if agg["indep_ratio_equal"] else np.nan,
                             "indep_over_cumulative_optimal": float(np.mean(agg["indep_ratio_opt"])) if agg["indep_ratio_opt"] else np.nan,
                             "cumulative_over_cumulative": 1.0})
    res = pd.DataFrame(rows)
    tag = a.split
    reporting.write_table(res, out / "tables" / f"e6_checks_{tag}", {"indep_over_cumulative_equal": "{:.3f}", "indep_over_cumulative_optimal": "{:.3f}", "float_int_gap": "{:.1e}"})
    reporting.write_table(pd.DataFrame(opp_rows), out / "tables" / f"e6_opportunities_{tag}")
    # incremental gas penalty table (paper Table 1 layout)
    ns = cfg["partitions"]
    cum_gas = [g_first + (n - 1) * g_extra for n in ns]
    gt = pd.DataFrame({"n_fragments": ns, "cumulative_gas_k": [c / 1e3 for c in cum_gas], "penalty_vs_n1_pct": [100 * (c / cum_gas[0] - 1) for c in cum_gas],
                       "source": gas_src})
    reporting.write_table(gt, out / "tables" / f"e6_gas_penalty_{tag}", {"cumulative_gas_k": "{:.1f}", "penalty_vs_n1_pct": "{:.1f}"})
    reporting.write_manifest("e6", cfg, [CACHE / "aligned" / f"{k}.parquet" for k in cfg["pools"]], a.split, {"gas_source": gas_src})
    tot = res[["mono_exact_fail", "opt_exact_fail", "rev_max_fail", "rev_negative_charge", "cor1_fail"]].sum()
    print("VIOLATION COUNTS (expected 0):", tot.to_dict())
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(pd.DataFrame(opp_rows).to_string(index=False))
        print(res[res["variant"].isin(["raw"])][["pool", "n_fragments", "n_opps", "n_opt", "n_rev", "indep_over_cumulative_equal", "indep_over_cumulative_optimal", "float_int_gap"]].round(4).to_string(index=False))
        print(gt.round(1).to_string(index=False))


if __name__ == "__main__":
    main()
