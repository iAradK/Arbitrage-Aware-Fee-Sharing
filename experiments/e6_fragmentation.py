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
    c = c.assign(x=x, y=y, dirn=dirn, n_used=n_used, S=S, C=C, R=R, S_usd=S * usd, C_usd=C * usd, d1=tr["d1"])
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


def fragment_deltas(o, cum_inputs: np.ndarray, pool, dirs=None) -> tuple[np.ndarray, np.ndarray]:
    """Per-fragment trader deltas (d0 token0, d1 token1, numeraire units) of a sequential path; cum_inputs are the
    forward path's cumulative net inputs (dirs None), or the (direction, amount) steps of a mixed path."""
    if dirs is None:
        t = cpmm.trade(o.x, o.y, pool.fee, o.dirn, cum_inputs)
        c0, c1 = np.concatenate([[0.0], np.atleast_1d(t["d0"])]), np.concatenate([[0.0], np.atleast_1d(t["d1"])])
        return np.diff(c0), np.diff(c1)
    x, y, d0s, d1s = o.x, o.y, [], []
    for dr, amt in dirs:
        t = cpmm.trade(x, y, pool.fee, dr, amt)
        x, y = float(t["x"]), float(t["y"])
        d0s.append(float(t["d0"]))
        d1s.append(float(t["d1"]))
    return np.array(d0s), np.array(d1s)


