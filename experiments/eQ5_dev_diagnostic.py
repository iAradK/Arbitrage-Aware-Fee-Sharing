"""Q5 diagnostic (python experiments/eQ5_dev_diagnostic.py): why a buffer that rises with the scope-opening deviation
dev = |log(P_pool / P_hat)| cuts Experiment B's unsplit transfer far more than it cuts tipped violations. Validation months.
For two populations, the distribution of dev and how the signed relative overestimation (P_hat - P) / P_hat, in the
direction of the trade, moves with it:
  replay    Experiment B's opportunities (buffered rule, k = 15, d = 1; as eQ5_buffer_calibration.crossblock), weighted
            by count and by the stored p95 unsplit charge (Q4 prop_rel, dynamic, N = 1);
  observed  Experiment C's standalone-feasible swaps, all and the p95 tipped violations (Q4 prop_rel V1 charges).
Read-only; reuses the committed Q5 module. Writes results/eQ5/eQ5_dev_diagnostic_valid.csv."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

W = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(W))
sys.path.insert(0, str(W / "experiments"))
import eQ5_buffer_calibration as q5  # noqa: E402
import e2_sequential_replay as e2  # noqa: E402
import eC_netting_variants as q2  # noqa: E402
import eC_same_block_replay as eC  # noqa: E402
import eQ4_splitting as q4s  # noqa: E402
from common import cpmm, minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS, RESULTS  # noqa: E402

cfg2 = reporting.load_config(W / "experiments" / "configs" / "e2.yml")
lam, gam = cfg2["headline"]["lambda"], cfg2["headline"]["gamma"]
q4cb = pd.read_csv(RESULTS / "eQ4" / "eQ4_crossblock_opportunities_valid.csv.gz", float_precision="round_trip")
q4ch = pd.read_csv(RESULTS / "eQ4" / "eQ4_swaps_charges_valid.csv.gz", float_precision="round_trip")
sw = pd.read_csv(RESULTS / "eC" / "eC_swaps_valid.csv.gz", float_precision="round_trip")
bg = pd.read_parquet(CACHE / "block_gas.parquet", columns=["block", "timestamp"]).sort_values("block")
bts = pd.DatetimeIndex(bg["timestamp"]).tz_convert("UTC").as_unit("ns").asi8
QS = (0.25, 0.5, 0.75, 0.9)


def wq(v, w, q):
    o = np.argsort(v)
    c = np.cumsum(w[o]) / w.sum()
    return float(v[o][np.searchsorted(c, q)])


def describe(pop, key, var, dev, over, w=None):
    w = np.ones(len(dev)) if w is None else w
    r = {"pool": key, "variant": var, "population": pop, "n": int(len(dev)), "weight_sum": float(w.sum())}
    r.update({f"dev_q{q:g}_bp": 1e4 * wq(dev, w, q) for q in QS})
    r["corr_dev_over"] = float(np.corrcoef(dev, over)[0, 1]) if len(dev) > 2 else np.nan
    for lo, hi in [(0, 10), (10, 20), (20, 30), (30, 50), (50, 1e9)]:
        m = (dev * 1e4 >= lo) & (dev * 1e4 < hi)
        r[f"wshare_dev_{lo}_{hi if hi < 1e9 else 'inf'}bp"] = float(w[m].sum() / w.sum())
        r[f"over_p95_dev_{lo}_{hi if hi < 1e9 else 'inf'}bp"] = 1e4 * float(np.quantile(over[m], 0.95)) if m.sum() >= 20 else np.nan
    return r


rows = []
for key, var in q5.POOL_VARIANTS:
    pool = POOLS[key]
    # ---- replay: the opportunities of crossblock(), with their scope-opening deviation and overestimation
    mech, k, d = q4s.RULE
    C_all = e2.make_configs(cfg2)
    j = C_all.index[(C_all["mech"] == mech) & (C_all["lam"] == lam) & (C_all["gamma"] == gam)][0]
    C = C_all.loc[[0, j]].reset_index(drop=True)
    R_usd = mg.r_regimes_usd(key, var, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
    seed = q5.seed_candidates(key, var, R_usd, d, k, e2.PRIOR["valid"], cfg2)
    seed_e2 = pd.DataFrame({"t": seed["t_utc"], "err_usd": seed["err_usd"], "err_signed_usd": seed["err_signed_usd"]})
    g = mg.build_grid(key, "valid", var, lags=(0, d))
    r, act, pp = q5.recorded_simulate(g, key, C, cfg2, R_usd, d, k, seed_e2)
    xy = q5.recorded_simulate.xy
    gs = pd.DatetimeIndex(g["t"]).tz_convert("UTC").tz_localize(None).as_unit("ns")
    ex = act[r["step_exec"][act, 1]]
    pref0, prefd = g["p_ref_0"].to_numpy(), g[f"p_ref_{d}"].to_numpy()
    rep = []
    for i in ex:
        t = gs[i]
        bi = int(np.searchsorted(bts, t.value, "left"))
        m0 = int((pd.Timestamp(bts[bi]).floor("min") - gs[0]) // pd.Timedelta(minutes=1)) if bi < len(bts) else -1
        if m0 < 0 or m0 >= len(g):
            continue
        pi, rho = pref0[m0], prefd[m0]
        x0, y0 = float(xy[int(i)][0][1]), float(xy[int(i)][1][1])
        dirn, n0 = cpmm.full_correction_net(x0, y0, pool.fee, pi)
        rep.append({"t": t, "dev": abs(np.log((y0 / x0) / rho)), "over": int(dirn) * (rho - pi) / rho})
    rep = pd.DataFrame(rep)
    rep["t"] = rep["t"].astype("datetime64[ns]")
    st = q4cb[(q4cb.pool == key) & (q4cb.variant == var) & (q4cb.rule == "prop_rel") & (q4cb["mode"] == "dynamic") & (q4cb.N == 1)]
    st = st.assign(t=pd.to_datetime(st["t"], utc=True).dt.tz_convert(None).astype("datetime64[ns]"))
    x = st[["t", "charge_1"]].merge(rep, on="t", how="left", validate="one_to_one")
    assert x["dev"].notna().all(), (key, var, "replay opportunities not matched")
    ch = x[x.charge_1 > 0]
    rows.append(describe("replay_all", key, var, x.dev.to_numpy(), x.over.to_numpy()))
    rows.append(describe("replay_charged_count", key, var, ch.dev.to_numpy(), ch.over.to_numpy()))
    rows.append(describe("replay_charged_charge_weighted", key, var, ch.dev.to_numpy(), ch.over.to_numpy(), ch.charge_1.to_numpy()))
    # ---- observed swaps: the block's scope-opening deviation (first swap of the block), the swap's overestimation
    al = eC.load_swaps(key, var, "valid")
    df = sw[(sw.pool == key) & (sw.variant == var) & (sw.setting == "buffered_eps_d1")].reset_index(drop=True)
    c4 = q4ch[(q4ch.pool == key) & (q4ch.variant == var) & (q4ch.rule == "prop_rel")].reset_index(drop=True)
    assert (df.block.to_numpy() == al.block.to_numpy()).all() and (c4.block.to_numpy() == al.block.to_numpy()).all()
    dev_sw = np.abs(np.log(al["price_pre"] / al["pi_hook"])).to_numpy()
    first = al.groupby("block").cumcount().to_numpy() == 0
    dev_open = pd.Series(np.where(first, dev_sw, np.nan)).groupby(al["block"].to_numpy()).transform("first").to_numpy()
    sgn = -np.sign(al["amount0"].to_numpy())                     # +1 = the swapper receives the base token
    over = sgn * (al["pi_hook"] - al["pi_bench"]).to_numpy() / al["pi_hook"].to_numpy()
    M = df["margin_usd"].to_numpy()
    feas = (M >= 0) & np.isfinite(dev_open) & np.isfinite(over)
    tip = feas & (c4["r_V1_usd"].to_numpy() > M)
    rows.append(describe("observed_feasible", key, var, dev_open[feas], over[feas]))
    rows.append(describe("observed_tipped_p95", key, var, dev_open[tip], over[tip]))
    chg = feas & (c4["r_V1_usd"].to_numpy() > 0) & (al["block"].to_numpy() != q2.EVENT_BLOCK)
    if chg.sum() > 2:
        rows.append(describe("observed_charged_ex_event_charge_weighted", key, var, dev_open[chg], over[chg], c4["r_V1_usd"].to_numpy()[chg]))
    print(key, var, "done", flush=True)

out = pd.DataFrame(rows)
out.to_csv(RESULTS / "eQ5" / "eQ5_dev_diagnostic_valid.csv", index=False)
with pd.option_context("display.width", 300, "display.max_columns", 40):
    print(out.round(3).to_string(index=False))
json.dump({"git_commit": reporting.git_commit(), "script": "experiments/eQ5_dev_diagnostic.py"},
          open(RESULTS / "eQ5" / "manifest_dev_diagnostic_valid.json", "w"), indent=1)
