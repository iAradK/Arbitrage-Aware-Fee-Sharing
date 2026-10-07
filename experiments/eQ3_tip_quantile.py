#!/usr/bin/env python3
"""Q3: which tip estimate tau_hat in the hook's K_hat (Eq. 8/9: g x (base fee + tau_hat) x ETH + R) keeps participation
violations of the unsplit ideal rule below 1% at the smallest loss of transfer? Validation months only.

Harness: experiments/eB_cross_block_split.py with N = 1 (unsplit): the executed corrections of the E2 ideal replay
(retained margin, k = 1, d = 0, median R, lambda 0.75, gamma 0.02) on ETH/USDC and ETH/WBTC (raw and 24-hour offset),
block 0 = the first chain block at or after the replay minute. The arbitrager pays 180,214 gas at that block's gas price
(base fee + the block's median priority fee); the hook's kappa uses the block's base fee plus tau_hat. Settings:
  P50, P90, P95, P99   tau_hat = that quantile of the block median priority fee over the training months
                       (fee-history blocks before 2026-04-01; P50 = 0.05 gwei is the current tau_hat)
  gasprice             E2's K_hat: the block's actual gas price (no tip error)
plus a finer quantile scan (supplementary) to locate where violations cross 1%.

A participation violation (paper's definition) is a baseline-feasible correction (full correction to the benchmark
covers R with zero transfer) that the rule leaves below R or that no longer executes. The sample and the drop rule are
eB's, so P50 and gasprice reproduce eB's N = 1 rows exactly (asserted).

  python experiments/eQ3_tip_quantile.py               # validation months; --split test is refused
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

import eA_strategic_split as eA  # noqa: E402
import eB_cross_block_split as eB  # noqa: E402
from common import cpmm, minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS, RESULTS  # noqa: E402

OUT = RESULTS / "eQ3"
REQUESTED = {"P50": 0.50, "P90": 0.90, "P95": 0.95, "P99": 0.99}
SCAN = [0.60, 0.70, 0.75, 0.80, 0.85, 0.875, 0.925, 0.975, 0.98, 0.985]
MAX_VIOLATION_RATE = 0.01


def tip_quantiles(qs) -> dict:
    b = pd.read_parquet(CACHE / "block_gas.parquet", columns=["timestamp", "tip_p50_wei", "gas_source"])
    train = (pd.DatetimeIndex(b["timestamp"]) < mg.VALID_START) & (b["gas_source"] == "fee_history").to_numpy()
    t = b.loc[train, "tip_p50_wei"]
    return {q: float(t.quantile(q)) for q in qs}


def opportunities(pool_key, variant, cfg2, lam, gam, blocks_all, split):
    """eB's ideal-rule opportunities with block-0 data (same sample and drop rule as eB.run)."""
    mech, k, d = eB.RULES["ideal"]
    g, steps, tab, R_usd, cfg_row = eB.replay(pool_key, variant, mech, k, d, split, cfg2, lam, gam)
    assert float(cfg_row["dmult"]) == 0.0
    gs = pd.DatetimeIndex(g["t"]).tz_convert("UTC").tz_localize(None).as_unit("ns")
    bts, Nmax = blocks_all["ts"], max(eB.NS)
    pref0, prefd = g["p_ref_0"].to_numpy(), g[f"p_ref_{d}"].to_numpy()
    usd_g, eth_g = g["usd_per_num"].to_numpy(), g["eth_in_num"].to_numpy()
    rows = []
    for o_ in steps[steps["executed"]].itertuples():
        t = gs[o_.i]
        bi = int(np.searchsorted(bts, t.value, "left"))
        if bi + Nmax >= len(bts):
            continue
        bidx = np.arange(bi, bi + Nmax + 1)
        mins = ((pd.DatetimeIndex(bts[bidx]).floor("min") - gs[0]) // pd.Timedelta(minutes=1)).to_numpy()
        if mins[-1] >= len(g) or mins[0] < 0:
            continue
        mb = mins[:-1]
        if not (np.isfinite(pref0[mb]).all() and np.isfinite(prefd[mb]).all() and np.isfinite(usd_g[mb]).all()):
            continue
        m0 = mb[0]
        rows.append({"t": t, "x": o_.x, "y": o_.y, "pi0": pref0[m0], "rho0": prefd[m0], "usd0": usd_g[m0],
                     "ethusd0": eth_g[m0] * usd_g[m0], "bf0": blocks_all["base_fee"][bi], "gp0": blocks_all["gas_price"][bi]})
    return pd.DataFrame(rows), R_usd


def evaluate(opps, pool_key, R_usd, kappa_settings, cfg2, lam, gam, Fint):
    pool = POOLS[pool_key]
    G, gas_u = cfg2["grid_points"], cfg2["gas_units"] + cfg2["hook_overhead_gas"]
    tol = 1e-9 * max(1.0, R_usd)
    out = []
    for o_ in opps.itertuples():
        dirn, n0 = cpmm.full_correction_net(o_.x, o_.y, pool.fee, o_.pi0)
        trf = cpmm.trade(o_.x, o_.y, pool.fee, dirn, n0)
        C0 = gas_u * o_.gp0 * 1e-18 * o_.ethusd0
        S_full = float(np.maximum(o_.pi0 * trf["d0"] + trf["d1"], 0.0)) * o_.usd0
        bfeas = bool(n0 > 0 and S_full - C0 >= R_usd - tol)
        for name, gas_price_in_kappa in kappa_settings(o_):
            kappa0 = gas_u * gas_price_in_kappa * 1e-18 * o_.ethusd0 + R_usd
            o = {"x": o_.x, "y": o_.y, "fee": pool.fee, "dirn": int(dirn), "n0": float(n0), "pi0": o_.pi0, "rho0": o_.rho0,
                 "usd0": o_.usd0, "ethusd0": o_.ethusd0, "kappa0": kappa0, "gp0": o_.gp0,
                 "unit0": float(eA.unit_usd(pool_key, o_.usd0)), "C0": C0, "cext0": 0.0}
            p = eB.plan(o, 1, G, lam, gam, R_usd, tol) if n0 > 0 else None
            e = eB.execute(o, p, None, "constant", Fint, {"first": gas_u, "extra": 0, "extra_charged": 0}, lam, gam) if p else None
            payoff = e["payoff"] if e else 0.0
            viol = bfeas and (p is None or payoff < R_usd - tol)
            out.append({"t": o_.t, "setting": name, "baseline_feasible": bfeas, "executed": p is not None,
                        "charge": e["charge"] if e else 0.0, "payoff": payoff, "violation": viol,
                        "tip_gap_gwei": (o_.gp0 - o_.bf0) / 1e9, "kappa0_usd": kappa0})
    return pd.DataFrame(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    a = ap.parse_args()
    if a.split != "valid":
        raise SystemExit("Q3 runs on the validation months only; test months are refused.")
    cfg2 = reporting.load_config(ROOT / "experiments" / "configs" / "e2.yml")
    lam, gam = cfg2["headline"]["lambda"], cfg2["headline"]["gamma"]
    Fint = eA.Fint(lam, gam)
    qs = sorted(set(REQUESTED.values()) | set(SCAN))
    tips = tip_quantiles(qs)
    assert tips[0.5] == eA.tau_hat_wei(), "P50 must be the current tau_hat"
    label = {q: next((k for k, v in REQUESTED.items() if v == q), f"q{q:g}") for q in qs}

    def settings(o_):
        return [(label[q], o_.bf0 + tips[q]) for q in qs] + [("gasprice", o_.gp0)]

    bg = pd.read_parquet(CACHE / "block_gas.parquet", columns=["block", "timestamp", "base_fee_wei", "gas_price_wei"])
    bg = bg.sort_values("block").reset_index(drop=True)
    blocks_all = {"ts": pd.DatetimeIndex(bg["timestamp"]).tz_convert("UTC").as_unit("ns").asi8, "block": bg["block"].to_numpy(),
                  "base_fee": bg["base_fee_wei"].to_numpy(float), "gas_price": bg["gas_price_wei"].to_numpy(float)}
    OUT.mkdir(parents=True, exist_ok=True)
    per, summ = [], []
    for key, var in eB.POOL_VARIANTS:
        t0 = time.time()
        opps, R_usd = opportunities(key, var, cfg2, lam, gam, blocks_all, "valid")
        ev = evaluate(opps, key, R_usd, settings, cfg2, lam, gam, Fint)
        ev.insert(0, "variant", var)
        ev.insert(0, "pool", key)
        per.append(ev)
        ref = ev[ev.setting == "gasprice"]["charge"].sum()
        p50 = ev[ev.setting == "P50"]["charge"].sum()
        for name, x in ev.groupby("setting", sort=False):
            bf = x["baseline_feasible"]
            q = next((q for q in qs if label[q] == name), None)
            summ.append({"pool": key, "variant": var, "setting": name, "requested": name in REQUESTED or name == "gasprice",
                         "quantile": q, "tau_hat_gwei": tips[q] / 1e9 if q is not None else np.nan,
                         "n_opportunities": len(x), "n_baseline_feasible": int(bf.sum()), "n_executed": int(x["executed"].sum()),
                         "violations": int(x["violation"].sum()), "violation_rate": float(x["violation"].sum() / bf.sum()),
                         "transfer_usd": x["charge"].sum(), "transfer_share_of_gasprice": x["charge"].sum() / ref,
                         "transfer_share_of_P50": x["charge"].sum() / p50, "R_usd": R_usd})
        print(f"{key} {var}: {len(opps)} opportunities in {time.time() - t0:.0f}s", flush=True)
    per = pd.concat(per, ignore_index=True)
    summ = pd.DataFrame(summ)
    assert (pd.DatetimeIndex(per["t"]) < mg.SPLIT_RANGE["valid"][1].tz_convert("UTC").tz_localize(None)).all()

    # reproduction of eB's unsplit rows (dynamic and constant coincide at N = 1)
    eb = pd.read_csv(RESULTS / "eB" / "eB_summary_valid.csv")
    eb = eb[(eb.rule == "ideal") & (eb.N == 1) & (eb["mode"] == "dynamic")]
    checks = []
    for (key, var), x in summ.groupby(["pool", "variant"]):
        for setting, kc in (("P50", "basefee"), ("gasprice", "gasprice")):
            mine = x[x.setting == setting].iloc[0]
            ref = eb[(eb.pool == key) & (eb.variant == var) & (eb.kappa == kc)].iloc[0]
            c = {"pool": key, "variant": var, "setting": setting, "transfer_usd": mine.transfer_usd, "eB_transfer_1_usd": ref.transfer_1_usd,
                 "violations": int(mine.violations), "eB_violations_unsplit": int(ref.violations_unsplit),
                 "n": int(mine.n_opportunities), "eB_n": int(ref.n_opportunities)}
            c["match"] = bool(np.isclose(c["transfer_usd"], c["eB_transfer_1_usd"], rtol=1e-12, atol=1e-9)
                              and c["violations"] == c["eB_violations_unsplit"] and c["n"] == c["eB_n"])
            assert c["match"], c
            checks.append(c)

    # recommendation among the requested quantiles: violations below 1% in every pool, smallest loss of transfer
    req = summ[summ.setting.isin(REQUESTED)]
    worst = req.groupby("setting")["violation_rate"].max()
    ok = [s for s in REQUESTED if worst[s] < MAX_VIOLATION_RATE]
    rec = min(ok, key=lambda s: REQUESTED[s]) if ok else None    # transfer falls with tau_hat: the lowest passing quantile
    scan = summ[summ["quantile"].notna()].groupby("quantile")["violation_rate"].max()
    lowest_scan = float(scan[scan < MAX_VIOLATION_RATE].index.min()) if (scan < MAX_VIOLATION_RATE).any() else None
    recd = {"criterion": f"max violation rate over pools < {MAX_VIOLATION_RATE:.0%}, then the smallest loss of transfer",
            "recommended_requested_quantile": rec, "worst_violation_rate_by_requested_setting": worst.to_dict(),
            "lowest_passing_quantile_in_scan": lowest_scan}
    print(json.dumps(recd, indent=1))

    per.to_csv(OUT / "eQ3_opportunities_valid.csv.gz", index=False)
    summ.to_csv(OUT / "eQ3_summary_valid.csv", index=False)
    pd.DataFrame(checks).to_csv(OUT / "eQ3_checks_valid.csv", index=False)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
    inputs = ([CACHE / "aligned" / f"{k_}.parquet" for k_, _ in eB.POOL_VARIANTS] + [CACHE / "block_gas.parquet"]
              + [RESULTS / "e1" / f"depth_{k_}.parquet" for k_, _ in eB.POOL_VARIANTS]
              + sorted((ROOT / "data" / "data" / "binance").glob("*_1m.csv.gz"))
              + [RESULTS / "eB" / "eB_summary_valid.csv", Path(__file__).resolve(), ROOT / "experiments" / "eB_cross_block_split.py"])
    m = {"experiment": "eQ3", "split": "valid", "git_commit": head, "uncommitted": dirty,
         "note": "data/data/binance/*_1m.csv.gz are the main checkout's current klines (uncommitted there); hashes listed",
         "linked_from_main_checkout": ["cache", "results/e1", "results/e2", "data/data/lido"],
         "inputs": {Path(p).relative_to(ROOT).as_posix(): eB.sha(p) for p in dict.fromkeys(inputs)},
         "parameters": {"rule": "ideal (retained, k=1, d=0)", "lambda": lam, "gamma": gam, "R": "median regime",
                        "tip_quantiles_training_wei": {label[q]: tips[q] for q in qs}, "kappa_gas_units": cfg2["gas_units"] + cfg2["hook_overhead_gas"],
                        "max_violation_rate": MAX_VIOLATION_RATE, "e2_config_sha256": reporting.config_hash(cfg2)},
         "checks_vs_eB": checks, "recommendation": recd,
         "outputs": {p.name: eB.sha(p) for p in sorted(OUT.glob("eQ3_*_valid*"))}}
    (OUT / "manifest_valid.json").write_text(json.dumps(m, indent=1, default=str))
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(summ.round(4).to_string(index=False))


if __name__ == "__main__":
    main()
