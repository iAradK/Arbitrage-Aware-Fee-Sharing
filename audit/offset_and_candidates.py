#!/usr/bin/env python3
r"""Signed-deviation profile per pool-month and corrected E6 candidate definition.
Rerun from repo root: .venv\Scripts\python.exe audit\offset_and_candidates.py --data-dir data
Read-only on data; writes audit/offset_results.json. No network.
Candidate = pre-swap deviation (previous swap's ending price vs reference at this swap's time,
last completed 1-min bar) exceeds fee tier AND swap moves price toward the reference.
"""
import argparse, json
from pathlib import Path
import numpy as np, pandas as pd
ap = argparse.ArgumentParser(); ap.add_argument("--data-dir", default="data")
ap.add_argument("--out", default=str(Path(__file__).parent / "offset_results.json")); a = ap.parse_args()
D = Path(a.data_dir)
POOLS = {"eth_usdc_005": (18, 6, 5e-4), "usdc_usdt_0001": (6, 6, 1e-4),
         "eth_wbtc_030": (18, 8, 3e-3), "eth_wsteth_001": (18, 18, 1e-4)}
TRAIN_END = pd.Timestamp("2026-05-01", tz="UTC"); TEST0 = pd.Timestamp("2026-07-01", tz="UTC")
def kl(s): return pd.read_csv(D/"data"/"binance"/f"{s}_1m.csv.gz", index_col=0, parse_dates=True)
lido = pd.read_csv(D/"data"/"lido"/"wsteth_rate_events.csv"); lido["t"] = pd.to_datetime(lido.block_time, utc=True)
def ref(pool):
    if pool == "eth_usdc_005": p = kl("ETHUSDC").close
    elif pool == "eth_wbtc_030": p = kl("ETHBTC").close
    elif pool == "usdc_usdt_0001":
        k = kl("USDCUSDT"); p = (k.quote_volume / k.volume).where(k.volume > 0, k.close)
    else: return pd.DataFrame({"avail": lido.t, "ref": lido.wsteth_per_eth}), pd.Timedelta(days=3)
    return pd.DataFrame({"avail": p.index + pd.Timedelta(minutes=1), "ref": p.values}), pd.Timedelta(minutes=5)
def q(x): return {"med_bp": float(x.median()*1e4), "p5_bp": float(x.quantile(.05)*1e4), "p95_bp": float(x.quantile(.95)*1e4), "n": int(len(x))}
R = {}
for pool, (d0, d1, fee) in POOLS.items():
    df = pd.read_csv(D/"swaps_data"/f"swaps_{pool}.csv", usecols=["timestamp","logIndex","amountUSD","sqrtPriceX96","tx_blockNumber"], dtype={"sqrtPriceX96": str})
    df["t"] = pd.to_datetime(df.timestamp, unit="s", utc=True).astype("datetime64[us, UTC]")
    df = df.sort_values(["t","logIndex"]).reset_index(drop=True)
    df["px"] = (df.sqrtPriceX96.astype(float)/2**96)**2 * 10.0**(d0-d1)
    rs, tol = ref(pool); rs = rs.sort_values("avail"); rs["avail"] = rs.avail.astype("datetime64[us, UTC]")
    df["ref"] = pd.merge_asof(df[["t"]], rs, left_on="t", right_on="avail", direction="backward", tolerance=tol).ref.values
    df["month"] = df.t.dt.strftime("%Y-%m")
    df["post"] = np.log(df.px/df.ref); df["pre"] = np.log(df.px.shift(1)/df.ref)
    tr = df[df.t < TRAIN_END]; off_const = float(tr.post.median())
    off_month = df.groupby("month").post.median()
    out = {"fee_bp": fee*1e4, "train_offset_bp": off_const*1e4,
           "signed_post_dev_by_month": {m: q(g.post.dropna()) for m, g in df.groupby("month")},
           "monthly_median_offset_bp_std_across_months": float(off_month.std()*1e4)}
    t = df[(df.t >= TEST0) & df.ref.notna() & df.pre.notna()].copy()
    def cands(pre, post, thr):
        c = t[(pre.abs() > thr) & (post.abs() < pre.abs())].copy()
        c["s"] = pre[c.index].abs() * c.amountUSD
        r = {"n": int(len(c)), "share_of_test": float(len(c)/len(t)), "ge100": bool(len(c) >= 100), "ge400": bool(len(c) >= 400)}
        if len(c) >= 4:
            qq = pd.qcut(c.s.rank(method="first"), 4, labels=False); r["by_surplus_quartile"] = [int((qq == i).sum()) for i in range(4)]
        r["by_month"] = {m: int((c.month == m).sum()) for m in ["2026-07", "2026-08"]}
        return r
    variants = {"raw": (t.pre, t.post),
                "minus_train_offset": (t.pre - off_const, t.post - off_const),
                "minus_monthly_offset(in-sample)": (t.pre - t.month.map(off_month), t.post - t.month.map(off_month))}
    out["e6"] = {}
    for name, (pre, post) in variants.items():
        out["e6"][name] = {"gt_1x_fee": cands(pre, post, fee), "gt_2x_fee": cands(pre, post, 2*fee)}
    out["e6"]["old_definition_post_dev_gt_fee_raw"] = int((t.post.abs() > fee).sum())
    out["test_n"] = int(len(t)); R[pool] = out
json.dump(R, open(a.out, "w"), indent=1)
for p, o in R.items():
    print(p, "fee", o["fee_bp"], "train offset bp", round(o["train_offset_bp"],2), "monthly-median std bp", round(o["monthly_median_offset_bp_std_across_months"],2))
    for m, s in o["signed_post_dev_by_month"].items(): print("  ", m, {k: round(v,2) for k, v in s.items()})
    for v, d in o["e6"].items():
        if isinstance(d, dict): print("  ", v, "1x:", d["gt_1x_fee"]["n"], d["gt_1x_fee"].get("by_surplus_quartile"), "2x:", d["gt_2x_fee"]["n"])
        else: print("  ", v, d)
