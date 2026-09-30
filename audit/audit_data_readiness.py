#!/usr/bin/env python3
"""Read-only audit of the data folder. Writes audit/audit_results.json.

Rerun (from the repo root, with the project venv):
    .venv\\Scripts\\python.exe audit\\audit_data_readiness.py --data-dir data
Only reads files under --data-dir; writes only into audit/. No network. Prints no secrets.
"""
import argparse, json, re, sys, os
from pathlib import Path
import numpy as np, pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--data-dir", default="data")
ap.add_argument("--out", default=str(Path(__file__).parent / "audit_results.json"))
a = ap.parse_args()
D = Path(a.data_dir)
R = {}

POOLS = {  # dec0, dec1, fee
    "eth_usdc_005": (18, 6, 5e-4),
    "usdc_usdt_0001": (6, 6, 1e-4),
    "eth_wbtc_030": (18, 8, 3e-3),
    "eth_wsteth_001": (18, 18, 1e-4),
}
STUDY0 = pd.Timestamp("2025-09-01", tz="UTC"); STUDY1 = pd.Timestamp("2026-09-01", tz="UTC")
TRAIN_END = pd.Timestamp("2026-05-01", tz="UTC"); VAL_END = pd.Timestamp("2026-07-01", tz="UTC")
PYTH_UPG = pd.Timestamp("2026-08-26", tz="UTC")

def split_of(ts):  # ts: tz-aware Series
    return np.where(ts < TRAIN_END, "train", np.where(ts < VAL_END, "val", "test"))

def parse_time(s):
    """detect unit; return (UTC DatetimeIndex/Series, unit string)"""
    if pd.api.types.is_numeric_dtype(s):
        m = float(s.iloc[0]) if len(s) else 0
        unit = "s" if m < 1e11 else "ms" if m < 1e14 else "us"
        return pd.to_datetime(s, unit=unit, utc=True), unit
    return pd.to_datetime(s, utc=True), "iso-string"

def gaps(idx_sorted, n=5):
    d = pd.Series(idx_sorted[1:] - idx_sorted[:-1], index=idx_sorted[:-1])
    top = d.sort_values(ascending=False).head(n)
    return [{"start": str(k), "gap": str(v)} for k, v in top.items()]

# ---------------- Binance ----------------
bin_res = {}; klines = {}
for sym in ["ETHUSDC", "ETHBTC", "USDCUSDT", "ETHUSDT", "BTCUSDT"]:
    f = D / "data" / "binance" / f"{sym}_1m.csv.gz"
    if not f.exists():
        bin_res[sym] = {"found": False}; continue
    df = pd.read_csv(f)
    ts, unit = parse_time(df["timestamp"]); df["ts"] = ts
    dup = int(df.ts.duplicated().sum())
    df = df.drop_duplicates("ts").sort_values("ts").set_index("ts")
    klines[sym] = df
    full = pd.date_range(STUDY0, STUDY1 - pd.Timedelta(minutes=1), freq="min")
    miss = full.difference(df.index)
    test_full = full[full >= VAL_END]
    miss_test = test_full.difference(df.index)
    inpd = df[(df.index >= STUDY0) & (df.index < STUDY1)]
    r = {"found": True, "cols": list(df.columns), "unit": unit, "rows": len(df),
         "first": str(df.index[0]), "last": str(df.index[-1]),
         "rows_in_study": len(inpd), "expected_minutes": len(full),
         "missing_minutes_study": len(miss), "expected_test": len(test_full),
         "missing_minutes_test": len(miss_test), "dup_ts": dup,
         "nonpositive_price_rows": int((df[["open", "high", "low", "close"]] <= 0).any(axis=1).sum()),
         "zero_volume_rows": int((df.volume == 0).sum()),
         "nan_rows": int(df.isna().any(axis=1).sum()),
         "close_min": float(df.close.min()), "close_max": float(df.close.max())}
    # top gaps within the study period (gaps between consecutive existing timestamps)
    r["top5_gaps"] = gaps(inpd.index)
    if sym == "USDCUSDT":
        r["distinct_close"] = int(df.close.nunique())
        r["close_value_counts_top"] = {str(k): int(v) for k, v in df.close.value_counts().head(8).items()}
        r["distinct_close_test"] = int(df[df.index >= VAL_END].close.nunique())
    bin_res[sym] = r
