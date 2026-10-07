#!/usr/bin/env python3
"""E8 post-processing (Phase 2 item 2.5): loss-versus-rebalancing over the replay path. No replay is rerun.

LVR (Milionis et al.) of a constant-product position with virtual reserves x = L / sqrt(P), y = L sqrt(P) accrues at
sigma^2 / 8 times the position value V = 2 L sqrt(P). On the one-minute benchmark grid this gives, per minute,

    LVR_t = L_t sqrt(P_{t-1}) / 4 * (log P_t - log P_{t-1})^2      (numeraire units, converted to USD),

with L_t the E1 trailing depth known before t (the depth the replay uses) and P the benchmark (with the trailing offset
of the primary variant). It depends only on the price path and the depth, not on the mechanism, so it is the same for
every rule. One-minute returns miss the variance within a minute, so this is a lower bound on the continuous-time LVR of
the in-range position; it is not the LVR of total pool TVL.

The table puts it beside the replay's realized quantities (E2 tag `test`: k = 1, d = 0, median R): the baseline's gross
surplus S (the LPs' realized loss net of the pool fee), the funds each rule recovers, and their ratios to LVR. Intervals
resample whole days (paired with the E2 daily aggregates, same seed and B as e2_bootstrap.py).

  python experiments/e8_lvr.py --split test
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE  # noqa: E402

PRIMARY = {"eth_usdc_005": "raw", "eth_wbtc_030": "corr24h", "eth_wsteth_001": "corr24h"}
MECHS = ["baseline", "static_0.05pct", "static_0.30pct", "unconstrained", "cap_gamma0", "retained"]
LAM, GAM, B, SEED = 0.75, 0.02, 2000, 20260924
# the wstETH reference is the Lido rate, which changes once a day: the minute grid has no intraday variance, so the
# formula is not applicable there (rows kept, flagged `applicable = False`)
DAILY_REF = {"eth_wsteth_001"}


def lvr_minutes(key: str, split: str) -> pd.DataFrame:
    g = mg.build_grid(key, split, PRIMARY[key], lags=(0,))
    lp = np.log(g["p_ref_0"].to_numpy())
    dl = np.diff(lp, prepend=np.nan)
    p_prev = np.exp(np.roll(lp, 1))
    p_prev[0] = np.nan
    lvr_num = g["L"].to_numpy() * np.sqrt(p_prev) / 4.0 * dl ** 2
    v_usd = 2 * g["L"].to_numpy() * np.sqrt(g["p_ref_0"].to_numpy()) * g["usd_per_num"].to_numpy()
    out = pd.DataFrame({"t": g["t"], "lvr_usd": lvr_num * g["usd_per_num"].to_numpy(), "value_usd": v_usd})
    out["ok"] = np.isfinite(out["lvr_usd"])
    out["day"] = pd.DatetimeIndex(out["t"]).tz_convert(None).floor("D")     # naive UTC, as in the E2 daily file
    return out


def pick(df, pool, mech):
    x = df[(df.pool == pool) & (df.variant == PRIMARY[pool]) & (df.regime == "median") & (df.mech == mech)]
    if mech in ("unconstrained", "cap_gamma0", "retained"):
        x = x[np.isclose(x.lam, LAM)]
    if mech == "retained":
        x = x[np.isclose(x.gamma, GAM)]
    return x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["valid", "test"], default="test")
    a = ap.parse_args()
    e2o = reporting.out_dir("e2")
    out = reporting.out_dir("e8")
    summ = pd.read_parquet(e2o / f"e2_summary_{a.split}.parquet")
    daily = pd.read_parquet(e2o / f"e2_daily_{a.split}.parquet")
    rows = []
    for pool in PRIMARY:
        m = lvr_minutes(pool, a.split)
        ld = m[m.ok].groupby("day")["lvr_usd"].sum()
        days = np.sort(pick(daily, pool, "baseline")["day"].unique())
        ld = ld.reindex(days).fillna(0.0).to_numpy()
        idx = np.random.default_rng(SEED).integers(0, len(days), size=(B, len(days)))
        lvr_tot, lvr_boot = ld.sum(), ld[idx].sum(1)
        v_mean = float(m.loc[m.ok, "value_usd"].mean())
        base = pick(summ, pool, "baseline").iloc[0]
        S_base = float(base.gross_surplus_usd)
        for mech in MECHS:
            r = pick(summ, pool, mech).iloc[0]
            dm = pick(daily, pool, mech).set_index("day").reindex(days)["sum_r"].fillna(0.0).to_numpy()
            share = dm[idx].sum(1) / lvr_boot
            lo, hi = np.quantile(share, [0.025, 0.975])
            rows.append({"pool": pool, "variant": PRIMARY[pool], "mech": mech, "n_days": len(days),
                         "minutes_with_lvr": int(m.ok.sum()), "mean_position_value_usd": v_mean,
                         "lvr_usd": lvr_tot, "lvr_bp_per_day": lvr_tot / len(days) / v_mean * 1e4,
                         "baseline_surplus_usd": S_base, "baseline_surplus_over_lvr": S_base / lvr_tot,
                         "recovered_usd": float(r.protection_usd), "recovered_over_lvr": float(r.protection_usd) / lvr_tot,
                         "ci_lo": float(lo), "ci_hi": float(hi),
                         "surplus_share": float(r.protection_usd) / S_base if S_base > 0 else np.nan,
                         "applicable": pool not in DAILY_REF})
    res = pd.DataFrame(rows)
    fmt = {c: "{:,.0f}" for c in ("mean_position_value_usd", "lvr_usd", "baseline_surplus_usd", "recovered_usd")}
    fmt.update({c: "{:.4f}" for c in ("lvr_bp_per_day", "baseline_surplus_over_lvr", "recovered_over_lvr", "ci_lo", "ci_hi", "surplus_share")})
    reporting.write_table(res, out / "tables" / f"e8_lvr_{a.split}", fmt)
    reporting.write_manifest("e8", {"analysis": "lvr", "lam": LAM, "gamma": GAM, "B": B, "seed": SEED},
                             [e2o / f"e2_summary_{a.split}.parquet", e2o / f"e2_daily_{a.split}.parquet"] +
                             [CACHE / "aligned" / f"{k}.parquet" for k in PRIMARY], a.split, {"source_tag": a.split},
                             tag=f"lvr_{a.split}", latest=False)
    with pd.option_context("display.width", 250, "display.max_rows", 100):
        print(res[["pool", "mech", "lvr_usd", "lvr_bp_per_day", "baseline_surplus_over_lvr", "recovered_usd", "recovered_over_lvr",
                   "ci_lo", "ci_hi", "surplus_share"]].round(4).to_string(index=False))


if __name__ == "__main__":
    main()
