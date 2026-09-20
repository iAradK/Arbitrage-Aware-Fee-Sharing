#!/usr/bin/env python3
"""Build hook-level replay vectors from Uniswap v4, Binance, Pyth, and gas data.

The output matches hook_level_replay/example/events.csv:
  event_id,timestamp,block_number,surplus_hat_usd,true_surplus_usd,
  execution_margin_hat_usd,lp_loss_usd,gas_price_gwei,eth_usd,
  pool_price,reference_price,oracle_price,liquidity

Important modeling note
-----------------------
The uploaded Uniswap file contains poolDayDatas rather than individual swap states
or initialized-tick data. Therefore this script estimates arbitrage surplus using the
active-liquidity virtual-reserve approximation:

    x = L / sqrt(P),  y = L * sqrt(P)

with the correct Q96 and token-decimal conversions. This is exact only while the
corrective trade remains inside the current active-liquidity interval. For a stronger
swap-level experiment, supply event-level states and tick-crossing liquidity.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

Q96 = float(2**96)
BINANCE_COLUMNS = [
    "open_time_us", "open", "high", "low", "close", "volume",
    "close_time_us", "quote_volume", "trades", "taker_buy_base",
    "taker_buy_quote", "ignore",
]


def read_binance(paths: Iterable[str]) -> pd.DataFrame:
    """Read Binance Vision headerless 1-minute kline CSV files."""
    parts = []
    for path in paths:
        df = pd.read_csv(path, header=None, names=BINANCE_COLUMNS)
        for c in ["open_time_us", "open", "high", "low", "close"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        # Current Binance archives may use microseconds; older ones often use ms.
        unit = "us" if df["open_time_us"].dropna().median() > 1e14 else "ms"
        df["timestamp"] = pd.to_datetime(df["open_time_us"], unit=unit, utc=True)
        df["reference_price"] = df["close"]
        parts.append(df[["timestamp", "reference_price"]])
    out = pd.concat(parts, ignore_index=True).dropna().sort_values("timestamp")
    return out.drop_duplicates("timestamp", keep="last")


def read_pyth(path: str) -> pd.DataFrame:
    """Read common processed and raw Pyth CSV schemas.

    Accepted price columns include pyth_price, pyth_close, price, oracle_price,
    parsed_price, aggregate_price, and close. For OHLC Pyth files, pyth_close
    is preferred. If a raw integer price is
    paired with an exponent column (pyth_expo or expo), the exponent is
    applied automatically.
    """
    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]

    price_candidates = [
        "pyth_price", "oracle_price", "parsed_price",
        "aggregate_price", "pyth_close", "pyth_price_close",
        "price", "close", "pyth_open",
    ]
    price_col = next((c for c in price_candidates if c in df.columns), None)
    if price_col is None:
        raise ValueError(
            "Could not find a Pyth price column. Found columns: "
            + ", ".join(df.columns)
            + ". Expected one of: "
            + ", ".join(price_candidates)
        )

    timestamp_candidates = [
        "timestamp", "datetime", "pyth_publish_time",
        "publish_time", "publishTime", "time", "date",
    ]
    timestamp_col = next((c for c in timestamp_candidates if c in df.columns), None)
    if timestamp_col is None:
        raise ValueError(
            "Could not find a Pyth timestamp column. Found columns: "
            + ", ".join(df.columns)
        )

    raw_ts = df[timestamp_col]
    numeric_ts = pd.to_numeric(raw_ts, errors="coerce")
    if numeric_ts.notna().mean() > 0.8:
        median = numeric_ts.dropna().abs().median()
        if median > 1e17:
            unit = "ns"
        elif median > 1e14:
            unit = "us"
        elif median > 1e11:
            unit = "ms"
        else:
            unit = "s"
        df["timestamp"] = pd.to_datetime(numeric_ts, unit=unit, utc=True, errors="coerce")
    else:
        df["timestamp"] = pd.to_datetime(raw_ts, utc=True, errors="coerce")

    price = pd.to_numeric(df[price_col], errors="coerce")
    expo_col = next((c for c in ["pyth_expo", "expo", "exponent"] if c in df.columns), None)
    # Apply an exponent only when the chosen price appears to be a raw integer.
    if expo_col is not None and price_col in {"price", "aggregate_price"}:
        expo = pd.to_numeric(df[expo_col], errors="coerce").fillna(0)
        price = price * np.power(10.0, expo)
    df["oracle_price"] = price

    publish_col = next((c for c in ["pyth_publish_time", "publish_time", "publishTime"] if c in df.columns), None)
    if publish_col is not None:
        publish_raw = pd.to_numeric(df[publish_col], errors="coerce")
        median = publish_raw.dropna().abs().median() if publish_raw.notna().any() else np.nan
        if pd.notna(median):
            if median > 1e17:
                unit = "ns"
            elif median > 1e14:
                unit = "us"
            elif median > 1e11:
                unit = "ms"
            else:
                unit = "s"
            publish = pd.to_datetime(publish_raw, unit=unit, utc=True, errors="coerce")
            df["oracle_age_seconds"] = (df["timestamp"] - publish).dt.total_seconds()
        else:
            df["oracle_age_seconds"] = np.nan
    else:
        df["oracle_age_seconds"] = np.nan

    if "status" in df.columns:
        status = df["status"].astype(str).str.lower()
        accepted = status.isin(["ok", "trading", "valid", "1", "true"])
        if accepted.any():
            df = df[accepted]

    return (
        df[["timestamp", "oracle_price", "oracle_age_seconds"]]
        .dropna(subset=["timestamp", "oracle_price"])
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
    )


def read_daily_series(path: str, date_col: str, value_col: str, output_col: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df[date_col], utc=True, errors="coerce")
    df[output_col] = pd.to_numeric(df[value_col], errors="coerce")
    return df[["timestamp", output_col]].dropna().sort_values("timestamp")


def read_pool(path: str, token0_decimals: int, token1_decimals: int) -> pd.DataFrame:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("data", {}).get("poolDayDatas", [])
    if not rows:
        raise ValueError("No data.poolDayDatas rows found in Uniswap JSON")
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(pd.to_numeric(df["date"]), unit="s", utc=True)
    df["event_id"] = df["id"].astype(str)
    df["pool_price"] = pd.to_numeric(df["token0Price"], errors="coerce")
    df["liquidity"] = pd.to_numeric(df["liquidity"], errors="coerce")
    df["sqrt_price_x96"] = pd.to_numeric(df["sqrtPrice"], errors="coerce")
    df["block_number"] = ""  # poolDayDatas does not expose a block number.

    # Virtual human-unit reserves for token0 (USDT) and token1 (WETH).
    df["virtual_reserve_token0"] = (
        df["liquidity"] * Q96 / df["sqrt_price_x96"] / (10**token0_decimals)
    )
    df["virtual_reserve_token1"] = (
        df["liquidity"] * df["sqrt_price_x96"] / Q96 / (10**token1_decimals)
    )
    return df[[
        "event_id", "timestamp", "block_number", "pool_price", "liquidity",
        "virtual_reserve_token0", "virtual_reserve_token1",
    ]].dropna(subset=["timestamp", "pool_price", "liquidity"]).sort_values("timestamp")


def gross_surplus_usd(x_usdt: pd.Series, y_eth: pd.Series, external_price: pd.Series, fee_bps: float) -> pd.Series:
    """Optimal one-range CPMM arbitrage profit in USD, optionally with input fee."""
    x = x_usdt.to_numpy(dtype=float)
    y = y_eth.to_numpy(dtype=float)
    p = external_price.to_numpy(dtype=float)
    k = x * y
    a = 1.0 - fee_bps / 10_000.0
    if not (0 < a <= 1):
        raise ValueError("pool_fee_bps must be in [0, 10000)")

    pool_p = x / y
    result = np.zeros_like(p)

    # ETH is cheap in pool: input USDT, receive ETH.
    hi = p > pool_p
    target_x_effective = np.sqrt(p[hi] * k[hi] * a)
    dx = np.maximum(0.0, (target_x_effective - x[hi]) / a)
    eth_out = y[hi] - k[hi] / (x[hi] + a * dx)
    result[hi] = p[hi] * eth_out - dx

    # ETH is expensive in pool: input ETH, receive USDT.
    lo = p < pool_p
    target_y_effective = np.sqrt(k[lo] * a / p[lo])
    dy = np.maximum(0.0, (target_y_effective - y[lo]) / a)
    usdt_out = x[lo] - k[lo] / (y[lo] + a * dy)
    result[lo] = usdt_out - p[lo] * dy

    return pd.Series(np.maximum(result, 0.0), index=x_usdt.index)


def asof_join(left: pd.DataFrame, right: pd.DataFrame, tolerance: str, direction: str = "nearest") -> pd.DataFrame:
    return pd.merge_asof(
        left.sort_values("timestamp"), right.sort_values("timestamp"),
        on="timestamp", direction=direction, tolerance=pd.Timedelta(tolerance),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pool-json", required=True)
    ap.add_argument("--pyth-csv", required=True)
    ap.add_argument("--binance-csv", nargs="+", required=True)
    ap.add_argument("--gas-csv", required=True)
    ap.add_argument("--eth-usd-csv", required=True)
    ap.add_argument("--output", default="hook_events.csv")
    ap.add_argument("--diagnostics", default="hook_events_diagnostics.csv")
    ap.add_argument("--token0-decimals", type=int, default=6)
    ap.add_argument("--token1-decimals", type=int, default=18)
    ap.add_argument("--pool-fee-bps", type=float, default=0.0)
    ap.add_argument("--arb-gas-units", type=float, default=150_000.0,
                    help="Baseline arbitrage gas units, excluding the new hook overhead")
    ap.add_argument("--reservation-margin-usd", type=float, default=0.0)
    ap.add_argument("--gas-column", default="median_gas_price_gwei")
    ap.add_argument("--market-tolerance", default="12h")
    ap.add_argument("--oracle-tolerance", default="12h")
    ap.add_argument("--drop-unmatched", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()

    pool = read_pool(args.pool_json, args.token0_decimals, args.token1_decimals)
    market = read_binance(args.binance_csv)
    oracle = read_pyth(args.pyth_csv)
    gas = read_daily_series(args.gas_csv, "date", args.gas_column, "gas_price_gwei")
    eth = read_daily_series(args.eth_usd_csv, "timestamp", "eth_usd", "eth_usd")

    df = asof_join(pool, market, args.market_tolerance)
    df = asof_join(df, oracle, args.oracle_tolerance)
    df = asof_join(df, gas, "36h")
    df = asof_join(df, eth, "36h")

    required_matches = ["reference_price", "oracle_price", "gas_price_gwei", "eth_usd"]
    df["matched_all_sources"] = df[required_matches].notna().all(axis=1)

    df["true_surplus_usd"] = gross_surplus_usd(
        df["virtual_reserve_token0"], df["virtual_reserve_token1"],
        df["reference_price"], args.pool_fee_bps,
    )
    df["surplus_hat_usd"] = gross_surplus_usd(
        df["virtual_reserve_token0"], df["virtual_reserve_token1"],
        df["oracle_price"], args.pool_fee_bps,
    )
    df["execution_margin_hat_usd"] = (
        args.arb_gas_units * df["gas_price_gwei"] * 1e-9 * df["eth_usd"]
        + args.reservation_margin_usd
    )
    # Arbitrage loss is the natural denominator for this event-level replay.
    df["lp_loss_usd"] = df["true_surplus_usd"]

    diagnostics_cols = [
        "event_id", "timestamp", "matched_all_sources", "pool_price",
        "reference_price", "oracle_price", "gas_price_gwei", "eth_usd",
        "oracle_age_seconds", "true_surplus_usd", "surplus_hat_usd",
        "virtual_reserve_token0", "virtual_reserve_token1",
    ]
    df[diagnostics_cols].to_csv(args.diagnostics, index=False)

    if args.drop_unmatched:
        df = df[df["matched_all_sources"]].copy()

    output_cols = [
        "event_id", "timestamp", "block_number", "surplus_hat_usd",
        "true_surplus_usd", "execution_margin_hat_usd", "lp_loss_usd",
        "gas_price_gwei", "eth_usd", "pool_price", "reference_price",
        "oracle_price", "liquidity",
    ]
    df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df[output_cols].to_csv(args.output, index=False)

    total = len(pool)
    matched = int(df.shape[0])
    print(f"Pool states: {total}")
    print(f"Complete vectors written: {matched}")
    print(f"Output: {args.output}")
    print(f"Diagnostics: {args.diagnostics}")
    if matched < total:
        print("Some pool dates were unmatched. Inspect the diagnostics CSV.")
    print("Surplus model: one-range virtual CPMM approximation")
    print(f"Pool fee: {args.pool_fee_bps:g} bps")
    print(f"Baseline arbitrage gas: {args.arb_gas_units:g} units")


if __name__ == "__main__":
    main()