R["binance"] = bin_res
raw = D / "data" / "binance" / "raw"
R["binance_raw_dirs"] = {p.name: len(list(p.iterdir())) for p in raw.iterdir()} if raw.exists() else {}

# ---------------- Pyth ----------------
f = D / "data" / "pyth" / "onchain_ETH_USD_events.csv.gz"
if f.exists():
    py = pd.read_csv(f); py["t"] = pd.to_datetime(py.block_time, utc=True); py = py.sort_values("t")
    py["m"] = py.t.dt.strftime("%Y-%m")
    per_month = {}
    for m, g in py.groupby("m"):
        s = g.staleness_s
        per_month[m] = {"n": len(g), "med": float(s.median()), "p90": float(s.quantile(.9)), "p99": float(s.quantile(.99))}
    gp = py.t.diff()
    pre, post = py[py.t < PYTH_UPG], py[py.t >= PYTH_UPG]
    def st(g): return {"n": len(g), "med": float(g.staleness_s.median()), "p90": float(g.staleness_s.quantile(.9)),
                       "p99": float(g.staleness_s.quantile(.99)), "max": float(g.staleness_s.max())} if len(g) else {"n": 0}
    R["pyth"] = {"cols": list(py.columns[:-2]), "rows": len(py), "first": str(py.t.iloc[0]), "last": str(py.t.iloc[-1]),
                 "per_month": per_month, "largest_gap": str(gp.max()), "largest_gap_at": str(py.t[gp.idxmax()]),
                 "top5_gaps": [{"end": str(py.t[i]), "gap": str(v)} for i, v in gp.sort_values(ascending=False).head(5).items()],
                 "pre_upgrade": st(pre), "post_upgrade": st(post),
                 "dup_tx_log": int(py.duplicated(["tx_hash", "log_index"]).sum()),
                 "price_usd_min": float(py.price_usd.min()), "price_usd_max": float(py.price_usd.max()),
                 "post_upgrade_days_covered": float((py.t.max() - PYTH_UPG).total_seconds() / 86400)}
    R["pyth_benchmarks_1m_present"] = (D / "data" / "pyth" / "pyth_ETH_USD_1m.csv.gz").exists()
else:
    R["pyth"] = {"found": False}

# ---------------- fee history ----------------
f = D / "data" / "gas" / "fee_history.csv"
fh = None
if f.exists():
    fh = pd.read_csv(f)
    b = fh.block_number.values
    s = np.sort(b); ub = np.unique(s)
    holes = int((np.diff(ub) - 1).clip(min=0).sum())
    dgap = np.diff(ub); big = np.argsort(-dgap)[:5]
    R["fee_history"] = {"cols": list(fh.columns), "rows": len(fh), "min_block": int(s[0]), "max_block": int(s[-1]),
                        "unique_blocks": int(len(ub)), "dup_blocks": int(len(s) - len(ub)),
                        "hole_blocks": holes, "sorted_asc_in_file": bool((np.diff(b) > 0).all()),
                        "expected_rows_if_contiguous": int(s[-1] - s[0] + 1),
                        "largest_holes": [{"after_block": int(ub[i]), "next_block": int(ub[i + 1])} for i in big if dgap[i] > 1],
                        "base_fee_gwei_min": float(fh.base_fee_gwei.min()), "base_fee_gwei_med": float(fh.base_fee_gwei.median()),
                        "base_fee_gwei_max": float(fh.base_fee_gwei.max()),
                        "zero_tip_p50_share": float((fh.tip_p50_wei == 0).mean()),
                        "zero_tip_p10_share": float((fh.tip_p10_wei == 0).mean()),
                        "gas_price_p50_gwei_med": float(fh.gas_price_p50_gwei.median()),
                        "nan_rows": int(fh.isna().any(axis=1).sum())}
    fh_blocks = set(ub.tolist())
