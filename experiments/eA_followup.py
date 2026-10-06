#!/usr/bin/env python3
"""Experiment A follow-up (post-processing of results/eA; no replay is re-executed).

1. Block capacity: l <= floor(gas_limit_b / 185,180), gas_limit_b of the correction's block from the BigQuery blocks
   export (data/data/gas/bq-results-*.csv; E2 corrections: last block strictly before the grid minute, as the replay's
   gas price). Sensitivities l <= 10 and l <= 50.
2. Aggregation of the two-way split: sum-weighted (sum of charges / sum of F(a)) and mean of per-correction ratios, for
   the independent rule on two equal-input CPMM fragments (E6 sample only; this is what Section 5.4.2 reports), two
   equal-surplus pieces and the optimal l = 2 of Experiment A. The E6 sample uses both E6's K (180,214 gas at the
   block's actual gas price + R) and Experiment A's kappa (base fee + tau_hat).
3. Part 2: share of price-correcting swaps whose block-scope overcharge turns them into a participation violation.
4. Manifest: provenance of the per-correction sizes and the exact reproduction of every stored total.

  python experiments/eA_followup.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

import e6_fragmentation as e6  # noqa: E402
import eA_strategic_split as eA  # noqa: E402
from common import data_io, fixedpoint as fp, mechanism as mech, minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS, RESULTS  # noqa: E402

OUT = eA.OUT
WAD = fp.WAD
CAPS = {"block": None, "l<=10": 10, "l<=50": 50}


def saving_path(a_usd, k_usd, u_usd, ctx, st, F: eA.Fint):
    """charge(l) (WAD) and saving(l) (USD) for l = 1..L, stopping at the first uncharged remainder (as split_one)."""
    a_w, k_w = fp.to_wad(max(a_usd, 0.0)), fp.to_wad(k_usd)
    piece = k_w - max(1, fp.to_wad(u_usd))
    F_a = F(a_w, k_w)
    ch, sv = [F_a], [0.0]
    if F_a > 0:
        for l in range(2, -(-a_w // k_w) + 2):
            c = F(max(a_w - (l - 1) * piece, 0), k_w)
            ch.append(c)
            sv.append((F_a - c) / WAD - (l - 1) * ctx + (st if c == 0 else 0.0))
            if c == 0:
                break
    return np.array(ch, dtype=object), np.array(sv)


def best_l(sv: np.ndarray, cap: int | None) -> int:
    """Smallest l maximising saving over 1..cap (ties within 1e-12 USD go to the smaller l, as split_one)."""
    s = sv if cap is None else sv[:cap]
    best, lb = 0.0, 1
    for i in range(1, len(s)):
        if s[i] > best + 1e-12:
            best, lb = s[i], i + 1
    return lb


def block_gas_limit() -> pd.DataFrame:
    b = data_io.load_blocks()[["block", "timestamp", "gas_limit"]]
    return b


def capacity(corr: pd.DataFrame, blocks: pd.DataFrame, gas: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    c = corr.copy()
    # block of each correction: E6 sample has it; E2 corrections take the last block strictly before the grid minute
    bts = pd.DatetimeIndex(blocks["timestamp"])
    t = pd.DatetimeIndex(pd.to_datetime(c["timestamp"], utc=True))
    idx = bts.searchsorted(t, side="left") - 1
    asof_block = np.where(idx >= 0, blocks["block"].to_numpy()[np.clip(idx, 0, None)], -1)
    c["block_used"] = np.where(c["block"].notna(), c["block"].fillna(-1), asof_block).astype(np.int64)
    gl = blocks.set_index("block")["gas_limit"]
    c["gas_limit"] = gl.reindex(c["block_used"]).to_numpy()
    assert c["gas_limit"].notna().all(), "correction block missing from the BigQuery export"
    c["cap_block"] = (c["gas_limit"] // gas["extra_tx_gas"]).astype(int)
    F = {("e6_sample"): eA.Fint(0.75, 0.05), ("e2_replay"): eA.Fint(0.75, 0.02)}
    rows = []
    for r in c.itertuples():
        ch, sv = saving_path(r.a_usd, r.kappa_usd, r.unit_usd, r.c_tx_usd, r.settle_usd, F[r.source])
        lu = best_l(sv, None)
        assert lu == r.l_star and abs(float(ch[lu - 1]) / WAD - r.charge_star_usd) < 1e-9, "uncapped optimum differs from eA"
        out = {"l_star_uncapped": lu}
        for name, cap in CAPS.items():
            cp = r.cap_block if cap is None else cap
            lc = best_l(sv, cp)
            out[f"l_star_{name}"], out[f"charge_{name}_usd"] = lc, float(ch[lc - 1]) / WAD
            out[f"lcap_{name}"] = cp
        rows.append(out)
    c = pd.concat([c, pd.DataFrame(rows, index=c.index)], axis=1)
    q = lambda x, p: float(np.quantile(x, p)) if len(x) else np.nan  # noqa: E731
    summ = []
    for (src, st, pool, var), x in c.groupby(["source", "setting", "pool", "variant"], sort=False):
        tot = x["F_a_usd"].sum()
        chg = x[x["F_a_usd"] > 0]
        base = {"source": src, "setting": st, "pool": pool, "variant": var, "n_corrections": len(x), "n_charged": len(chg),
                "original_usd": tot, "gas_limit_median": q(x["gas_limit"], 0.5), "gas_limit_min": float(x["gas_limit"].min()),
                "cap_block_median": q(x["cap_block"], 0.5), "cap_block_min": int(x["cap_block"].min()),
                "surviving_share_uncapped": x["charge_star_usd"].sum() / tot if tot > 0 else np.nan,
                "surviving_share_l2": x["charge2_usd"].sum() / tot if tot > 0 else np.nan,
                "l_star_median_uncapped": q(chg["l_star_uncapped"], 0.5), "l_star_p95_uncapped": q(chg["l_star_uncapped"], 0.95),
                "l_star_max_uncapped": int(chg["l_star_uncapped"].max()) if len(chg) else np.nan}
        for name in CAPS:
            bind = chg["l_star_uncapped"] > chg[f"lcap_{name}"]
            base.update({f"surviving_share_{name}": x[f"charge_{name}_usd"].sum() / tot if tot > 0 else np.nan,
                         f"strategic_usd_{name}": x[f"charge_{name}_usd"].sum(),
                         f"l_star_median_{name}": q(chg[f"l_star_{name}"], 0.5), f"l_star_p95_{name}": q(chg[f"l_star_{name}"], 0.95),
                         f"l_star_max_{name}": int(chg[f"l_star_{name}"].max()) if len(chg) else np.nan,
                         f"n_cap_binds_{name}": int(bind.sum()),
                         f"transfer_share_cap_binds_{name}": chg.loc[bind, "F_a_usd"].sum() / tot if tot > 0 else np.nan})
        summ.append(base)
    return c, pd.DataFrame(summ)


def aggregation(corr: pd.DataFrame, tau: float) -> pd.DataFrame:
    """Two-way split, two aggregations. Ratios are taken over corrections with F(a) > 0."""
    cfg6 = reporting.load_config(ROOT / "experiments" / "configs" / "e6.yml")
    lam, gam = cfg6["lam"], cfg6["gamma"]
    F6 = eA.Fint(lam, gam)
    rows = []

    def agg(name, kconv, src, st, pool, var, Fa, ch):
        Fa, ch = np.asarray(Fa, float), np.asarray(ch, float)
        m = Fa > 0
        rows.append({"source": src, "setting": st, "pool": pool, "variant": var, "method": name, "kappa": kconv,
                     "n_charged": int(m.sum()), "sum_weighted": ch[m].sum() / Fa[m].sum() if m.any() else np.nan,
                     "mean_of_ratios": float(np.mean(ch[m] / Fa[m])) if m.any() else np.nan})

    for key, var in eA.SAMPLE_VARIANTS:
        pool = POOLS[key]
        R_usd = mg.r_regimes_usd(key, var, cfg6["gas_units"], cfg6["r_quantiles"])[cfg6["r_regime"]]
        c_all, _ = e6.all_candidates(key, var, "test", cfg6, R_usd)
        sam = c_all[(c_all["n_used"] > 0) & (c_all["S"] - c_all["C"] >= c_all["R"])].reset_index(drop=True)
        mine = corr[(corr.source == "e6_sample") & (corr.setting == "delta0") & (corr.pool == key) & (corr.variant == var)]
        assert len(mine) == len(sam) and np.allclose(mine["a_usd"].to_numpy(), sam["S_usd"].to_numpy(), rtol=0, atol=0)
        A_half, A_full, K_e6 = [], [], []
        for o in sam.itertuples():
            A = e6.path_prefix_surplus(o, np.array([0.5, 1.0]), pool) * o.usd_per_num        # E6 equal-input path, USD
            A_half.append(A[0])
            A_full.append(A[1])
            K_e6.append((o.C + o.R) * o.usd_per_num)
        A_half, A_full, K_e6 = map(np.asarray, (A_half, A_full, K_e6))
        s1, s2 = A_half, A_full - A_half
        k_eA = mine["kappa_usd"].to_numpy()
        for kconv, K in [("e6_K (actual gas price)", K_e6), ("eA_kappa (base fee + tau_hat)", k_eA)]:
            # float, exactly as e6_fragmentation (mech.independent_charges on the equal split)
            Ff = mech.F_scalar(A_full, K, lam, gam, 0.0)
            ind_f = mech.F_scalar(s1, K, lam, gam, 0.0) + mech.F_scalar(s2, K, lam, gam, 0.0)
            agg("independent, 2 equal-input CPMM fragments (float, as E6)", kconv, "e6_sample", "delta0", key, var, Ff, ind_f)
            Fi = np.array([F6(fp.to_wad(a), fp.to_wad(k)) for a, k in zip(A_full, K)], float) / WAD
            ind_i = np.array([F6(fp.to_wad(max(x, 0.0)), fp.to_wad(k)) + F6(fp.to_wad(max(y, 0.0)), fp.to_wad(k))
                              for x, y, k in zip(s1, s2, K)], float) / WAD
            agg("independent, 2 equal-input CPMM fragments (integer)", kconv, "e6_sample", "delta0", key, var, Fi, ind_i)
            a = mine["a_usd"].to_numpy()
            Fa = np.array([F6(fp.to_wad(x), fp.to_wad(k)) for x, k in zip(a, K)], float) / WAD
            half = np.array([2 * F6(fp.to_wad(x / 2), fp.to_wad(k)) for x, k in zip(a, K)], float) / WAD
            agg("2 equal-surplus pieces (integer)", kconv, "e6_sample", "delta0", key, var, Fa, half)
        agg("optimal l = 2 (piece kappa - 1 unit)", "eA_kappa (base fee + tau_hat)", "e6_sample", "delta0", key, var,
            mine["F_a_usd"], mine["charge2_usd"])
    for (st, key, var), x in corr[corr.source == "e2_replay"].groupby(["setting", "pool", "variant"], sort=False):
        F2 = eA.Fint(0.75, 0.02)
        half = np.array([2 * F2(fp.to_wad(a / 2), fp.to_wad(k)) for a, k in zip(x["a_usd"], x["kappa_usd"])], float) / WAD
        agg("2 equal-surplus pieces (integer)", "run K_hat", "e2_replay", st, key, var, x["F_a_usd"], half)
        agg("optimal l = 2 (piece kappa - 1 unit)", "run K_hat", "e2_replay", st, key, var, x["F_a_usd"], x["charge2_usd"])
    return pd.DataFrame(rows)


def part2_violation(p2: pd.DataFrame) -> pd.DataFrame:
    """Participation violation = total block-scope charge F(a) + overcharge exceeds the own estimated margin (a - kappa)^+.
    Turned into a violation = the swap was estimated-feasible (a >= kappa) and is not any more."""
    rows = []
    for (pool, var, st), x in p2.groupby(["pool", "variant", "setting"], sort=False):
        feas = x["a_usd"] >= x["kappa_usd"]
        turned = feas & x["violation_total"]
        rows.append({"pool": pool, "variant": var, "setting": st, "n_pc_swaps": len(x), "n_overcharged": int((x["overcharge_usd"] > 0).sum()),
                     "n_violation": int(x["violation_total"].sum()), "share_violation": float(x["violation_total"].mean()),
                     "n_estimated_feasible": int(feas.sum()), "n_turned_into_violation": int(turned.sum()),
                     "share_turned_of_pc": float(turned.mean()), "share_turned_of_feasible": float(turned.sum() / feas.sum()) if feas.any() else np.nan,
                     "n_violation_already_infeasible": int((~feas & x["violation_total"]).sum())})
    return pd.DataFrame(rows)


def main():
    gas = eA.gas_constants()
    tau = eA.tau_hat_wei()
    corr = pd.read_csv(OUT / "eA_part1_corrections.csv.gz", float_precision="round_trip")
    blocks = block_gas_limit()
    cap_corr, cap_sum = capacity(corr, blocks, gas)
    cap_sum.to_csv(OUT / "eA_fu_capacity_summary.csv", index=False)
    cap_corr[["source", "setting", "pool", "variant", "timestamp", "block_used", "gas_limit", "a_usd", "kappa_usd", "F_a_usd",
              "l_star_uncapped"] + [f"{p}_{n}" for n in CAPS for p in ("lcap", "l_star")] + [f"charge_{n}_usd" for n in CAPS]] \
        .to_csv(OUT / "eA_fu_capacity_corrections.csv.gz", index=False)
    ag = aggregation(corr, tau)
    ag.to_csv(OUT / "eA_fu_aggregation.csv", index=False)
    e6c = pd.read_csv(RESULTS / "e6" / "tables" / "e6_checks_test.csv")
    paper = float(e6c[(e6c.pool == "eth_usdc_005") & (e6c.variant == "raw") & (e6c.n_fragments == 2)].indep_over_cumulative_equal.iloc[0])
    rep = ag[(ag.pool == "eth_usdc_005") & (ag.method.str.contains("float")) & (ag.kappa.str.startswith("e6_K"))].iloc[0]
    assert abs(rep["mean_of_ratios"] - paper) < 1e-12, (rep["mean_of_ratios"], paper)
    vio = part2_violation(pd.read_csv(OUT / "eA_part2_swaps.csv.gz", float_precision="round_trip"))
    vio.to_csv(OUT / "eA_fu_part2_violation.csv", index=False)

    # 4. manifest: provenance of the per-correction sizes; exact reproduction of every stored total
    ck = pd.read_csv(OUT / "eA_checks.csv", float_precision="round_trip")
    tot = ck[ck["check"].str.contains("protection_usd")]
    eps = ck[ck["check"].str.contains("eps_S")]
    repro = [{"pool": r.pool, "variant": r.variant, "check": r.check, "recomputed": r.recomputed, "stored": r.stored,
              "abs_diff": abs(r.recomputed - r.stored), "n_exec_recomputed": int(r.n_exec_recomputed), "n_exec_stored": int(r.n_exec_stored)}
             for r in tot.itertuples()]
    exact = bool((tot["recomputed"] == tot["stored"]).all() and (tot["n_exec_recomputed"] == tot["n_exec_stored"]).all())
    eps_exact = bool((eps["recomputed"] == eps["stored"]).all())
    prov = {"per_correction_sizes": "in-memory re-execution of the frozen e2_sequential_replay.simulate() (unmodified; E2 config "
                                    "hash equal to results/e2/frozen_config.sha256), restricted to the baseline plus the one "
                                    "configuration; nothing under results/e2 written; a = F^-1(executed transfer)",
            "stored_totals_reproduced_exactly": exact, "rolling_eps_medians_reproduced_exactly": eps_exact,
            "reproduction": repro, "source_of_checks": "results/eA/eA_checks.csv"}
    assert exact and eps_exact, "a stored total was not reproduced bit for bit"
    mf = json.load(open(OUT / "manifest.json"))
    mf["provenance"] = prov
    mf["followup"] = "manifest_followup.json"
    (OUT / "manifest.json").write_text(json.dumps(mf, indent=1))
    reporting.write_manifest("eA", {"followup_of": "eA", "caps": {k: v for k, v in CAPS.items()}, "extra_tx_gas": gas["extra_tx_gas"]},
                             [OUT / "eA_part1_corrections.csv.gz", OUT / "eA_part2_swaps.csv.gz", OUT / "eA_checks.csv",
                              RESULTS / "e6" / "tables" / "e6_checks_test.csv", CACHE / "aligned" / "eth_usdc_005.parquet",
                              CACHE / "aligned" / "eth_wbtc_030.parquet", Path(__file__).resolve(), Path(eA.__file__).resolve()]
                             + [Path(p) for p in sorted((ROOT / "data" / "data" / "gas").glob("bq-results-*.csv"))], "test",
                             {"provenance": prov, "paper_45_1": {"value": paper, "aggregation": "mean of per-correction ratios",
                                                                 "source": "results/e6/tables/e6_checks_test.csv, indep_over_cumulative_equal, n=2"},
                              "outputs": {p.name: eA.sha(p) for p in sorted(OUT.glob("eA_fu_*"))}}, tag="followup", latest=False)
    for sd in ("tables", "figures"):
        p_ = OUT / sd
        if p_.exists() and not any(p_.iterdir()):
            p_.rmdir()
    with pd.option_context("display.width", 250, "display.max_columns", 80, "display.max_rows", 200):
        print(cap_sum.T.to_string())
        print(ag.to_string())
        print(vio.to_string())
    print("exact reproduction:", exact, eps_exact, "| paper 45.1% =", paper)


if __name__ == "__main__":
    main()