def reversal_steps(o, n: int) -> list:
    """The (direction, net input) steps of reversal_prefix_surplus."""
    n_f = max(1, n // 2)
    steps = [(int(o.dirn), o.n_used / n_f)] * n_f
    if n >= 2:
        steps.append((-int(o.dirn), 0.5 * o.n_used * (o.pi if int(o.dirn) < 0 else 1.0 / o.pi)))
    rest = n - len(steps)
    steps += [(int(o.dirn), 0.5 * o.n_used / max(rest, 1))] * max(rest, 0)
    return steps[:n]


def block_scope_charges(o, d0s, d1s, layout: str, K_usd: float, eps_rel: float, lam_bps: int, gam_bps: int) -> tuple[int, int]:
    """Total charge (ETH wad) of a fragment path through the final hook's integer reference, contract orientation
    (token0 = ETH, token1 = the pool's numeraire token, reference = token0 per token1 = 1 / pi). layout: "one" (one swap,
    the sum of the deltas), "tx" (one transaction), "txs" (one transaction per fragment, one block). Returns the total
    collected and F(final block surplus)."""
    usd = o.usd_per_num
    S = 10 ** 18
    ref = int(round(S / o.pi))                              # token0 (ETH) per token1 unit, WAD
    k_w = int(round(K_usd / (o.pi * usd) * S))              # kappa in ETH wad
    h = fp.ScopedHookReference(k_w, 0, lam_bps, gam_bps, scope="block", accumulation="tx_clip", buffer="rel",
                               eps_wad=int(round(eps_rel * 1e9)) * 10 ** 9, volume="net")
    c0 = np.round(np.cumsum(np.concatenate([[0.0], d0s])) * S).astype(object)
    c1 = np.round(np.cumsum(np.concatenate([[0.0], d1s])) * S).astype(object)
    i0, i1 = [int(c0[j + 1] - c0[j]) for j in range(len(d0s))], [int(c1[j + 1] - c1[j]) for j in range(len(d1s))]
    if layout == "one":
        i0, i1 = [sum(i0)], [sum(i1)]
    tot = 0
    for j, (a0, a1) in enumerate(zip(i0, i1)):
        tot += h.swap("p", 1, j if layout == "txs" else 0, a0, a1, ref, settle_token0=True)
    A = h.surplus("p")
    F_A = fp.transfer_wad(A, k_w, lam_bps * fp.BPS_TO_WAD, gam_bps * fp.BPS_TO_WAD, 0)[0]
    return tot, F_A


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
    if "final" in cfg:                                   # not frozen yet: validation months only
        if a.split != "valid":
            raise SystemExit("the final-rule E6 config runs on the validation months only until it is frozen")
    else:
        reporting.guard_split("e6", a.split, a.confirm_frozen, cfg)
    rng = np.random.default_rng(cfg["seed"])
    lam, gam, delta = fp.to_wad(cfg["lam"]), fp.to_wad(cfg["gamma"]), fp.to_wad(cfg["delta_usd"])
    gp = RESULTS / "e7" / "gas_profile.json"
    gas_src = "measured (results/e7/gas_profile.json)" if gp.exists() else "placeholder: old paper figures"
    g_first, g_extra = cfg["gas_first_call"], cfg["gas_per_extra_fragment"]
    fin = cfg.get("final")
    if fin is not None:                                  # the final block-scoped hook's fragment gas
        g_first, g_extra = fin["gas_first_call"], fin["gas_per_extra_fragment"]
        gas_src = "final block-scoped hook (BlockScopedHookFragmentGasTest, config/gas_block_scope.json)"
        lam_bps, gam_bps = int(round(cfg["lam"] * 1e4)), int(round(cfg["gamma"] * 1e4))
        epst = pd.read_csv(reporting.run_root() / fin["eps_source"])
    elif gp.exists():
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
            if fin is not None and len(opps):
                et = epst[(epst["pool"] == key) & (epst["variant"] == variant)]
                ppb = pd.Series(et["eps_rel_ppb"].to_numpy(float), index=pd.DatetimeIndex(pd.to_datetime(et["day"])).tz_localize(None))
                dd = pd.DatetimeIndex(opps["timestamp"])
                dd = (dd.tz_convert(None) if dd.tz is not None else dd).floor("D")
                opps = opps.assign(eps_rel=ppb.reindex(dd).to_numpy() / 1e9)
                assert opps["eps_rel"].notna().all(), f"{key} {variant}: opportunity days without a daily eps"
            for n in cfg["partitions"]:
                agg = {"n_opps": 0, "mono_exact_fail": 0, "rev_max_fail": 0, "rev_negative_charge": 0, "cor1_fail": 0, "opt_exact_fail": 0,
                       "indep_ratio_equal": [], "indep_ratio_opt": [], "n_rev": 0, "n_opt": 0, "float_int_gap": 0.0}
                if fin is not None:                      # block-scoped checks (final hook, integer reference)
                    agg.update({"bs_n_charged_unsplit": 0, "bs_one_tx_ne_unsplit": 0, "bs_txs_below_unsplit": 0,
                                "bs_txs_equal_unsplit": 0, "bs_txs_above_unsplit": 0, "bs_txs_max_shortfall_wei": 0,
                                "bs_txs_ratio": [], "bs_rev_ne_F": 0, "bs_rev_negative": 0, "bs_indep_ratio": []})
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
                    if fin is not None:                  # block-scoped reference, final hook
                        d0s, d1s = fragment_deltas(o, fracs * o.n_used, pool)
                        U, _ = block_scope_charges(o, d0s, d1s, "one", K_usd, o.eps_rel, lam_bps, gam_bps)
                        T1, _ = block_scope_charges(o, d0s, d1s, "tx", K_usd, o.eps_rel, lam_bps, gam_bps)
                        Tn, _ = block_scope_charges(o, d0s, d1s, "txs", K_usd, o.eps_rel, lam_bps, gam_bps)
                        agg["bs_one_tx_ne_unsplit"] += int(T1 != U)
                        if U > 0:
                            agg["bs_n_charged_unsplit"] += 1
                            agg["bs_txs_ratio"].append(Tn / U)
                            agg["bs_txs_below_unsplit"] += int(Tn < U)
                            agg["bs_txs_equal_unsplit"] += int(Tn == U)
                            agg["bs_txs_above_unsplit"] += int(Tn > U)
                            agg["bs_txs_max_shortfall_wei"] = max(agg["bs_txs_max_shortfall_wei"], int(U - Tn))
                        # independent per-swap rule with the final buffer (Fig. 4d): each fragment its own scope
                        sb = np.maximum(o.pi * d0s + d1s - o.eps_rel * np.abs(d1s), 0.0) * usd
                        unb = max(float(o.pi * d0s.sum() + d1s.sum() - o.eps_rel * abs(d1s.sum())), 0.0) * usd
                        F_u = float(mech.F_scalar(unb, K_usd, cfg["lam"], cfg["gamma"], 0.0))
                        if F_u > 0:
                            agg["bs_indep_ratio"].append(float(mech.independent_charges(sb, K_usd, cfg["lam"], cfg["gamma"], 0.0).sum()) / F_u)
                        if n >= 2:                       # reversal path across transactions: sum of charges = F(final block surplus)
                            r0, r1 = fragment_deltas(o, None, pool, reversal_steps(o, n))
                            Tr, F_A = block_scope_charges(o, r0, r1, "txs", K_usd, o.eps_rel, lam_bps, gam_bps)
                            agg["bs_rev_ne_F"] += int(Tr != F_A)
                            agg["bs_rev_negative"] += int(Tr < 0)
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
                             "cumulative_over_cumulative": 1.0,
                             **({"bs_txs_over_unsplit_mean": float(np.mean(agg["bs_txs_ratio"])) if agg["bs_txs_ratio"] else np.nan,
                                 "bs_txs_over_unsplit_min": float(np.min(agg["bs_txs_ratio"])) if agg["bs_txs_ratio"] else np.nan,
                                 "bs_indep_over_unsplit_equal": float(np.mean(agg["bs_indep_ratio"])) if agg["bs_indep_ratio"] else np.nan}
                                if fin is not None else {})})
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
    ins = [CACHE / "aligned" / f"{k}.parquet" for k in cfg["pools"]]
    if fin is not None:
        ins += [reporting.run_root() / fin["eps_source"], ROOT / "config" / "gas_block_scope.json", Path(a.config).resolve()]
    reporting.write_manifest("e6", cfg, ins, a.split, {"gas_source": gas_src, "results_run": reporting.RESULTS_RUN})
    tot = res[["mono_exact_fail", "opt_exact_fail", "rev_max_fail", "rev_negative_charge", "cor1_fail"]].sum()
    print("VIOLATION COUNTS (expected 0):", tot.to_dict())
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(pd.DataFrame(opp_rows).to_string(index=False))
        print(res[res["variant"].isin(["raw"])][["pool", "n_fragments", "n_opps", "n_opt", "n_rev", "indep_over_cumulative_equal", "indep_over_cumulative_optimal", "float_int_gap"]].round(4).to_string(index=False))
        print(gt.round(1).to_string(index=False))


if __name__ == "__main__":
    main()