else:
    R["fee_history"] = {"found": False}; fh_blocks = set()

# ---------------- Lido ----------------
f = D / "data" / "lido" / "wsteth_rate_events.csv"
lido = None
if f.exists():
    lido = pd.read_csv(f); lido["t"] = pd.to_datetime(lido.block_time, utc=True); lido = lido.sort_values("t").reset_index(drop=True)
    dec = lido.eth_per_wsteth.diff() < 0
    gd = lido.t.diff()
    R["lido"] = {"cols": list(lido.columns[:-1]), "rows": len(lido), "first": str(lido.t.iloc[0]), "last": str(lido.t.iloc[-1]),
                 "median_gap": str(gd.median()), "max_gap": str(gd.max()), "max_gap_at": str(lido.t[gd.idxmax()]),
                 "decreases": int(dec.sum()), "decrease_rows": [{"t": str(lido.t[i]), "d": float(lido.eth_per_wsteth.diff()[i])} for i in lido.index[dec][:5]],
                 "rate_min": float(lido.eth_per_wsteth.min()), "rate_max": float(lido.eth_per_wsteth.max()),
                 "dup_tx": int(lido.tx_hash.duplicated().sum()),
                 "inverse_consistency_max_err": float((lido.eth_per_wsteth * lido.wsteth_per_eth - 1).abs().max())}
else:
    R["lido"] = {"found": False}

# ---------------- legacy gas ----------------
leg = {}
for p in list(D.rglob("ether_gas*.csv")) + list((D.parent / "contracts" / "data").glob("ether_gas*.csv")):
    g = pd.read_csv(p)
    leg[str(p)] = {"cols": list(g.columns), "rows": len(g), "first": str(g.iloc[0, 0]), "last": str(g.iloc[-1, 0])}
R["legacy_gas"] = leg
R["coinbase_or_1s_present"] = [str(p) for p in D.rglob("*") if re.search(r"coinbase|1s\.csv|_1s", p.name, re.I)]

# ---------------- swaps ----------------
sw = {}; swaps = {}
usecols = ["id", "timestamp", "logIndex", "amount0", "amount1", "amountUSD", "sqrtPriceX96", "tick", "tx_blockNumber", "tx_gasUsed", "tx_gasPrice", "tx_id"]
for pool in POOLS:
    f = D / "swaps_data" / f"swaps_{pool}.csv"
    if not f.exists():
        sw[pool] = {"found": False}; continue
    hdr = list(pd.read_csv(f, nrows=0).columns)
    df = pd.read_csv(f, usecols=usecols, dtype={"id": str, "sqrtPriceX96": str, "tx_id": str})
    ts, unit = parse_time(df["timestamp"]); df["t"] = ts
    df["sq"] = df.sqrtPriceX96.astype(float)
    n = len(df)
    dup_id = int(df.id.duplicated().sum()); dup_txlog = int(df.duplicated(["tx_id", "logIndex"]).sum())
    idcheck = float((df.id == df.tx_id + "-" + df.logIndex.astype(str)).mean())
    df = df.sort_values(["t", "logIndex"]).reset_index(drop=True)
    df["month"] = df.t.dt.strftime("%Y-%m")
    gp = df.t.diff()
    # parts consistency
    parts = D / "swaps_data" / pool / "parts"
    st = [json.load(open(p)) for p in sorted(parts.glob("*.state.json"))]
    parts_rows = int(sum(s["rows"] for s in st)); parts_done = all(s.get("done") for s in st)
    sw[pool] = {"header": hdr, "rows": n, "unit": unit, "first": str(df.t.iloc[0]), "last": str(df.t.iloc[-1]),
                "monthly": {k: int(v) for k, v in df.groupby("month").size().items()},
                "largest_gap": str(gp.max()), "largest_gap_at": str(df.t[gp.idxmax()]),
                "top5_gaps": [{"end": str(df.t[i]), "gap": str(v)} for i, v in gp.sort_values(ascending=False).head(5).items()],
                "dup_id": dup_id, "dup_txid_logindex": dup_txlog, "id_equals_txid_dash_logindex_share": idcheck,
                "block_min": int(df.tx_blockNumber.min()), "block_max": int(df.tx_blockNumber.max()),
                "unique_blocks": int(df.tx_blockNumber.nunique()),
                "gasUsed_zero_share": float((df.tx_gasUsed == 0).mean()), "gasUsed_nan": int(df.tx_gasUsed.isna().sum()),
                "gasUsed_med": float(df.tx_gasUsed.median()),
                "gasPrice_zero_share": float((df.tx_gasPrice == 0).mean()), "gasPrice_med_gwei": float(df.tx_gasPrice.median() / 1e9),
                "amount_zero_share": float(((df.amount0 == 0) | (df.amount1 == 0)).mean()),
                "amountUSD_zero_share": float((df.amountUSD == 0).mean()),
                "nan_rows": int(df[["amount0", "amount1", "sq", "tick"]].isna().any(axis=1).sum()),
                "parts_files": len(st), "parts_rows_sum": parts_rows, "parts_all_done": parts_done,
                "rows_outside_study": int(((df.t < STUDY0) | (df.t >= STUDY1)).sum()),
                "tx_ts_equals_ts_share": None}
    swaps[pool] = df
