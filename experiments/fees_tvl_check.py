#!/usr/bin/env python3
"""Pool fees and TVL on the test months, with a check of the subgraph's fee field (external review 1.1(d)).

Reads data/data/subgraph/pool_day_data_jun_jul_2026.json (written by data/fetch_pool_day_data.py). For v4 pools the
subgraph derives feesUSD from volume and the fee tier, which is wrong for pools with dynamic fees and has been seen to
exceed the volume itself. Per pool, the check compares:
  - subgraph feesUSD with volumeUSD x fee tier (daily ratio, flagged outside [0.9, 1.1]),
  - subgraph volumeUSD with the volume summed from our own swap-level records (cache/aligned),
  - the fee tier and hook address reported by the subgraph with the pool definition in common/pools.py.
The fees used downstream are the subgraph's feesUSD when their total is within 10% of volume x tier, and otherwise
the swap-derived fees (sum of amountUSD x fee). The recovered protection of the retained margin (median R, ideal
replay, tag `test`) is then expressed as a share of those fees and of the mean TVL.

  python experiments/fees_tvl_check.py
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

from common import reporting  # noqa: E402
from common.pools import CACHE, POOLS  # noqa: E402
from e2_bootstrap import PRIMARY  # noqa: E402

SRC = ROOT / "data" / "data" / "subgraph" / "pool_day_data_jun_jul_2026.json"
TOL = 0.10
DYNAMIC_FEE_FLAG = 0x800000
ZERO_HOOK = "0x" + "0" * 40


def main():
    rec = json.loads(SRC.read_text())
    lo, hi = pd.Timestamp(rec["date_range_utc"]["start_inclusive"], tz="UTC"), pd.Timestamp(rec["date_range_utc"]["end_exclusive"], tz="UTC")
    n_days = (hi - lo).days
    summ = pd.read_parquet(reporting.out_dir("e2") / "e2_summary_test.parquet")
    ret = summ[(summ["mech"] == "retained") & (summ["regime"] == "median") & np.isclose(summ["lam"], 0.75) & np.isclose(summ["gamma"], 0.02)]
    rows, daily = [], []
    for k, p in POOLS.items():
        r = rec["pools"][k]
        d = pd.DataFrame(r["poolDayDatas"])
        for c in ("feesUSD", "tvlUSD", "volumeUSD"):
            d[c] = d[c].astype(float)
        d["day"] = pd.to_datetime(d["date"].astype(int), unit="s", utc=True)
        d["fees_vol_tier"] = d["volumeUSD"] * p.fee
        d["fee_ratio"] = np.where(d["fees_vol_tier"] > 0, d["feesUSD"] / d["fees_vol_tier"].where(d["fees_vol_tier"] > 0, 1), np.nan)
        al = pd.read_parquet(CACHE / "aligned" / f"{k}.parquet", columns=["timestamp", "amountUSD"])
        al = al[(al["timestamp"] >= lo) & (al["timestamp"] < hi)]
        ours = al.assign(day=al["timestamp"].dt.floor("D")).groupby("day")["amountUSD"].apply(lambda x: x.abs().sum())
        d = d.merge(ours.rename("volume_swaps").reset_index(), on="day", how="outer").sort_values("day")
        d.insert(0, "pool", k)
        daily.append(d)
        bad = d["fee_ratio"].notna() & ((d["fee_ratio"] - 1).abs() > TOL)
        vol_sg, vol_sw = d["volumeUSD"].sum(), d["volume_swaps"].sum()
        fees_sg, fees_vt, fees_sw = d["feesUSD"].sum(), vol_sg * p.fee, vol_sw * p.fee
        ok = fees_vt > 0 and abs(fees_sg / fees_vt - 1) <= TOL
        info = r["pool"] or {}
        tier = int(info["feeTier"]) if info.get("feeTier") is not None else None
        prot = ret[(ret["pool"] == k) & (ret["variant"] == PRIMARY[k])]["protection_usd"]
        prot = float(prot.iloc[0]) if len(prot) else np.nan
        fees_used = fees_sg if ok else fees_sw
        rows.append({"pool": k, "days_subgraph": int(d["date"].notna().sum()), "days_expected": n_days,
                     "fee_tier_subgraph": tier, "fee_tier_model": round(p.fee * 1e6),
                     "dynamic_fee": tier == DYNAMIC_FEE_FLAG, "hooks": info.get("hooks"), "has_hook": info.get("hooks") not in (None, ZERO_HOOK),
                     "volume_subgraph_usd": vol_sg, "volume_swaps_usd": vol_sw, "volume_ratio": vol_sg / vol_sw if vol_sw else np.nan,
                     "fees_subgraph_usd": fees_sg, "fees_vol_tier_usd": fees_vt, "fees_swaps_usd": fees_sw,
                     "fee_ratio_total": fees_sg / fees_vt if fees_vt else np.nan, "days_fee_ratio_off": int(bad.sum()),
                     "fees_check_passes": ok, "fees_used_usd": fees_used, "fees_source": "subgraph" if ok else "swaps",
                     "tvl_mean_usd": d["tvlUSD"].mean(), "tvl_median_usd": d["tvlUSD"].median(),
                     "protection_retained_usd": prot, "protection_over_fees": prot / fees_used if fees_used else np.nan,
                     "protection_over_tvl": prot / d["tvlUSD"].mean() if d["tvlUSD"].mean() else np.nan})
    res = pd.DataFrame(rows)
    out = reporting.out_dir("fees_tvl")
    pd.concat(daily, ignore_index=True).to_csv(out / "tables" / "fees_tvl_daily_test.csv", index=False)
    reporting.write_table(res, out / "tables" / "fees_tvl_test",
                          {c: "{:,.0f}" for c in res.columns if c.endswith("_usd")} | {"volume_ratio": "{:.3f}", "fee_ratio_total": "{:.3f}",
                                                                                     "protection_over_fees": "{:.3f}", "protection_over_tvl": "{:.4f}"})
    reporting.write_manifest("fees_tvl", {"tolerance": TOL}, [SRC] + [CACHE / "aligned" / f"{k}.parquet" for k in POOLS], "test",
                             {"fetched_at_utc": rec["fetched_at_utc"], "subgraph_id": rec["subgraph_id"]}, tag="test")
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(res.drop(columns=["hooks"]).T.to_string())


if __name__ == "__main__":
    main()
