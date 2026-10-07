#!/usr/bin/env python3
"""E8 post-processing (Phase 2 item 2.6): how concentrated the recovered funds are. No replay is rerun.

Per pool (primary variant, ideal reference k = 1, d = 0, median R):
  by correction  from results/e8/e8_steps_<tag>.parquet (per-correction transfers of the E8 run, whose as-frozen rows equal
                 E2): number of charged corrections, share of the total from the top 10 and top 50 corrections and from the
                 top 1% of corrections, Gini coefficient; checked against the E2 total.
  by day         from results/e2/e2_daily_<tag>.parquet: share of the total from the top 1 and top 5 days, the total without
                 the top 1 and top 5 days, and the width of the day-block 95% interval relative to the total (same seed
                 and B as e2_bootstrap.py), which the day concentration explains.

  python experiments/e8_concentration.py --tag test
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import reporting  # noqa: E402

PRIMARY = {"eth_usdc_005": "raw", "eth_wbtc_030": "corr24h", "eth_wsteth_001": "corr24h"}
LAM, GAM, B, SEED = 0.75, 0.02, 2000, 20260924


def gini(x: np.ndarray) -> float:
    x = np.sort(np.asarray(x, dtype=float))
    n = len(x)
    return float((2 * np.arange(1, n + 1) - n - 1) @ x / (n * x.sum())) if n and x.sum() > 0 else np.nan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="test")
    a = ap.parse_args()
    e2o, out = reporting.out_dir("e2"), reporting.out_dir("e8")
    summ = pd.read_parquet(e2o / f"e2_summary_{a.tag}.parquet")
    daily = pd.read_parquet(e2o / f"e2_daily_{a.tag}.parquet")
    sf = out / f"e8_steps_{a.tag}.parquet"
    steps = pd.read_parquet(sf) if sf.exists() else None
    rows = []
    for pool, var in PRIMARY.items():
        sel = lambda df: df[(df.pool == pool) & (df.variant == var) & (df.regime == "median") & (df.mech == "retained")
                            & np.isclose(df.lam, LAM) & np.isclose(df.gamma, GAM)]
        total = float(sel(summ).protection_usd.iloc[0])
        dd = sel(daily).sort_values("day")
        r_day = dd["sum_r"].to_numpy()
        assert abs(r_day.sum() - total) < 1e-6 * max(1.0, total)
        idx = np.random.default_rng(SEED).integers(0, len(r_day), size=(B, len(r_day)))
        lo, hi = np.quantile(r_day[idx].sum(1), [0.025, 0.975])
        top = np.sort(r_day)[::-1]
        row = {"pool": pool, "variant": var, "recovered_usd": total, "n_days": len(r_day),
               "top1_day_share": top[:1].sum() / total, "top5_days_share": top[:5].sum() / total,
               "recovered_without_top1_day_usd": total - top[:1].sum(), "recovered_without_top5_days_usd": total - top[:5].sum(),
               "ci_lo_usd": float(lo), "ci_hi_usd": float(hi), "ci_width_over_total": float(hi - lo) / total,
               "median_day_usd": float(np.median(r_day)), "max_day": str(pd.Timestamp(dd["day"].to_numpy()[np.argmax(r_day)]).date())}
        if steps is not None:
            x = steps[(steps.pool == pool) & (steps.mech == "retained")]["r_usd"].to_numpy()
            assert abs(x.sum() - total) < 1e-3 * max(1.0, total), (pool, x.sum(), total)   # float32 per-step storage
            xp = np.sort(x[x > 0])[::-1]
            n1 = max(1, int(np.ceil(0.01 * len(xp))))
            row.update({"n_executed": len(x), "n_charged": len(xp), "top10_corrections_share": xp[:10].sum() / x.sum(),
                        "top50_corrections_share": xp[:50].sum() / x.sum(), "top1pct_corrections_share": xp[:n1].sum() / x.sum(),
                        "median_charge_usd": float(np.median(xp)), "max_charge_usd": float(xp[0]), "gini_charged": gini(xp)})
        rows.append(row)
    res = pd.DataFrame(rows)
    fmt = {c: "{:,.2f}" for c in res.columns if c.endswith("usd")}
    fmt.update({c: "{:.4f}" for c in res.columns if c.endswith("share") or c in ("ci_width_over_total", "gini_charged")})
    reporting.write_table(res, out / "tables" / f"e8_concentration_{a.tag}", fmt)
    reporting.write_manifest("e8", {"analysis": "concentration", "lam": LAM, "gamma": GAM, "B": B, "seed": SEED},
                             [e2o / f"e2_summary_{a.tag}.parquet", e2o / f"e2_daily_{a.tag}.parquet"] + ([sf] if steps is not None else []),
                             a.tag.split("_")[0], {"source_tag": a.tag}, tag=f"concentration_{a.tag}", latest=False)
    with pd.option_context("display.width", 250):
        print(res.T.to_string())


if __name__ == "__main__":
    main()