R["swaps"] = sw

# secrets scan (paths only): small text files under data/ and repo .env
sec = []
pat = re.compile(r"(infura\.io/v3/[0-9a-f]{16,}|alchemy\.com/v2/[\w-]{16,}|api[_-]?key\s*[=:]\s*['\"]?[\w-]{16,}|PYTH_API_KEY\s*=\s*\S+)", re.I)
for p in D.rglob("*"):
    if p.is_file() and p.suffix in {".py", ".json", ".txt", ".md", ".env", ".log", ".toml"} and p.stat().st_size < 2e6:
        try:
            if pat.search(p.read_text(errors="ignore")): sec.append(str(p))
        except Exception: pass
for p in D.parent.rglob(".env"):
    if ".venv" not in str(p): sec.append(str(p) + " (.env file; contents not read)")
R["possible_secret_files"] = sec

# ---------------- Level 2 ----------------
def ref_series(pool):
    """returns DataFrame[avail (UTC), ref (pool orientation: token1 per token0), ref_alt]"""
    if pool == "eth_usdc_005": k = klines["ETHUSDC"]; p = k.close
    elif pool == "usdc_usdt_0001":
        k = klines["USDCUSDT"]; p = (k.quote_volume / k.volume).where(k.volume > 0, k.close)
    elif pool == "eth_wbtc_030": k = klines["ETHBTC"]; p = k.close
    else:
        if lido is None: return None
        return pd.DataFrame({"avail": lido.t, "ref": lido.wsteth_per_eth, "ref_alt": lido.eth_per_wsteth})
    return pd.DataFrame({"avail": p.index + pd.Timedelta(minutes=1), "ref": p.values})  # bar open T is only complete at T+60s

def join_ref(df, pool, tol):
    rs = ref_series(pool)
    if rs is None: return None
    rs = rs.sort_values("avail"); rs["avail"] = rs["avail"].astype("datetime64[us, UTC]")
    left = df[["t"]].sort_values("t").reset_index(); left["t"] = left["t"].astype("datetime64[us, UTC]")
    m = pd.merge_asof(left, rs, left_on="t", right_on="avail", direction="backward", tolerance=tol)
    m = m.set_index("index").sort_index()
    return m

L2 = {}
samples = {"month10_week": (pd.Timestamp("2026-06-15", tz="UTC"), pd.Timestamp("2026-06-22", tz="UTC")),
           "month12_week": (pd.Timestamp("2026-08-10", tz="UTC"), pd.Timestamp("2026-08-17", tz="UTC"))}
