#!/usr/bin/env python3
"""E2 post-processing (plan item 2.4): ex-post recoverable margin of the ideal replay. No replay is rerun.

For every executed correction the replay records the surplus S, the payoff Pi = S - C - r and the transfer r, so the
ex-post participation margin of a rule's executed corrections is

    M = sum (S - C - R) = searcher_net + protection - R * n_executed.

The margin of the baseline (zero-transfer hook) is the most that any participation-compatible rule can take from the
baseline corrections without suppressing one. The table compares each rule's recovered funds with it and with the
nominal target lambda * (gross surplus of the baseline corrections). Point values come from
results/e2/e2_summary_test.parquet (tag `test`: k = 1, d = 0). Intervals resample whole days of
results/e2/e2_daily_test.parquet with the paired day-block bootstrap of e2_bootstrap.py (same seed and B).

  python experiments/e2_recoverable_margin.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import reporting  # noqa: E402
from common.pools import CACHE  # noqa: E402

TAG = "test"
PRIMARY = {"eth_usdc_005": "raw", "eth_wbtc_030": "corr24h", "eth_wsteth_001": "corr24h"}   # USDC/USDT excluded (W8)
MECHS = ["baseline", "static_0.05pct", "static_0.30pct", "unconstrained", "cap_gamma0", "retained"]
LAM, GAM, B, SEED = 0.75, 0.02, 2000, 20260924


def pick(df: pd.DataFrame, pool: str, regime: str, mech: str) -> pd.DataFrame:
    x = df[(df.pool == pool) & (df.variant == PRIMARY[pool]) & (df.regime == regime) & (df.mech == mech)]
    if mech in ("unconstrained", "cap_gamma0", "retained"):
        x = x[np.isclose(x.lam, LAM)]
    if mech == "retained":
        x = x[np.isclose(x.gamma, GAM)]
    return x


def main():
    out = reporting.out_dir("e2")
    summ = pd.read_parquet(out / f"e2_summary_{TAG}.parquet")
    daily = pd.read_parquet(out / f"e2_daily_{TAG}.parquet")
    rng = np.random.default_rng(SEED)
    rows = []
    for pool in PRIMARY:
        for regime in ("low", "median", "high"):
            base = pick(summ, pool, regime, "baseline").iloc[0]
            R = float(base.R_usd)
            M_base = float(base.searcher_net_usd + base.protection_usd - R * base.n_executed)
            nominal = LAM * float(base.gross_surplus_usd)
            dd = {m: pick(daily, pool, regime, m) for m in MECHS}
            days = np.sort(dd["baseline"]["day"].unique())
            idx = rng.integers(0, len(days), size=(B, len(days)))
            db = dd["baseline"].set_index("day").reindex(days).fillna(0.0)
            mb_day = (db.sum_pi + db.sum_r - R * db.n_exec).to_numpy()
            mb_boot = mb_day[idx].sum(1)
            assert abs(mb_day.sum() - M_base) < 1e-6 * max(1.0, abs(M_base)), (pool, regime, "daily and summary margins differ")
            for m in MECHS:
                r = pick(summ, pool, regime, m).iloc[0]
                M_own = float(r.searcher_net_usd + r.protection_usd - R * r.n_executed)
                dm = dd[m].set_index("day").reindex(days).fillna(0.0)
                rec_boot = dm.sum_r.to_numpy()[idx].sum(1)
                share = rec_boot / np.where(mb_boot > 0, mb_boot, np.nan)
                lo, hi = np.nanquantile(share, [0.025, 0.975])
                if m in ("cap_gamma0", "retained"):                        # cap guarantee: r <= (1 - gamma) M per trade
                    g = 0.0 if m == "cap_gamma0" else GAM
                    assert r.protection_usd <= (1 - g) * M_own + 1e-6, (pool, regime, m, "recovered exceeds the capped margin")
                rows.append({"pool": pool, "variant": PRIMARY[pool], "regime": regime, "R_usd": R, "mech": m,
                             "n_executed": int(r.n_executed), "execution_rate": float(r.execution_rate),
                             "gross_surplus_usd": float(r.gross_surplus_usd), "margin_own_usd": M_own,
                             "margin_baseline_usd": M_base, "nominal_target_usd": nominal,
                             "recovered_usd": float(r.protection_usd),
                             "recovered_over_baseline_margin": float(r.protection_usd) / M_base if M_base > 0 else np.nan,
                             "ci_lo": float(lo), "ci_hi": float(hi),
                             "recovered_over_nominal": float(r.protection_usd) / nominal if nominal > 0 else np.nan,
                             "recovered_over_own_margin": float(r.protection_usd) / M_own if M_own > 0 else np.nan})
    res = pd.DataFrame(rows)
    ret = res[res.mech == "retained"].set_index(["pool", "regime"])
    bas = res[res.mech == "baseline"].set_index(["pool", "regime"])
    res.attrs["retained_same_path_max_rel_gap"] = float(((ret.margin_own_usd - bas.margin_own_usd).abs() / bas.margin_own_usd).max())
    fmt = {c: "{:,.2f}" for c in res.columns if c.endswith("usd")}
    fmt.update({c: "{:.4f}" for c in ("execution_rate", "recovered_over_baseline_margin", "ci_lo", "ci_hi",
                                      "recovered_over_nominal", "recovered_over_own_margin")})
    reporting.write_table(res, out / "tables" / f"e2_recoverable_margin_{TAG}", fmt)
    reporting.write_manifest("e2", {"analysis": "recoverable margin", "lam": LAM, "gamma": GAM, "B": B, "seed": SEED},
                             [out / f"e2_summary_{TAG}.parquet", out / f"e2_daily_{TAG}.parquet"], "test",
                             {"source_tag": TAG}, tag=f"recoverable_margin_{TAG}", latest=False)
    with pd.option_context("display.width", 250, "display.max_rows", 100):
        print("max relative gap between the retained margin's and the baseline's ex-post margin:",
              res.attrs["retained_same_path_max_rel_gap"])
        print(res[res.regime == "median"][["pool", "mech", "n_executed", "margin_own_usd", "margin_baseline_usd", "nominal_target_usd",
                                           "recovered_usd", "recovered_over_baseline_margin", "ci_lo", "ci_hi",
                                           "recovered_over_nominal"]].round(3).to_string(index=False))
        print(res[(res.pool == "eth_usdc_005") & (res.mech == "retained")][["regime", "R_usd", "margin_baseline_usd", "recovered_usd",
                                                                          "recovered_over_baseline_margin", "ci_lo", "ci_hi"]].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
