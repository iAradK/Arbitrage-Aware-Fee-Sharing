#!/usr/bin/env python3
"""E5: strategic robustness to size-dependent costs, C(q) = c0 + c1*N + c2*N^2 (N = trade notional, USD).

Theorem 2 needs a constant cost; this measures how far the trade-selection guarantee degrades. For each sampled
scenario (a train minute's pool state and depth, a reference shock drawn from train reference returns) the searcher's
baseline best response q0 maximises S - C(q); the post-transfer best response q_BR maximises S - C(q) - r_hat(q)
subject to payoff >= R (smallest correction on ties). c1 and c2 are assumed sensitivity ranges, not fitted values.

  python experiments/e5_size_dependent_costs.py          # train data only
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import cpmm, minutegrid as mg, plotstyle, reporting  # noqa: E402
from common.mechanism import argmax_smallest  # noqa: E402
from common.plotstyle import POOL_LABEL  # noqa: E402
from common.pools import CACHE, POOLS  # noqa: E402

CFG = ROOT / "experiments" / "configs" / "e5.yml"


def sample_scenarios(pool_key: str, cfg: dict, rng: np.random.Generator) -> pd.DataFrame:
    g = mg.build_grid(pool_key, "train", cfg["variant"], lags=(0,))
    ok = g["L"].notna() & g["p_ref_0"].notna() & g["gas_wei"].notna()
    g = g[ok].reset_index(drop=True)
    ref = g["p_ref_0"].to_numpy()
    n = cfg["n_scenarios"]
    hs = np.array(cfg["shock_horizons_min"][pool_key])
    i = rng.integers(1500, len(g), n)                               # state minute (needs history for the shock draw)
    if cfg["shock_source"][pool_key] == "pre_dev":
        al = mg.load_aligned(pool_key)
        pdv = al.loc[al["split"] == "train", "pre_dev"].dropna().to_numpy()
        big = np.abs(pdv) >= cfg["min_abs_shock_fee_multiple"] * POOLS[pool_key].fee
        # drop deviations of swaps that pushed the thin pool far out of range (log deviations up to 18 in train); applied to a
        # pool at normal depth they produce multi-million-USD surpluses (v1 bug, DECISIONS V3)
        sane = np.abs(pdv) <= cfg.get("max_abs_pre_dev", np.inf)
        keep = -pdv[big & sane]                                        # reference relative to the pool
    else:
        m = 60 * n
        u = rng.integers(1500, len(g), m)                           # independent minutes used for the shock pool
        h = rng.choice(hs, m)
        cand = np.log(ref[u] / ref[np.maximum(u - h, 0)])
        keep = cand[np.abs(cand) >= cfg["min_abs_shock_fee_multiple"] * POOLS[pool_key].fee]
    if len(keep) < 50:
        raise SystemExit(f"{pool_key}: only {len(keep)} train shocks exceed the fee band")
    shock = rng.choice(keep, n)
    df = g.iloc[i].reset_index(drop=True)
    df["shock"] = shock
    df.attrs["n_shock_pool"] = int(len(keep))
    df["pi"] = df["p_ref_0"] * np.exp(shock)
    df["p_start"] = df["p_ref_0"]                                  # pool in sync with the reference before the shock
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CFG))
    a = ap.parse_args()
    cfg = reporting.load_config(a.config)
    out = reporting.out_dir("e5")
    rng = np.random.default_rng(cfg["seed"])
    G = cfg["grid_points"]
    fr = np.arange(1, G + 1) / G
    rows = []
    for key in cfg["pools"]:
        pool = POOLS[key]
        sc = sample_scenarios(key, cfg, rng)
        reg = mg.r_regimes_usd(key, cfg["variant"], 150000, cfg["r_quantiles"])[cfg["r_regime"]]
        al = mg.load_aligned(key)
        N_med = float(al.loc[al["split"] == "train", "amountUSD"].astype(float).median())
        x = (sc["L"] / np.sqrt(sc["p_start"])).to_numpy()[:, None]
        y = (sc["L"] * np.sqrt(sc["p_start"])).to_numpy()[:, None]
        pi = sc["pi"].to_numpy()[:, None]
        usd = sc["usd_per_num"].to_numpy()[:, None]
        dirn, n0 = cpmm.full_correction_net(x, y, pool.fee, pi)
        n = n0 * fr[None, :]
        tr = cpmm.trade(x, y, pool.fee, dirn, n)
        S = np.maximum(pi * tr["d0"] + tr["d1"], 0.0) * usd                     # USD
        N = tr["notional"] * usd                                               # USD
        p_post = tr["y"] / tr["x"]
        err_after = np.abs(np.log(p_post / pi))
        err_before = np.abs(np.log(sc["p_start"].to_numpy()[:, None] / pi))
        has = (n0 > 0)[:, 0]
        final = "final" in cfg
        if final:                                        # the hook's buffer: eps_rel of the scenario's day times |d1|, in USD
            tab = pd.read_csv(reporting.run_root() / cfg["final"]["eps_source"])
            tab = tab[(tab["pool"] == key) & (tab["variant"] == cfg["variant"])]
            ppb = pd.Series(tab["eps_rel_ppb"].to_numpy(float), index=pd.DatetimeIndex(pd.to_datetime(tab["day"])).tz_localize(None))
            day = pd.DatetimeIndex(sc["t"]).tz_convert(None).floor("D")
            eps_rel = ppb.reindex(day).to_numpy() / 1e9           # ppb -> fraction
            assert np.isfinite(eps_rel).all(), f"{key}: scenario days without a training eps"
            Sb = np.maximum(S - eps_rel[:, None] * np.abs(tr["d1"]) * usd, 0.0)
        else:
            Sb = S
        for gu, (brank, bps), m, mode in itertools.product(cfg["gas_units"], [(-1, 0.0)] + list(enumerate(cfg["external_fee_bps"][key])), cfg["curvature_multiple"], cfg["k_hat_modes"]):
            c0 = mg.gas_cost_num(sc["gas_wei"].to_numpy(), sc["eth_in_num"].to_numpy(), gu + cfg["hook_overhead_gas"])[:, None] * usd
            c1 = bps * 1e-4
            c2 = m * c1 / (2 * N_med)
            C = c0 + c1 * N + c2 * N ** 2
            # final: a charged correction pays the charged hook gas, and the hook's K_hat uses it; otherwise C_ch = C
            c0_ch = (mg.gas_cost_num(sc["gas_wei"].to_numpy(), sc["eth_in_num"].to_numpy(), gu + cfg["hook_overhead_gas_charged"])[:, None]
                     * usd) if final else c0
            C_ch = c0_ch + c1 * N + c2 * N ** 2
            R = reg
            base_pay = S - C
            j0 = argmax_smallest(base_pay)
            bf = has & (base_pay[np.arange(len(sc)), j0] >= R)
            # gas_only_buf: gas-only estimate plus a Theorem 3 buffer delta = eps_K, the P95 over baseline-feasible scenarios of the
            # size-dependent cost at the baseline optimum (the part of K the hook does not see); fitted on the same train scenarios
            sd = (C - c0)[np.arange(len(sc)), j0]
            dK = float(np.quantile(sd[bf], 0.95)) if (mode == "gas_only_buf" and bf.any()) else 0.0
            Khat = (C_ch + R) if mode == "exact" else (c0_ch + R + dK)
            for lam, gamma in itertools.product(cfg["lambdas"], cfg["gammas"]):
                r = np.minimum(lam * Sb, (1 - gamma) * np.maximum(Sb - Khat, 0.0))
                Pi = S - np.where(r > 0, C_ch, C) - r
                feas = Pi >= R - 1e-9 * max(1.0, R)
                anyf = feas.any(axis=1)
                jb = argmax_smallest(np.where(feas, Pi, -np.inf))
                idx = np.arange(len(sc))
                ex = bf & anyf
                sel = bf
                q_ratio = np.where(ex, (jb + 1) / (j0 + 1), np.nan)
                s_ratio = np.where(ex & (S[idx, j0] > 0), S[idx, jb] / np.where(S[idx, j0] > 0, S[idx, j0], 1), np.nan)
                perr = np.where(ex, err_after[idx, jb], err_before[:, 0])
                rows.append({"pool": key, "gas_units": gu, "external_fee_bps": bps, "bps_rank": brank, "curvature": m, "k_hat": mode, "lam": lam, "gamma": gamma,
                             "n_scenarios": len(sc), "n_baseline_feasible": int(sel.sum()), "execution_rate": float(ex.sum() / max(sel.sum(), 1)),
                             "baseline_optimum_kept": float(((jb == j0) & ex)[sel].mean()) if sel.any() else np.nan,
                             "q_ratio_median": float(np.nanmedian(q_ratio[sel])) if ex[sel].any() else np.nan,
                             "q_ratio_mean": float(np.nanmean(q_ratio[sel])) if ex[sel].any() else np.nan,
                             "surplus_ratio_mean": float(np.nanmean(s_ratio[sel])) if ex[sel].any() else np.nan,
                             "price_error_mean": float(perr[sel].mean()) if sel.any() else np.nan,
                             "transfer_usd_mean": float(np.where(ex, r[idx, jb], 0.0)[sel].mean()) if sel.any() else np.nan,
                             "recapture": float(np.where(ex, r[idx, jb], 0.0)[sel].sum() / max(np.where(ex, S[idx, jb], 0.0)[sel].sum(), 1e-12)),
                             "R_usd": R, "N_med_usd": N_med, "delta_K_usd": dK})
        print(key, "scenarios", len(sc), "baseline-feasible (g=150k,5bps,m=0.5):",
              int([r for r in rows if r["pool"] == key and r["gas_units"] == 150000 and r["bps_rank"] == 1 and r["curvature"] == 0.5 and r["lam"] == 0.75][0]["n_baseline_feasible"]), flush=True)
    res = pd.DataFrame(rows)
    res.to_parquet(out / "e5_summary.parquet", index=False)
    slice_ = res[(res["gas_units"] == 150000) & (res["bps_rank"] == 1) & (res["curvature"] == 0.5) & (res["lam"] == 0.75)]
    reporting.write_table(slice_.drop(columns=["gas_units", "bps_rank", "curvature", "lam", "n_scenarios", "N_med_usd"]), out / "tables" / "e5_slice_gas150k_5bps_curv0.5",
                          {c: "{:.3f}" for c in ["execution_rate", "baseline_optimum_kept", "q_ratio_median", "q_ratio_mean", "surplus_ratio_mean", "recapture"]} | {"price_error_mean": "{:.2e}"})
    ins = [CACHE / "aligned" / f"{k}.parquet" for k in cfg["pools"]]
    if "final" in cfg:
        ins += [reporting.run_root() / cfg["final"]["eps_source"], ROOT / "config" / "gas_block_scope.json", Path(a.config).resolve()]
    reporting.write_manifest("e5", cfg, ins, "train", {"note": "train months only", "final": cfg.get("final"),
                                                         "results_run": reporting.RESULTS_RUN})
    figs(res, out, cfg)
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(slice_[slice_["k_hat"] == "gas_only"][["pool", "gamma", "n_baseline_feasible", "execution_rate", "baseline_optimum_kept", "q_ratio_median", "surplus_ratio_mean", "price_error_mean", "recapture"]].round(4).to_string(index=False))


def figs(res: pd.DataFrame, out: Path, cfg: dict):
    plotstyle.apply()
    for xvar, fixed, tag in (("gamma", ("lam", 0.75), "vs_gamma"), ("lam", ("gamma", 0.02), "vs_lambda")):
        for metric, ylab, nm in (("q_ratio_mean", r"trade selection $q_{BR}/q_0$", "qratio"), ("surplus_ratio_mean", r"surplus ratio $S(q_{BR})/S(q_0)$", "sratio"),
                                 ("price_error_mean", r"final price error $|\log(p_{post}/p_{ref})|$", "perr"), ("execution_rate", "execution rate", "exec")):
            fig, axs = plt.subplots(1, len(cfg["pools"]), figsize=(2.6 * len(cfg["pools"]), 2.8), squeeze=False)
            for ax, key in zip(axs[0], cfg["pools"]):
                d = res[(res["pool"] == key) & (res["gas_units"] == 150000) & (res["bps_rank"] == 1) & np.isclose(res[fixed[0]], fixed[1])]
                for (m, mode), g in d.groupby(["curvature", "k_hat"]):
                    g = g.sort_values(xvar)
                    ax.plot(g[xvar], g[metric], marker="o", ms=3, ls={"gas_only": "-", "exact": "--"}.get(mode, ":"),label=fr"curvature {m}, {mode}")
                ax.set_title(POOL_LABEL[key], fontsize=8)
                ax.set_xlabel(plotstyle.GAMMA if xvar == "gamma" else plotstyle.LAMBDA)
            axs[0, 0].set_ylabel(ylab)
            axs[0, 0].legend(frameon=False, fontsize=5)
            fig.tight_layout()
            reporting.savefig(fig, out / "figures" / f"e5_{nm}_{tag}")
            plt.close(fig)


if __name__ == "__main__":
    main()