for pool, (d0, d1, fee) in POOLS.items():
    if pool not in swaps: continue
    df = swaps[pool].copy()
    df["px"] = (df.sq / 2 ** 96) ** 2 * 10.0 ** (d0 - d1)
    df["tradepx"] = (df.amount1 / df.amount0).abs()
    tol = pd.Timedelta(days=3) if pool == "eth_wsteth_001" else pd.Timedelta(minutes=5)
    m = join_ref(df, pool, tol)
    out = {"fee": fee}
    if m is None:
        L2[pool] = {"error": "no reference"}; continue
    df["ref"] = m.ref; df["ref_alt"] = m.get("ref_alt", pd.Series(np.nan, index=df.index))
    df["ref_age_s"] = (df.t - m.avail).dt.total_seconds()
    df["split"] = split_of(df.t)
    df["fh_hit"] = df.tx_blockNumber.isin(fh_blocks)
    out["join_coverage"] = {s: {"n": int((df.split == s).sum()),
                                "ref_found": float(df.ref[df.split == s].notna().mean()),
                                "fee_history_found": float(df.fh_hit[df.split == s].mean())} for s in ["train", "val", "test"]}
    out["ref_age_s_med_p99"] = [float(df.ref_age_s.median()), float(df.ref_age_s.quantile(.99))]
    for name, (s0, s1) in samples.items():
        g = df[(df.t >= s0) & (df.t < s1) & df.ref.notna()]
        if len(g) == 0: out[name] = {"n": 0}; continue
        dev = np.log(g.px / g.ref)          # direct orientation
        dinv = np.log(g.px * g.ref)         # if reference is inverted
        r = {"n": int(len(g)), "implied_px_med": float(g.px.median()), "ref_med": float(g.ref.median()),
             "med_abs_logdev_direct": float(dev.abs().median()), "med_signed_logdev_direct": float(dev.median()),
             "med_abs_logdev_if_inverted_ref": float(dinv.abs().median()),
             "fee_multiples_direct": float(dev.abs().median() / fee),
             "amounts_vs_sqrtprice_med_ratio": float((g.tradepx / g.px).median())}
        if pool == "eth_wsteth_001":
            dalt = np.log(g.px * g.ref_alt)  # pool token1/token0 vs ETH-per-wstETH
            r["med_abs_logdev_using_eth_per_wsteth_inverted"] = float(dalt.abs().median())
        out[name] = r
    # E6 feasibility: months 11-12, deviation vs fee tier
    t = df[(df.t >= VAL_END) & df.ref.notna()].copy()
    t["dev"] = np.log(t.px / t.ref)
    cand = t[t.dev.abs() > fee].copy()
    cand["surplus_proxy"] = cand.dev.abs() * cand.amountUSD
    e6 = {"test_swaps_with_ref": int(len(t)), "candidates_dev_gt_fee": int(len(cand)),
          "candidate_share": float(len(cand) / max(len(t), 1))}
    if len(cand) >= 4:
        q = pd.qcut(cand.surplus_proxy.rank(method="first"), 4, labels=False)
        e6["by_surplus_quartile"] = [int((q == i).sum()) for i in range(4)]
        e6["surplus_proxy_quartile_edges_usd"] = [float(x) for x in cand.surplus_proxy.quantile([.25, .5, .75]).values]
    for mth in ["2026-07", "2026-08"]:
        e6[f"cand_{mth}"] = int((cand.month == mth).sum())
    e6["cand_dev_gt_2fee"] = int((cand.dev.abs() > 2 * fee).sum())
    e6["cand_ge_100"] = bool(len(cand) >= 100); e6["cand_ge_400"] = bool(len(cand) >= 400)
    out["e6"] = e6
    # E1 helpers: rows per split, share of swaps with liquidity/fee columns
    out["rows_per_split"] = {s: int((df.split == s).sum()) for s in ["train", "val", "test"]}
    L2[pool] = out
R["level2"] = L2

# swap block range vs fee_history
if fh is not None:
    for pool, df in swaps.items():
        R["swaps"][pool]["fee_history_covers_block_range"] = bool(
            R["fee_history"]["min_block"] <= df.tx_blockNumber.min() and R["fee_history"]["max_block"] >= df.tx_blockNumber.max())
        R["swaps"][pool]["swap_blocks_missing_in_fee_history"] = int((~pd.Series(df.tx_blockNumber.unique()).isin(fh_blocks)).sum())

json.dump(R, open(a.out, "w"), indent=1, default=str)
print("wrote", a.out)
