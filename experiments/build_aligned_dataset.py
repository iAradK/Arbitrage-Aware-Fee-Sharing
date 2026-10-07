#!/usr/bin/env python3
"""Build one aligned parquet per pool (swaps + lookahead-free reference + gas + offsets + split).

Run from the repo root:  .venv/Scripts/python.exe experiments/build_aligned_dataset.py [--pools a,b]
Outputs: cache/aligned/<pool>.parquet, cache/block_gas.parquet, results/data_qc.md, config/data_manifest.json
Reads data/ only; no network. Test-month statistics in the QC report are limited to counts and join rates.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import data_io, refprice  # noqa: E402
from common.pools import (CACHE, OFFSET_MIN_SWAPS, OFFSET_WINDOWS, POOLS, REF_LAGS_MIN, RESULTS,  # noqa: E402
                          STUDY_END, STUDY_START, split_of)

AUDIT = ROOT / "audit" / "offset_results.json"


def _bars(symbol, kind):
    return refprice.bar_series(data_io.load_klines(symbol), kind)


def build_pool(pool, gas, lido, bars):
    df = data_io.load_swaps(pool)
    n_raw = len(df)
    dup = int(df.duplicated(["tx_hash", "log_index"]).sum())
    assert dup == 0, f"{pool.key}: {dup} duplicate (tx_hash, log_index)"
    assert df["ts"].is_monotonic_increasing, f"{pool.key}: timestamps not monotone in (block, log_index)"

    t = pd.DatetimeIndex(df["ts"])
    # reference price at lags on the minute grid
    for d in REF_LAGS_MIN:
        if pool.ref_kind == "lido":
            df[f"p_ref_lag_{d}"] = refprice.lookup_lido(lido, t, d, "wsteth_per_eth")
        else:
            df[f"p_ref_lag_{d}"] = refprice.lookup_bars(bars[pool.ref_symbol], t, d)
    df["p_ref"] = df["p_ref_lag_0"]
    if pool.ref_kind == "lido":
        ev = lido.set_index("t")["wsteth_per_eth"]
        idx = ev.index.searchsorted(t, side="right") - 1
        df["ref_age_s"] = (t - ev.index[idx]).total_seconds()
    else:
        df["ref_age_s"] = (t - (refprice.last_completed_open(t) + refprice.MIN)).total_seconds()

    # the only rows allowed to lack a reference are the first minutes of the window (no completed bar yet)
    early0 = df["ts"] < STUDY_START + pd.Timedelta(minutes=1)
    bad0 = df["p_ref"].isna() & ~early0
    assert not bad0.any(), f"{pool.key}: {int(bad0.sum())} swaps without a reference bar"
    dropped_warmup = int(df["p_ref"].isna().sum())
    df = df[df["p_ref"].notna()].reset_index(drop=True)
    t = pd.DatetimeIndex(df["ts"])
    for d in REF_LAGS_MIN:
        warm = df["ts"] < STUDY_START + pd.Timedelta(minutes=d + 1)
        assert not (df[f"p_ref_lag_{d}"].isna() & ~warm).any(), f"{pool.key}: lag {d} missing after warm-up"

    # price bookkeeping
    df["price_pre"] = df["price"].shift(1)
    df["post_dev"] = np.log(df["price"] / df["p_ref"])
    df["pre_dev"] = np.log(df["price_pre"] / df["p_ref"])
    for name, w in OFFSET_WINDOWS.items():
        df[f"offset_{name}"] = refprice.trailing_offset(df["ts"], df["block"], df["post_dev"], w, OFFSET_MIN_SWAPS).to_numpy()

    # gas at the swap block
    g = gas.set_index("block")
    j = g.reindex(df["block"])
    assert j["gas_price_wei"].notna().all(), f"{pool.key}: swap blocks missing from the block table"
    df["gas_price_wei"] = j["gas_price_wei"].to_numpy()
    df["base_fee_wei"] = j["base_fee_wei"].to_numpy()
    df["tip_p50_wei"] = j["tip_p50_wei"].to_numpy()
    df["gas_source"] = j["gas_source"].to_numpy()
    df["block_ts_match"] = (j["timestamp"].to_numpy() == df["ts"].to_numpy())

    # USD and numeraire conversions (all lookahead-free: last completed bar / forward-filled rate)
    eth_usd = refprice.lookup_bars(bars["ETHUSDC"], t)
    df["eth_usd"] = eth_usd
    if pool.key == "eth_usdc_005":
        df["eth_in_num"], df["usd_per_num"] = eth_usd, 1.0
    elif pool.key == "usdc_usdt_0001":
        df["eth_in_num"], df["usd_per_num"] = refprice.lookup_bars(bars["ETHUSDT"], t), 1.0  # USDT ~ 1 USD
    elif pool.key == "eth_wbtc_030":
        df["eth_in_num"], df["usd_per_num"] = refprice.lookup_bars(bars["ETHBTC"], t), refprice.lookup_bars(bars["BTCUSDT"], t)
    else:
        df["eth_in_num"] = refprice.lookup_lido(lido, t, 0, "wsteth_per_eth")
        df["usd_per_num"] = eth_usd * refprice.lookup_lido(lido, t, 0, "eth_per_wsteth")
    for c in ("eth_usd", "eth_in_num", "usd_per_num"):
        assert df[c].notna().all(), f"{pool.key}: missing {c}"

    df["split"] = split_of(df["ts"]).to_numpy()
    df.insert(0, "pool", pool.key)
    keep = ["pool", "block", "ts", "log_index", "tx_hash", "amount0", "amount1", "amountUSD", "sqrtPriceX96", "tick",
            "price", "price_pre", "p_ref", *[f"p_ref_lag_{d}" for d in REF_LAGS_MIN], "ref_age_s", "post_dev", "pre_dev",
            *[f"offset_{n}" for n in OFFSET_WINDOWS], "gas_price_wei", "base_fee_wei", "tip_p50_wei", "gas_source",
            "eth_usd", "eth_in_num", "usd_per_num", "tx_gas_price_wei", "block_ts_match", "split"]
    out = df[keep].rename(columns={"ts": "timestamp"})
    return out, {"rows_raw": n_raw, "dropped_warmup": dropped_warmup, "dups": dup}


def check_against_audit(pool, df):
    """Monthly signed post-swap deviation medians must match audit/offset_results.json (same definition)."""
    if not AUDIT.exists():
        return {"status": "audit file absent"}
    ref = json.load(open(AUDIT))[pool.key]["signed_post_dev_by_month"]
    mine = (df.assign(m=df["timestamp"].dt.strftime("%Y-%m")).groupby("m")["post_dev"].median() * 1e4)
    # the audit (2026-09-24) covered whole months 2025-09 to 2026-08; compare only months that lie entirely inside the
    # study window (a partial month, e.g. 1-26 July 2026, has a different median)
    def _whole(m):
        m0 = pd.Timestamp(m + "-01", tz="UTC")
        return m0 >= STUDY_START and m0 + pd.offsets.MonthBegin(1) <= STUDY_END
    common_months = [m for m in ref if m in mine.index and _whole(m)]
    worst = max(abs(mine[m] - ref[m]["med_bp"]) for m in common_months)
    assert worst < 0.05, f"{pool.key}: monthly median deviation differs from audit by {worst:.3f} bp"
    return {"max_abs_diff_bp": float(worst)}


def qc_markdown(qc):
    L = ["# Data QC (aligned datasets)", "",
         "Generated by `experiments/build_aligned_dataset.py`. Test months show counts and join rates only "
         "(DECISIONS D15); deviation and offset summaries are train and validation only.", ""]
    L += ["## Rows by split", "", "| pool | train | valid | test | total |", "|---|---|---|---|---|"]
    for k, q in qc.items():
        c = q["rows_by_split"]
        L.append(f"| {k} | {c.get('train',0):,} | {c.get('valid',0):,} | {c.get('test',0):,} | {sum(c.values()):,} |")
    L += ["", "## Joins and integrity", "",
          "| pool | reference join | block-table join | swaps with real tip data | swap ts == block ts | duplicates | warm-up rows dropped | audit match (max bp diff) |",
          "|---|---|---|---|---|---|---|---|"]
    for k, q in qc.items():
        L.append(f"| {k} | 100% | 100% | {q['tip_real_share']:.1%} (test: {q['tip_real_share_test']:.1%}) | {q['ts_match']:.4%} | {q['dups']} | {q['dropped_warmup']} | {q['audit'].get('max_abs_diff_bp', 'n/a')} |")
    L += ["", "## Reference age at swap (seconds, median / P99)", "", "| pool | median | P99 |", "|---|---|---|"]
    for k, q in qc.items():
        L.append(f"| {k} | {q['ref_age_med']:.0f} | {q['ref_age_p99']:.0f} |")
    L += ["", "## Signed post-swap deviation, train + valid (bp)", "", "| pool | median | P5 | P95 | \\|dev\\| median |", "|---|---|---|---|---|"]
    for k, q in qc.items():
        s = q["dev_trainvalid"]
        L.append(f"| {k} | {s['med']:.2f} | {s['p5']:.2f} | {s['p95']:.2f} | {s['abs_med']:.2f} |")
    L += ["", "## Trailing-offset coverage (share of swaps with a defined offset, train + valid)", "",
          "| pool | 6h | 24h | 7d |", "|---|---|---|---|"]
    for k, q in qc.items():
        c = q["offset_cov"]
        L.append(f"| {k} | {c['6h']:.1%} | {c['24h']:.1%} | {c['7d']:.1%} |")
    L += ["", "## Caveats carried into every report", "",
          "- WBTC and wstETH reference basis is not stable; a constant train offset is not used. Results are reported raw and with the trailing offset.",
          "- USDC/USDT: Binance resolves about 1 bp (= pool fee); minute VWAP is used and opportunity counts use a 2x-fee threshold.",
          "- wstETH reference is the Lido contract rate (stETH = 1 ETH), a daily step, forward-filled.",
          "- `tx_gasUsed` is 0 in all swap rows; gas units are a configured parameter.",
          "- Blocks without fee-history tips (none inside the window since 2026-10-01) would get `base_fee + frozen tip` (`gas_source`).",
          "- Pyth on-chain events are complete over the window (BigQuery cross-check, NOTES/2026-10-01_bigquery_crosscheck.md)."]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", default=",".join(POOLS))
    a = ap.parse_args()
    CACHE.joinpath("aligned").mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(exist_ok=True)

    gas = data_io.build_block_gas_table()
    gas.to_parquet(CACHE / "block_gas.parquet")
    lido = data_io.load_lido()
    bars = {"ETHUSDC": _bars("ETHUSDC", "binance_close"), "ETHUSDT": _bars("ETHUSDT", "binance_close"),
            "ETHBTC": _bars("ETHBTC", "binance_close"), "BTCUSDT": _bars("BTCUSDT", "binance_close"),
            "USDCUSDT": _bars("USDCUSDT", "binance_vwap")}
    qc = {}
    for key in a.pools.split(","):
        pool = POOLS[key]
        df, info = build_pool(pool, gas, lido, bars)
        aud = check_against_audit(pool, df)
        df.to_parquet(CACHE / "aligned" / f"{key}.parquet", index=False)
        tv = df[df["split"] != "test"]
        real = df["gas_source"] == "fee_history"
        qc[key] = {
            "rows_by_split": df["split"].value_counts().to_dict(), "dups": info["dups"], "dropped_warmup": info["dropped_warmup"],
            "tip_real_share": float(real.mean()), "tip_real_share_test": float(real[df["split"] == "test"].mean()),
            "ts_match": float(df["block_ts_match"].mean()), "audit": aud,
            "ref_age_med": float(df["ref_age_s"].median()), "ref_age_p99": float(df["ref_age_s"].quantile(0.99)),
            "dev_trainvalid": {"med": float(tv["post_dev"].median() * 1e4), "p5": float(tv["post_dev"].quantile(.05) * 1e4),
                               "p95": float(tv["post_dev"].quantile(.95) * 1e4), "abs_med": float(tv["post_dev"].abs().median() * 1e4)},
            "offset_cov": {n: float(tv[f"offset_{n}"].notna().mean()) for n in OFFSET_WINDOWS},
        }
        print(key, len(df), "rows", qc[key]["rows_by_split"], "audit", aud, flush=True)
    (RESULTS / "data_qc.md").write_text(qc_markdown(qc), encoding="utf-8")
    print("wrote results/data_qc.md")


if __name__ == "__main__":
    main()
