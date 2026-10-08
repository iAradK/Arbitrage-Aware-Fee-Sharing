#!/usr/bin/env python3
"""New validation numbers of a rerun (results/<RESULTS_RUN>, the final rule) next to the stored ones, one table per
experiment; headline changes above 10% are flagged (a stored 0 is flagged when the new value exceeds a small absolute
tolerance). Also lists every result file of the rerun with the commit its manifest records. Writes
results/<RESULTS_RUN>/comparison/*.csv and prints the tables.

  RESULTS_RUN=final_valid python experiments/compare_final_valid.py --stored-root <checkout holding the stored results/>
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common import reporting  # noqa: E402

_ap = argparse.ArgumentParser()
_ap.add_argument("--stored-root", required=True, help="checkout whose results/ holds the stored validation results")
OLD = Path(_ap.parse_args().stored_root) / "results"
assert reporting.RESULTS_RUN, "set RESULTS_RUN to the rerun's output root"
NEW = reporting.run_root()
OUT = NEW / "comparison"
OUT.mkdir(exist_ok=True)

PRIMARY = {"eth_usdc_005": "raw", "eth_wbtc_030": "corr24h", "eth_wsteth_001": "corr24h"}
SHORT = {"eth_usdc_005": "ETH/USDC", "eth_wbtc_030": "ETH/WBTC", "eth_wsteth_001": "ETH/wstETH"}
tables = {}


def flag(old, new, headline=True, abs_tol=0.0):
    if not headline or pd.isna(old) or pd.isna(new):
        return ""
    if old == 0:
        return "FLAG" if abs(new) > abs_tol else ""
    return "FLAG" if abs(new - old) / abs(old) > 0.10 else ""


def emit(name, df):
    df.to_csv(OUT / f"{name}.csv", index=False)
    tables[name] = df


def pair(rows, label, old, new, headline=True, fmt="{:.4g}", abs_tol=0.0):
    ch = (new - old) / abs(old) if (old not in (0, None) and pd.notna(old) and pd.notna(new)) else np.nan
    rows.append({"quantity": label, "stored": old, "new": new, "rel_change": ch, "flag": flag(old, new, headline, abs_tol)})


# ---------------------------------------------------------------- E2
def e2():
    rows = []
    for tag, pools in (("valid", list(PRIMARY)), ("valid_lag1_k15_med", ["eth_usdc_005"]), ("valid_lag5_k15_med", ["eth_usdc_005"]),
                       ("valid_lag1_med", ["eth_usdc_005"]), ("valid_lag5_med", ["eth_usdc_005"])):
        o, n = pd.read_parquet(OLD / "e2" / f"e2_summary_{tag}.parquet"), pd.read_parquet(NEW / "e2" / f"e2_summary_{tag}.parquet")
        for pool in pools:
            for mech, lam, gam in (("baseline", 0, 0), ("static_0.05pct", 0, 0), ("static_0.30pct", 0, 0), ("unconstrained", .75, 0),
                                   ("cap_gamma0", .75, 0), ("retained", .75, .02), ("buffered_1eps", .75, .02), ("buffered_2eps", .75, .02)):
                sel = lambda d: d[(d.pool == pool) & (d.variant == PRIMARY[pool]) & (d.regime == "median") & (d.mech == mech)
                                  & np.isclose(d.lam, lam) & np.isclose(d.gamma, gam)]
                a, b = sel(o), sel(n)
                if not len(a) or not len(b):
                    continue
                a, b = a.iloc[0], b.iloc[0]
                for q in ("protection_usd", "recapture_rate", "execution_rate", "participation_violation_rate", "etw_mean"):
                    if mech == "baseline" and q in ("protection_usd", "recapture_rate"):
                        continue
                    pair(rows, f"{tag} {SHORT[pool]} {mech} {q}", a[q], b[q], headline=q != "etw_mean" or mech in ("baseline", "retained"),
                         abs_tol=0.005 if q == "participation_violation_rate" else 0.0)
    emit("e2_headline", pd.DataFrame(rows))
    # lambda sweep and B.4 grid (ETH/USDC, ideal, median regime)
    rows = []
    o, n = pd.read_parquet(OLD / "e2" / "e2_summary_valid.parquet"), pd.read_parquet(NEW / "e2" / "e2_summary_valid.parquet")
    for mech in ("retained", "buffered_1eps", "buffered_2eps"):
        for lam in (0.25, 0.5, 0.75, 0.95):
            for gam in (0.005, 0.02, 0.05, 0.10):
                sel = lambda d: d[(d.pool == "eth_usdc_005") & (d.regime == "median") & (d.mech == mech) & np.isclose(d.lam, lam) & np.isclose(d.gamma, gam)]
                pair(rows, f"ETH/USDC {mech} lam {lam} gamma {gam} recapture", sel(o).iloc[0]["recapture_rate"], sel(n).iloc[0]["recapture_rate"])
    emit("e2_lambda_gamma_grid", pd.DataFrame(rows))
    # B.9: three regimes, retained and buffered_1eps at the headline
    rows = []
    for pool in PRIMARY:
        for rg in ("low", "median", "high"):
            for mech in ("retained", "buffered_1eps", "cap_gamma0"):
                g = 0.02 if mech != "cap_gamma0" else 0.0
                sel = lambda d: d[(d.pool == pool) & (d.variant == PRIMARY[pool]) & (d.regime == rg) & (d.mech == mech) & np.isclose(d.lam, .75) & np.isclose(d.gamma, g)]
                a, b = sel(o).iloc[0], sel(n).iloc[0]
                pair(rows, f"{SHORT[pool]} R={rg} {mech} protection_usd", a["protection_usd"], b["protection_usd"])
                pair(rows, f"{SHORT[pool]} R={rg} {mech} violation_rate", a["participation_violation_rate"], b["participation_violation_rate"], abs_tol=0.005)
    emit("e2_B9_regimes", pd.DataFrame(rows))
    # B.8 hook-free comparison
    rows = []
    a = pd.read_csv(OLD / "e2" / "tables" / "e2_hook_gas_valid_lag0_med.csv")
    b = pd.read_csv(NEW / "e2" / "tables" / "e2_hook_gas_valid.csv")
    num = [c for c in a.columns if c in b.columns and pd.api.types.is_numeric_dtype(a[c])]
    for pool in a["pool"].unique():
        ar, br = a[a.pool == pool].iloc[0], b[b.pool == pool]
        if not len(br):
            continue
        br = br.iloc[0]
        for c in num:
            pair(rows, f"{SHORT.get(pool, pool)} {c}", ar[c], br[c], headline=c.startswith(("X", "etw_ratio", "share")))
    emit("e2_B8_hook_free", pd.DataFrame(rows))
    # B.5 ex-post margin: stored only for the test months
    b = pd.read_csv(NEW / "e2" / "tables" / "e2_recoverable_margin_valid.csv")
    emit("e2_B5_margin_new_only", b)


# ---------------------------------------------------------------- E8
def e8():
    rows = []
    for tag in ("valid", "valid_lag1_k15_med", "valid_lag5_k15_med"):
        o, n = pd.read_parquet(OLD / "e8" / f"e8_summary_{tag}.parquet"), pd.read_parquet(NEW / "e8" / f"e8_summary_{tag}.parquet")
        for pool in PRIMARY:
            for mech in ("retained", "retained+settle", "buffered_1eps", "buffered_1eps+settle", "buffered_1eps+1s", "dynfee_cal",
                         "mevtax_t0.8", "mevtax_t5.7"):
                sel = lambda d: d[(d.pool == pool) & (d.regime == "median") & (d.mech == mech)]
                a, b = sel(o), sel(n)
                if not len(a) or not len(b):
                    continue
                a, b = a.iloc[0], b.iloc[0]
                pair(rows, f"{tag} {SHORT[pool]} {mech} protection_usd", a["protection_usd"], b["protection_usd"])
                pair(rows, f"{tag} {SHORT[pool]} {mech} violation_rate", a["participation_violation_rate"], b["participation_violation_rate"], abs_tol=0.005)
                pair(rows, f"{tag} {SHORT[pool]} {mech} etw_mean", a["etw_mean"], b["etw_mean"], headline=mech == "dynfee_cal")
    co, cn = json.load(open(OLD / "e8" / "dynfee_calibration.json")), json.load(open(NEW / "e8" / "dynfee_calibration.json"))
    for pool in PRIMARY:
        pair(rows, f"dynfee beta {SHORT[pool]}", co["pools"][pool]["beta"], cn["pools"][pool]["beta"])
    emit("e8", pd.DataFrame(rows))
    emit("e8_concentration_new_only", pd.read_csv(NEW / "e8" / "tables" / "e8_concentration_valid.csv"))


# ---------------------------------------------------------------- E4
def e4():
    o, n = pd.read_csv(OLD / "e4" / "e4_summary_valid.csv"), pd.read_csv(NEW / "e4" / "e4_summary_valid.csv")
    rows = []
    for k in (1, 15):
        for d in (0, 1, 5, 60):
            for dk in ("0", "eps_S", "eps_S+eps_K"):
                sel = lambda x: x[(x.pool == "eth_usdc_005") & (x.variant == "raw") & (x.scenario == f"lag{d}" + ("" if k == 1 else f"_k{k}"))
                                  & (x.calibration == "rolling") & np.isclose(x.eta, 0) & (x.delta == dk)]
                a, b = sel(o), sel(n)
                if not len(a) or not len(b):
                    continue
                a, b = a.iloc[0], b.iloc[0]
                pair(rows, f"ETH/USDC k={k} d={d} buffer {dk} violation_rate", a["violation_rate"], b["violation_rate"], abs_tol=0.005)
                pair(rows, f"ETH/USDC k={k} d={d} buffer {dk} recapture_effective", a["recapture_effective"], b["recapture_effective"])
    for eta in (-0.5, 0.5):
        for dk in ("0", "eps_S+eps_K"):
            sel = lambda x: x[(x.pool == "eth_usdc_005") & (x.scenario == "lag5") & (x.calibration == "rolling") & np.isclose(x.eta, eta) & (x.delta == dk)]
            a, b = sel(o).iloc[0], sel(n).iloc[0]
            pair(rows, f"ETH/USDC eta={eta} d=5 buffer {dk} violation_rate", a["violation_rate"], b["violation_rate"], abs_tol=0.005)
            pair(rows, f"ETH/USDC eta={eta} d=5 buffer {dk} recapture_effective", a["recapture_effective"], b["recapture_effective"])
    for A in (60, 300, 3600):
        for cond in ("all", "valid_price"):
            sel = lambda x: x[(x.scenario == f"pyth_onchain_amax{A}") & (x.calibration == "rolling") & np.isclose(x.eta, 0) & (x.delta == "eps_S")
                              & (x.conditioning == cond)]
            a, b = sel(o), sel(n)
            if len(a) and len(b):
                pair(rows, f"Pyth A_max={A}s {cond} eps buffer violation_rate", a.iloc[0]["violation_rate"], b.iloc[0]["violation_rate"], abs_tol=0.005)
                pair(rows, f"Pyth A_max={A}s {cond} eps buffer recapture_effective", a.iloc[0]["recapture_effective"], b.iloc[0]["recapture_effective"])
    for k in (1, 15):                                   # eps fitted once on validation, per d
        for d in (1, 5, 10, 30, 60):
            sel = lambda x: x[(x.pool == "eth_usdc_005") & (x.variant == "raw") & (x.scenario == f"lag{d}" + ("" if k == 1 else f"_k{k}"))
                              & (x.calibration == "valid (in-sample)") & np.isclose(x.eta, 0) & (x.delta == "eps_S")]
            a, b = sel(o), sel(n)
            if len(a) and len(b):
                pair(rows, f"ETH/USDC k={k} d={d} eps fitted once on validation: violation_rate", a.iloc[0]["violation_rate"], b.iloc[0]["violation_rate"], abs_tol=0.005)
                pair(rows, f"ETH/USDC k={k} d={d} eps fitted once on validation: recapture_effective", a.iloc[0]["recapture_effective"], b.iloc[0]["recapture_effective"])
    for pool, var in (("eth_wbtc_030", "corr24h"), ("eth_wsteth_001", "corr24h")):
        for d in (1, 5, 60):
            for dk in ("0", "eps_S"):
                sel = lambda x: x[(x.pool == pool) & (x.variant == var) & (x.scenario == f"lag{d}") & (x.calibration == "rolling")
                                  & np.isclose(x.eta, 0) & (x.delta == dk)]
                a, b = sel(o), sel(n)
                if len(a) and len(b):
                    pair(rows, f"{SHORT[pool]} k=1 d={d} buffer {dk} violation_rate", a.iloc[0]["violation_rate"], b.iloc[0]["violation_rate"], abs_tol=0.005)
                    pair(rows, f"{SHORT[pool]} k=1 d={d} buffer {dk} recapture_effective", a.iloc[0]["recapture_effective"], b.iloc[0]["recapture_effective"])
    sel = lambda x: x[(x.scenario == "pyth_onchain") & (x.calibration == "rolling") & np.isclose(x.eta, 0) & (x.delta == "eps_S")]
    a, b = sel(o), sel(n)
    if len(a) and len(b):
        pair(rows, "Pyth, no staleness check, eps buffer: violation_rate", a.iloc[0]["violation_rate"], b.iloc[0]["violation_rate"], abs_tol=0.005)
        pair(rows, "Pyth, no staleness check, eps buffer: recapture_effective", a.iloc[0]["recapture_effective"], b.iloc[0]["recapture_effective"])
    emit("e4", pd.DataFrame(rows))


# ---------------------------------------------------------------- E5
def e5():
    o, n = pd.read_parquet(OLD / "e5" / "e5_summary.parquet"), pd.read_parquet(NEW / "e5" / "e5_summary.parquet")
    rows = []
    for pool in PRIMARY:
        for mode in ("gas_only", "gas_only_buf", "exact"):
            for gam in (0.0, 0.02, 0.05):
                sel = lambda x: x[(x.pool == pool) & (x.gas_units == 150000) & (x.bps_rank == 1) & np.isclose(x.curvature, 0.5) & np.isclose(x.lam, 0.75)
                                  & (x.k_hat == mode) & np.isclose(x.gamma, gam)]
                a, b = sel(o), sel(n)
                if not len(a) or not len(b):
                    continue
                a, b = a.iloc[0], b.iloc[0]
                for q in ("q_ratio_mean", "surplus_ratio_mean", "baseline_optimum_kept", "execution_rate", "recapture"):
                    pair(rows, f"{SHORT[pool]} {mode} gamma {gam} {q}", a[q], b[q])
    emit("e5", pd.DataFrame(rows))


# ---------------------------------------------------------------- E6
def e6():
    o, n = pd.read_csv(OLD / "e6" / "tables" / "e6_checks_valid.csv"), pd.read_csv(NEW / "e6" / "tables" / "e6_checks_valid.csv")
    rows = []
    for pool in PRIMARY:
        for var in ("raw",):
            for nf in (2, 4, 8, 16):
                sel = lambda x: x[(x.pool == pool) & (x.variant == var) & (x.n_fragments == nf)]
                a, b = sel(o), sel(n)
                if not len(a) or not len(b):
                    continue
                a, b = a.iloc[0], b.iloc[0]
                pair(rows, f"{SHORT[pool]} n={nf} indep_over_cumulative_equal (Fig. 4d, no buffer)", a["indep_over_cumulative_equal"], b["indep_over_cumulative_equal"])
                if "bs_indep_over_unsplit_equal" in b:
                    pair(rows, f"{SHORT[pool]} n={nf} independent rule with the final buffer / unsplit", np.nan, b["bs_indep_over_unsplit_equal"], headline=False)
                    for c in ("bs_n_charged_unsplit", "bs_one_tx_ne_unsplit", "bs_txs_below_unsplit", "bs_txs_equal_unsplit", "bs_txs_above_unsplit",
                              "bs_txs_max_shortfall_wei", "bs_txs_over_unsplit_mean", "bs_txs_over_unsplit_min", "bs_rev_ne_F", "bs_rev_negative"):
                        pair(rows, f"{SHORT[pool]} n={nf} {c}", np.nan, b[c], headline=False)
                for c in ("mono_exact_fail", "opt_exact_fail", "rev_max_fail", "rev_negative_charge", "cor1_fail"):
                    pair(rows, f"{SHORT[pool]} n={nf} {c}", a[c], b[c], headline=False)
    a, b = pd.read_csv(OLD / "e6" / "tables" / "e6_gas_penalty_valid.csv"), pd.read_csv(NEW / "e6" / "tables" / "e6_gas_penalty_valid.csv")
    for nf in a["n_fragments"]:
        pair(rows, f"cumulative gas n={nf} (k)", a[a.n_fragments == nf].cumulative_gas_k.iloc[0], b[b.n_fragments == nf].cumulative_gas_k.iloc[0])
    emit("e6", pd.DataFrame(rows))


# ---------------------------------------------------------------- E7 and B.10
def e7():
    rows = []
    a, b = pd.read_csv(OLD / "e7" / "tables" / "e7_smin_share_valid.csv"), pd.read_csv(NEW / "e7" / "tables" / "e7_smin_share_valid.csv")
    for pool in PRIMARY:
        ar, br = a[a.pool == pool].iloc[0], b[b.pool == pool].iloc[0]
        pair(rows, f"B.10 {SHORT[pool]} share S >= S_min (with buffer)", ar["share_S_ge_Smin"], br["share_S_ge_Smin"], abs_tol=0.005)
        pair(rows, f"B.10 {SHORT[pool]} share S >= S_min (no buffer)", ar["share_S_ge_Smin_delta0"], br["share_S_ge_Smin_delta0"], abs_tol=0.005)
        pair(rows, f"B.10 {SHORT[pool]} median S_min (K_hat) USD", ar["median_S_min_usd"], br["median_S_min_usd"])
    a, b = pd.read_csv(OLD / "e7" / "tables" / "e7_smin_gas_conditions.csv"), pd.read_csv(NEW / "e7" / "tables" / "e7_smin_gas_conditions.csv")
    for q in ("P50", "P99"):
        ar, br = a[(a.pool == "eth_usdc_005") & (a.gas_quantile == q)].iloc[0], b[(b.pool == "eth_usdc_005") & (b.gas_quantile == q)].iloc[0]
        pair(rows, f"B.10 ETH/USDC {q} C_gas_hat USD (kappa gas part)", ar["C_gas_usd"], br["C_gas_usd"])
        pair(rows, f"B.10 ETH/USDC {q} S_min without buffer USD", ar["S_min_delta0_usd"], br["S_min_delta0_usd"])
        pair(rows, f"B.10 ETH/USDC {q} hook gas USD at actual price", ar["hook_gas_actual_usd"], br["hook_gas_actual_usd"])
    fg = NEW / "e7" / "tables" / "e7_gas_fragments_block_scope.csv"
    if fg.exists():
        b = pd.read_csv(fg)
        a = pd.read_csv(OLD / "e7" / "tables" / "e7_gas_isolated.csv")
        for nf in (1, 2, 4, 8, 16):
            old = float(a[a.n_fragments == nf].hook_overhead_gas_total.iloc[0]) if nf in set(a.n_fragments) else np.nan
            new = float(b[(b.layout == "tx") & (b.n == nf)].hook_overhead_gas_total.iloc[0]) if ((b.layout == "tx") & (b.n == nf)).any() else np.nan
            pair(rows, f"E7 hook gas, {nf} fragments in one transaction", old, new)
        for lay in ("txs", "blocks"):
            for nf in (2, 16):
                r = b[(b.layout == lay) & (b.n == nf)]
                if len(r):
                    pair(rows, f"E7 hook gas, {nf} fragments, layout {lay} (new only)", np.nan, float(r.hook_overhead_gas_total.iloc[0]), headline=False)
    cb = NEW / "e6" / "tables" / "e6_cross_block_split_valid.csv"
    ct = OLD / "e6" / "tables" / "e6_cross_tx_split_test.csv"
    if cb.exists():
        b = pd.read_csv(cb).iloc[0]
        a = pd.read_csv(ct).iloc[0] if ct.exists() else None
        for c in ("n", "c_median_usd", "a_median_usd", "saving_median_usd", "saving_p95_usd", "extra_tx_cost_median_usd", "net_median_usd",
                  "share_net_positive", "net_median_a_ge_2c_usd"):
            pair(rows, f"B.10 split cost {c} (stored: cross-tx, TEST months; new: cross-block, valid)", a[c] if a is not None and c in a else np.nan, b[c], headline=False)
    emit("e7_b10", pd.DataFrame(rows))


def d0_check():
    """At d = 0 the reference is exact, eps = 0, so every buffered row must equal the retained margin's row (same pool,
    variant, regime, lambda, gamma) in every metric."""
    n = pd.read_parquet(NEW / "e2" / "e2_summary_valid.parquet")
    key = ["pool", "variant", "regime", "lam", "gamma"]
    ret = n[n.mech == "retained"].set_index(key)
    cols = [c for c in n.columns if c not in key + ["mech", "dmult", "phi", "eps_S_usd", "eps_S_used_median_usd", "eps_rel_median_bp"]
            and pd.api.types.is_numeric_dtype(n[c])]
    rows = []
    for mech in ("buffered_1eps", "buffered_2eps"):
        b = n[n.mech == mech].set_index(key)
        m = b[cols].join(ret[cols], rsuffix="_ret", how="inner")
        same = np.all([np.array_equal(m[c].to_numpy(), m[c + "_ret"].to_numpy(), equal_nan=True) for c in cols], axis=0)
        for (pool, var), g in m.groupby(level=["pool", "variant"]):
            eq = all(np.array_equal(g[c].to_numpy(), g[c + "_ret"].to_numpy(), equal_nan=True) for c in cols)
            rows.append({"mech": mech, "pool": pool, "variant": var, "rows": len(g), "identical_to_retained": eq,
                         "median_eps_rel_bp": float(n[(n.mech == mech) & (n.pool == pool) & (n.variant == var)]["eps_rel_median_bp"].median())})
        del same
    emit("e2_d0_buffered_equals_retained", pd.DataFrame(rows))


def eps_reference():
    s = pd.read_csv(NEW / "eps_reference" / "tables" / "eps_ref_summary_valid.csv")
    c = pd.read_csv(NEW / "eps_reference" / "eps_ref_coverage_valid.csv")
    cv = c.pivot_table(index=["pool", "variant", "reference"], columns="query_set", values="coverage").reset_index()
    emit("eps_reference", s.merge(cv, on=["pool", "variant", "reference"], how="left"))


def file_list():
    """Every file of the rerun with the commit of the manifest that wrote it: the tagged manifest whose tag is the longest
    part of the file name, else manifest_<split>.json, else the folder's manifest.json; gas_cold.csv: contracts_commit."""
    rows = []
    for f in sorted(p for p in NEW.rglob("*") if p.is_file()):
        rel = f.relative_to(NEW)
        if rel.parts[0] == "comparison" or f.name.startswith("manifest"):
            continue
        exp = NEW / rel.parts[0]
        commit, how = "", ""
        if f.name == "gas_cold.csv":
            commit, how = ";".join(sorted(set(pd.read_csv(f)["contracts_commit"].astype(str)))), "contracts_commit column"
        else:
            best = None
            for m in exp.glob("manifest_*.json"):
                tag = m.stem[len("manifest_"):]
                if tag and tag in f.stem and (best is None or len(tag) > len(best[0])):
                    best = (tag, m)
            m = best[1] if best else exp / "manifest.json"
            if m.exists():
                commit, how = json.load(open(m)).get("git_commit", ""), m.name
        rows.append({"file": rel.as_posix(), "commit": commit, "manifest": how})
    emit("file_list", pd.DataFrame(rows))


for f in (eps_reference, d0_check, e2, e8, e4, e5, e6, e7, file_list):
    try:
        f()
    except Exception as ex:                                   # noqa: BLE001
        print(f"## {f.__name__}: not compared ({ex!r})")
with pd.option_context("display.width", 250, "display.max_rows", 2000, "display.max_colwidth", 120):
    for name, df in tables.items():
        nf = int((df.get("flag", pd.Series(dtype=str)) == "FLAG").sum()) if "flag" in df else 0
        print(f"\n## {name} ({len(df)} rows, {nf} flagged)")
        print(df.to_string(index=False, float_format=lambda v: f"{v:.4g}"))
