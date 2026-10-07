"""Loaders for the raw inputs, plus SHA-256 hashing for manifests. Never writes under data/."""
from __future__ import annotations

import glob
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from .pools import DATA, STUDY_END, STUDY_START


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def load_klines(symbol: str) -> pd.DataFrame:
    p = DATA / "data" / "binance" / f"{symbol}_1m.csv.gz"
    kl = pd.read_csv(p, index_col=0, parse_dates=True)
    kl.index = pd.DatetimeIndex(kl.index).tz_convert("UTC") if kl.index.tz is not None else pd.DatetimeIndex(kl.index).tz_localize("UTC")
    kl.index = kl.index.as_unit("ns")
    kl = kl[(kl.index >= STUDY_START - pd.Timedelta(days=1)) & (kl.index < STUDY_END)]
    return kl


def load_lido() -> pd.DataFrame:
    ev = pd.read_csv(DATA / "data" / "lido" / "wsteth_rate_events.csv")
    ev["t"] = pd.to_datetime(ev["block_time"], utc=True)
    ev["t"] = ev["t"].dt.as_unit("ns")
    return ev[["t", "eth_per_wsteth", "wsteth_per_eth"]].sort_values("t").reset_index(drop=True)


def load_blocks() -> pd.DataFrame:
    """BigQuery blocks export: number, timestamp (UTC), base_fee_per_gas, gas_used, gas_limit."""
    files = sorted(glob.glob(str(DATA / "data" / "gas" / "bq-results-*.csv")))
    if not files:
        raise FileNotFoundError("no bq-results-*.csv in data/data/gas")
    # several exports may cover different block ranges (2026-10: July-August 2025 was exported separately);
    # concatenate them and keep one row per block, asserting that overlapping rows agree
    b = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    dup = b[b.duplicated("number", keep=False)]
    if not dup.empty and (dup.groupby("number").nunique() > 1).any().any():
        raise AssertionError("bq-results files disagree on an overlapping block")
    b = b.drop_duplicates("number")
    b["timestamp"] = pd.to_datetime(b["timestamp"], utc=True).dt.as_unit("ns")
    return b.rename(columns={"number": "block"}).sort_values("block").reset_index(drop=True)


def load_fee_history() -> pd.DataFrame:
    """All per-block fee-history files (fee_history*.csv), concatenated and de-duplicated."""
    files = sorted(glob.glob(str(DATA / "data" / "gas" / "fee_history*.csv")))
    parts = [pd.read_csv(f, usecols=["block_number", "base_fee_wei", "tip_p10_wei", "tip_p50_wei", "tip_p90_wei",
                                     "gas_used_ratio"]) for f in files]
    fh = pd.concat(parts).drop_duplicates("block_number").sort_values("block_number")
    return fh.rename(columns={"block_number": "block"}).reset_index(drop=True)


def build_block_gas_table(tip_window_days: int = 7) -> pd.DataFrame:
    """One row per block: timestamp, base fee, tip_p50 and gas price (= base_fee + tip_p50), in wei.

    Blocks without a fee-history row (none inside the study window since 2026-10-01) get base fee from the BigQuery
    export and the trailing-`tip_window_days` median tip_p50 frozen at the last block that has real tips
    (no lookahead). `gas_source` records which rule applied. Base fees are asserted to match exactly.
    """
    b = load_blocks()
    fh = load_fee_history()
    m = b.merge(fh, on="block", how="left")
    have = m["tip_p50_wei"].notna()
    same = (m.loc[have, "base_fee_wei"] == m.loc[have, "base_fee_per_gas"])
    if not same.all():
        raise AssertionError(f"{int((~same).sum())} blocks: fee_history base fee != BigQuery base fee")
    last_real_ts = m.loc[have, "timestamp"].max()
    recent = m[have & (m["timestamp"] > last_real_ts - pd.Timedelta(days=tip_window_days))]
    frozen_tip = float(recent["tip_p50_wei"].median())
    m["tip_p50_wei"] = m["tip_p50_wei"].fillna(frozen_tip)
    m["gas_source"] = np.where(have, "fee_history", "base+frozen_tip")
    m["base_fee_wei"] = m["base_fee_per_gas"].astype("float64")
    m["gas_price_wei"] = m["base_fee_wei"] + m["tip_p50_wei"]
    return m[["block", "timestamp", "base_fee_wei", "tip_p50_wei", "gas_price_wei", "gas_used_ratio", "gas_source"]]


def load_swaps(pool) -> pd.DataFrame:
    """Raw swap rows for one pool, sorted by (block, log_index), study window only."""
    df = pd.read_csv(pool.swaps_path, usecols=["id", "timestamp", "logIndex", "amount0", "amount1", "amountUSD",
                                               "sqrtPriceX96", "tick", "tx_id", "tx_blockNumber", "tx_gasPrice"],
                     dtype={"sqrtPriceX96": str, "id": str, "tx_id": str})
    df = df.rename(columns={"tx_blockNumber": "block", "logIndex": "log_index", "tx_id": "tx_hash",
                            "tx_gasPrice": "tx_gas_price_wei"})
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.as_unit("ns")
    df = df[(df["ts"] >= STUDY_START) & (df["ts"] < STUDY_END)]
    df = df.sort_values(["block", "log_index"]).reset_index(drop=True)
    df["sqrt_p"] = df["sqrtPriceX96"].astype(float) / 2.0**96
    df["price"] = df["sqrt_p"] ** 2 * 10.0 ** (pool.dec0 - pool.dec1)
    return df
