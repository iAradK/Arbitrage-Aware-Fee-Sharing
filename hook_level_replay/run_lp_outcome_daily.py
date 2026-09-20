#!/usr/bin/env python3
"""
Daily LP-outcome replay for participation-aware arbitrage surplus sharing.

This version is designed to run in two ways:

1. PyCharm Run button / no command-line parameters
   Put these files in the same directory as this script:
     - v4_pool_daily_cpmm.csv
     - v4_nextday_price.csv
     - 15_days_univ4_swaps.json              optional, used to calibrate R

   Then just press Run. The script will use the constants defined below.

2. Command line, with optional overrides
   python run_lp_outcome_daily.py --pool-csv ... --price-csv ...

What the script does:
  - Uses daily pool movements in a CPMM benchmark.
  - Compares baseline AMM, fixed-fee baselines, unconstrained sharing, strict cap, and buffered rule.
  - Can estimate observable gas cost either from a daily gas CSV or from swap-level
    gasPrice values in a Uniswap v4 swap JSON.
  - Can calibrate plausible R regimes from observed swap-level post-gas surplus
    proxies. This does NOT claim to identify the true private reservation profit.
    It only uses observed swaps to choose more defensible sensitivity values.

Important interpretation:
  C_t is observable/estimable execution cost.
  R is a private reservation margin. The script can calibrate candidate R values
  from observed post-gas surplus, but it cannot recover the true R exactly.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd


EPS = 1e-18
SCRIPT_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# PyCharm Run-button constants.
# Edit these values directly; no command-line parameters are required.
# ---------------------------------------------------------------------------
POOL_CSV = "v4_pool_daily_cpmm.csv"
PRICE_CSV = "v4_nextday_price.csv"
GAS_CSV = "ether_gas_4y.csv"
SWAP_JSON = "all_swaps.json"
OUT_DIR = "results_lp_outcomes_v4_auto_r"

LAMBDA_VALUES = [0.75, 0.95]
GAMMA = 0.05
DELTA = 0.0

# Fixed-fee industry baselines. These model ordinary AMM fee tiers that take
# a fixed percentage of the arbitrage swap notional. 0.0005 = 0.05%,
# 0.0030 = 0.30%. You can add 0.01 for a 1.00% stress tier.
FIXED_FEE_RATES = [0.0005, 0.0030]
# For a fair daily arbitrage event comparison, the default fee base is the
# notional of the CPMM price-correcting arbitrage trade. If you prefer to use
# the observed total daily volume from SWAP_JSON, set this to "observed_daily_volume".
FIXED_FEE_BASE = "arbitrage_notional"  # or "observed_daily_volume"

# Your swap JSON contains Jan. 2026 transactions only. When True, the daily
# replay is restricted to this window, so swap-calibrated R and fixed-fee
# diagnostics are evaluated on the same time period. End date is exclusive.
RESTRICT_REPLAY_TO_SWAP_WINDOW = True
SWAP_WINDOW_START = "2026-01-01"
SWAP_WINDOW_END = "2026-01-13"

# Fixed base execution cost in quote-token/USD units.
# If GAS_CSV or swap gas prices are used, keep this at 0.0 unless you
# intentionally want an extra fixed cost on top of gas-based cost.
FIXED_C = 0.0

# Used only if R calibration from swaps is unavailable or disabled.
FIXED_R_VALUES = [250.0, 500.0, 1000.0]

# Gas assumption, because many Graph swap exports have transaction.gasUsed=0.
GAS_UNITS = 150000.0
GAS_MULTIPLIER = 1.0

# R calibration from swap-level observed post-gas surplus proxies.
CALIBRATE_R_FROM_SWAPS = True
R_QUANTILES = [0.25, 0.50, 0.75]
MIN_CALIBRATED_R = 0.0
MAX_CALIBRATED_R = 5000.0

# Filters.
MIN_ABS_LOG_MOVE = 0.0
MIN_SURPLUS = 0.0


@dataclass(frozen=True)
class ReplayParams:
    lambdas: List[float]
    gamma: float
    delta: float
    fixed_c: float
    fixed_r_values: List[float]
    fixed_fee_rates: List[float]
    fixed_fee_base: str
    gas_units: float
    gas_multiplier: float
    min_abs_log_move: float
    min_surplus: float


def resolve_path(path: Optional[str]) -> Optional[str]:
    if path is None or str(path).strip() == "":
        return None
    p = Path(path)
    if p.is_absolute():
        return str(p)
    # Prefer current working directory, then script directory.
    cwd_p = Path.cwd() / p
    if cwd_p.exists():
        return str(cwd_p)
    return str(SCRIPT_DIR / p)


def _first_present(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _normalize_date_column(df: pd.DataFrame, name: str) -> pd.DataFrame:
    df = df.copy()
    date_col = _first_present(df, ["date", "day", "timestamp", "datetime", "block_timestamp", "time"])
    if date_col is None:
        raise ValueError(f"{name}: expected one of date/day/timestamp/datetime/block_timestamp/time columns")

    # Numeric timestamps in these data sets are normally unix seconds.
    if pd.api.types.is_numeric_dtype(df[date_col]) or df[date_col].astype(str).str.fullmatch(r"\d+").all():
        parsed = pd.to_datetime(pd.to_numeric(df[date_col], errors="coerce"), unit="s", utc=True, errors="coerce")
    else:
        parsed = pd.to_datetime(df[date_col], utc=True, errors="coerce")

    df["date"] = parsed.dt.floor("D").dt.tz_localize(None)
    if df["date"].isna().any():
        bad = df[df["date"].isna()].head(3)
        raise ValueError(f"{name}: failed to parse dates. Examples:\n{bad}")
    return df


def _read_csv(path: str, name: str) -> pd.DataFrame:
    if not path:
        raise ValueError(f"missing path for {name}")
    if not os.path.exists(path):
        raise FileNotFoundError(f"{name} not found: {path}")
    return pd.read_csv(path)


def load_inputs(pool_csv: str, price_csv: str, gas_csv: Optional[str]) -> pd.DataFrame:
    pool = _normalize_date_column(_read_csv(pool_csv, "pool_csv"), "pool_csv")
    price = _normalize_date_column(_read_csv(price_csv, "price_csv"), "price_csv")

    price_col = _first_present(price, ["external_price", "price", "close", "eth_usd", "ETH_USD", "binance_price", "reference_price"])
    if price_col is None:
        raise ValueError("price_csv: expected one of external_price/price/close/eth_usd/ETH_USD/binance_price/reference_price")
    price = price[["date", price_col]].rename(columns={price_col: "external_price"})
    price = price.groupby("date", as_index=False).last()

    pool_price_col = _first_present(pool, ["pool_price", "price", "close", "price_y_per_x", "token1_per_token0"])
    sqrt_price_col = _first_present(pool, ["sqrtPriceX96", "sqrt_price_x96", "sqrtPrice", "sqrt_price"])
    reserve_x_col = _first_present(pool, ["reserve_x", "reserve0", "reserve_token0", "x", "amount_x"])
    reserve_y_col = _first_present(pool, ["reserve_y", "reserve1", "reserve_token1", "y", "amount_y"])
    depth_y_col = _first_present(pool, ["depth_y", "tvl_y", "tvl_usd", "totalValueLockedUSD", "liquidity_y", "quote_depth"])
    liquidity_col = _first_present(pool, ["liquidity", "active_liquidity"])

    keep = ["date"]
    for c in [pool_price_col, sqrt_price_col, reserve_x_col, reserve_y_col, depth_y_col, liquidity_col]:
        if c and c not in keep:
            keep.append(c)
    pool = pool[keep].groupby("date", as_index=False).last()

    if pool_price_col is None and sqrt_price_col is not None:
        pool["pool_price"] = (pd.to_numeric(pool[sqrt_price_col], errors="coerce") / (2 ** 96)) ** 2
    elif pool_price_col is not None:
        pool["pool_price"] = pd.to_numeric(pool[pool_price_col], errors="coerce")
    else:
        pool["pool_price"] = np.nan

    if reserve_x_col is not None and reserve_y_col is not None:
        pool["reserve_x"] = pd.to_numeric(pool[reserve_x_col], errors="coerce")
        pool["reserve_y"] = pd.to_numeric(pool[reserve_y_col], errors="coerce")
        missing_price = pool["pool_price"].isna()
        pool.loc[missing_price, "pool_price"] = pool.loc[missing_price, "reserve_y"] / pool.loc[missing_price, "reserve_x"]
    else:
        if depth_y_col is not None:
            depth_y = pd.to_numeric(pool[depth_y_col], errors="coerce")
        elif liquidity_col is not None:
            depth_y = pd.to_numeric(pool[liquidity_col], errors="coerce")
        else:
            raise ValueError("pool_csv must contain either reserve_x,reserve_y OR pool_price plus depth_y/tvl_usd/liquidity")
        pool["reserve_y"] = depth_y
        pool["reserve_x"] = pool["reserve_y"] / pool["pool_price"]

    df = pool.merge(price, on="date", how="inner")

    if gas_csv:
        gas = _normalize_date_column(_read_csv(gas_csv, "gas_csv"), "gas_csv")
        gas_price_col = _first_present(gas, ["gas_price_gwei", "avg_gas_price_gwei", "base_fee_gwei", "gasPriceGwei"])
        gas_price_wei_col = _first_present(gas, ["gasPrice", "gas_price", "gas_price_wei"])
        eth_price_col = _first_present(gas, ["eth_usd_price", "eth_usd", "ETH_USD", "price", "close"])
        if gas_price_col is None and gas_price_wei_col is None:
            raise ValueError("gas_csv: expected gas_price_gwei or gasPrice/gas_price_wei")
        if gas_price_col is not None:
            gas["gas_price_gwei"] = pd.to_numeric(gas[gas_price_col], errors="coerce")
        else:
            gas["gas_price_gwei"] = pd.to_numeric(gas[gas_price_wei_col], errors="coerce") / 1e9
        if eth_price_col is not None:
            gas["eth_usd_price"] = pd.to_numeric(gas[eth_price_col], errors="coerce")
            gas = gas[["date", "gas_price_gwei", "eth_usd_price"]]
        else:
            gas = gas[["date", "gas_price_gwei"]]
        gas = gas.groupby("date", as_index=False).last()
        df = df.merge(gas, on="date", how="left")
    else:
        df["gas_price_gwei"] = np.nan
        df["eth_usd_price"] = np.nan

    df = df.sort_values("date").reset_index(drop=True)
    for c in ["pool_price", "reserve_x", "reserve_y", "external_price"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=["pool_price", "reserve_x", "reserve_y", "external_price"])
    df = df[(df["pool_price"] > 0) & (df["reserve_x"] > 0) & (df["reserve_y"] > 0) & (df["external_price"] > 0)]
    if len(df) < 2:
        raise ValueError("not enough valid matched daily rows after merging inputs")
    return df


def load_swaps_json(path: str) -> pd.DataFrame:
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"swap_json not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    swaps = obj.get("data", {}).get("swaps", obj.get("swaps", []))
    if not swaps:
        raise ValueError(f"no swaps found in {path}; expected data.swaps or swaps")
    rows = []
    for s in swaps:
        tx = s.get("transaction", {}) or {}
        rows.append({
            "id": s.get("id"),
            "timestamp": pd.to_numeric(s.get("timestamp"), errors="coerce"),
            "amount0": pd.to_numeric(s.get("amount0"), errors="coerce"),
            "amount1": pd.to_numeric(s.get("amount1"), errors="coerce"),
            "amountUSD": pd.to_numeric(s.get("amountUSD"), errors="coerce"),
            "sqrtPriceX96": pd.to_numeric(s.get("sqrtPriceX96"), errors="coerce"),
            "tick": pd.to_numeric(s.get("tick"), errors="coerce"),
            "logIndex": pd.to_numeric(s.get("logIndex"), errors="coerce"),
            "sender": s.get("sender"),
            "origin": s.get("origin"),
            "tx_id": tx.get("id"),
            "blockNumber": pd.to_numeric(tx.get("blockNumber"), errors="coerce"),
            "gasUsed_raw": pd.to_numeric(tx.get("gasUsed"), errors="coerce"),
            "gasPrice_wei": pd.to_numeric(tx.get("gasPrice"), errors="coerce"),
        })
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["timestamp", "amount0", "amount1", "amountUSD"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True, errors="coerce")
    df["date"] = df["datetime"].dt.floor("D").dt.tz_localize(None)
    sort_cols = [c for c in ["timestamp", "blockNumber", "logIndex"] if c in df.columns]
    df = df.sort_values(sort_cols).reset_index(drop=True)
    return df



def _parse_date_or_none(value: Optional[str]) -> Optional[pd.Timestamp]:
    if value is None or str(value).strip() == "":
        return None
    return pd.to_datetime(value).tz_localize(None) if pd.to_datetime(value).tzinfo is None else pd.to_datetime(value).tz_convert(None)


def filter_by_date_window(df: pd.DataFrame, start: Optional[str], end: Optional[str], name: str) -> pd.DataFrame:
    start_ts = _parse_date_or_none(start)
    end_ts = _parse_date_or_none(end)
    out = df.copy()
    if start_ts is not None:
        out = out[out["date"] >= start_ts]
    if end_ts is not None:
        out = out[out["date"] < end_ts]
    if out.empty:
        raise ValueError(f"{name}: no rows remain after date filter start={start}, end={end}")
    return out.reset_index(drop=True)


def build_fixed_fee_method_name(rate: float) -> str:
    bps = int(round(rate * 10000))
    return f"fixed_fee_{bps}bps"


def parse_fixed_fee_rate(method: str) -> Optional[float]:
    if not method.startswith("fixed_fee_") or not method.endswith("bps"):
        return None
    bps_str = method[len("fixed_fee_"):-len("bps")]
    try:
        return float(bps_str) / 10000.0
    except ValueError:
        return None


def load_swap_daily_features(
    swap_json: Optional[str],
    daily_df: pd.DataFrame,
    gas_units: float,
    gas_multiplier: float,
    start: Optional[str],
    end: Optional[str],
    out_dir: str,
) -> pd.DataFrame:
    """Attach daily features from the swap trace, mainly observed volume.

    The fixed-fee daily replay can use either the model-implied arbitrage notional
    or observed daily volume. Observed volume is only complete for the supplied
    swap window, which in the user's current data is Jan. 2026.
    """
    df = daily_df.copy()
    df["observed_daily_volume_usd"] = np.nan
    df["observed_daily_swap_count"] = 0
    if not swap_json or not os.path.exists(swap_json):
        return df

    swaps = load_swaps_json(swap_json)
    swaps = filter_by_date_window(swaps, start, end, "swap_json")
    swaps["amountUSD"] = pd.to_numeric(swaps["amountUSD"], errors="coerce").abs()
    daily = swaps.groupby("date", as_index=False).agg(
        observed_daily_volume_usd=("amountUSD", "sum"),
        observed_daily_swap_count=("id", "count"),
    )
    os.makedirs(out_dir, exist_ok=True)
    daily.to_csv(os.path.join(out_dir, "swap_daily_features.csv"), index=False)
    df = df.drop(columns=["observed_daily_volume_usd", "observed_daily_swap_count"], errors="ignore")
    df = df.merge(daily, on="date", how="left")
    df["observed_daily_volume_usd"] = df["observed_daily_volume_usd"].fillna(0.0)
    df["observed_daily_swap_count"] = df["observed_daily_swap_count"].fillna(0).astype(int)
    return df

def estimate_swap_price_y_per_x(swaps: pd.DataFrame, daily_prices: pd.DataFrame) -> pd.DataFrame:
    """Estimate quote/base price from amount0/amount1.

    The v4 subgraph token order can vary. In the user's trace, abs(amount0/amount1)
    is around ETH/USD scale, so we use that when it is close to the daily external
    price. If the inverse is closer, we use the inverse.
    """
    df = swaps.copy()
    ratio = (df["amount0"].abs() / df["amount1"].abs()).replace([np.inf, -np.inf], np.nan)
    inv_ratio = (df["amount1"].abs() / df["amount0"].abs()).replace([np.inf, -np.inf], np.nan)

    price_ref = daily_prices[["date", "external_price"]].drop_duplicates("date")
    df = df.merge(price_ref, on="date", how="left")

    # Choose the orientation closer in log distance to the external price.
    log_r = np.abs(np.log(ratio / df["external_price"]))
    log_inv = np.abs(np.log(inv_ratio / df["external_price"]))
    df["swap_price_y_per_x"] = np.where(log_inv < log_r, inv_ratio, ratio)
    df["swap_price_y_per_x"] = pd.to_numeric(df["swap_price_y_per_x"], errors="coerce")
    return df


def calibrate_r_from_swaps(
    swap_json: Optional[str],
    daily_df: pd.DataFrame,
    gas_units: float,
    gas_multiplier: float,
    r_quantiles: List[float],
    min_r: float,
    max_r: float,
    out_dir: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Optional[List[float]]:
    """Return candidate R values from observed post-gas surplus proxies.

    This is intentionally conservative in its interpretation. It computes a rough
    observed surplus proxy:
        gross_proxy = amountUSD * abs(log(swap_price / external_price))
        gas_cost    = gasPrice * gas_units * external_price
        post_gas_surplus_proxy = gross_proxy - gas_cost

    This is not a proof of the true reservation profit. It is just a way to choose
    plausible R regimes from the observed trace scale.
    """
    if not swap_json:
        return None
    if not os.path.exists(swap_json):
        print(f"Swap file not found, skipping R calibration: {swap_json}")
        return None

    swaps = load_swaps_json(swap_json)
    swaps = filter_by_date_window(swaps, start, end, "swap_json")
    swaps = estimate_swap_price_y_per_x(swaps, daily_df)
    swaps["amountUSD"] = pd.to_numeric(swaps["amountUSD"], errors="coerce").abs()
    swaps["gas_price_gwei"] = pd.to_numeric(swaps["gasPrice_wei"], errors="coerce") / 1e9
    swaps["gas_cost"] = gas_multiplier * swaps["gasPrice_wei"] * gas_units * swaps["external_price"] / 1e18
    swaps["price_gap_abs_log"] = np.abs(np.log(swaps["swap_price_y_per_x"] / swaps["external_price"]))
    swaps["gross_surplus_proxy"] = swaps["amountUSD"] * swaps["price_gap_abs_log"]
    swaps["post_gas_surplus_proxy"] = swaps["gross_surplus_proxy"] - swaps["gas_cost"]

    valid = swaps.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["amountUSD", "external_price", "swap_price_y_per_x", "gas_cost", "post_gas_surplus_proxy"]
    )
    valid = valid[(valid["amountUSD"] > 0) & (valid["external_price"] > 0) & (valid["swap_price_y_per_x"] > 0)]
    positive = valid[valid["post_gas_surplus_proxy"] > 0].copy()

    os.makedirs(out_dir, exist_ok=True)
    cal_path = os.path.join(out_dir, "swap_r_calibration.csv")
    valid.to_csv(cal_path, index=False)

    if positive.empty:
        print("No positive post-gas surplus proxies found in swap trace; using fixed R values.")
        print(f"Wrote diagnostic calibration file: {cal_path}")
        return None

    qs = positive["post_gas_surplus_proxy"].quantile(r_quantiles).to_dict()
    r_values = []
    for q in r_quantiles:
        val = float(qs[q])
        val = min(max(val, min_r), max_r)
        # Round to readable dollars/quote units.
        if val >= 100:
            val = round(val / 25.0) * 25.0
        elif val >= 10:
            val = round(val / 5.0) * 5.0
        else:
            val = round(val, 2)
        r_values.append(val)

    # Remove duplicates while preserving order.
    deduped = []
    for v in r_values:
        if v not in deduped:
            deduped.append(v)

    print("\nSwap-level R calibration diagnostic")
    print(f"  swap window: {start or 'unbounded'} to {end or 'unbounded'} (end exclusive)")
    print(f"  swaps loaded: {len(swaps)}")
    print(f"  valid swaps: {len(valid)}")
    print(f"  positive post-gas surplus proxies: {len(positive)}")
    print(f"  wrote: {cal_path}")
    print(f"  calibrated R values from quantiles {r_quantiles}: {deduped}")
    print("  Reminder: these are plausible R regimes, not identified private reservation profits.\n")
    return deduped


def cpmm_price(reserve_x: float, reserve_y: float) -> float:
    return reserve_y / reserve_x


def cpmm_value(reserve_x: float, reserve_y: float, price_y_per_x: float) -> float:
    return price_y_per_x * reserve_x + reserve_y


def price_restoring_reserves(k: float, target_price: float) -> Tuple[float, float]:
    x_star = math.sqrt(k / target_price)
    y_star = math.sqrt(k * target_price)
    return x_star, y_star


def cpmm_surplus_to_target(x: float, y: float, target_price: float) -> float:
    k = x * y
    return max(0.0, target_price * x + y - 2.0 * math.sqrt(k * target_price))


def transfer_unconstrained(lam: float, surplus: float) -> float:
    return max(0.0, lam * surplus)


def transfer_strict_cap(lam: float, surplus: float, c: float, r: float) -> float:
    return max(0.0, min(lam * surplus, max(0.0, surplus - c - r)))


def transfer_buffered(lam: float, gamma: float, surplus: float, c: float, r: float, delta: float) -> float:
    cap = max(0.0, (1.0 - gamma) * surplus - c - r - delta)
    return max(0.0, min(lam * surplus, cap))


def apply_method(
    method: str,
    lam: float,
    gamma: float,
    delta: float,
    x: float,
    y: float,
    target_price: float,
    c: float,
    r: float,
    fixed_fee_base: str = "arbitrage_notional",
    observed_daily_volume_usd: float = 0.0,
) -> Dict[str, float]:
    k = x * y
    p0 = cpmm_price(x, y)
    surplus_full = cpmm_surplus_to_target(x, y, target_price)
    baseline_feasible = (surplus_full - c) >= r and surplus_full > EPS

    x_star, y_star = price_restoring_reserves(k, target_price)
    arbitrage_notional = max(abs(x_star - x) * target_price, abs(y_star - y))
    observed_daily_volume_usd = float(observed_daily_volume_usd or 0.0)
    if fixed_fee_base == "observed_daily_volume":
        fixed_fee_notional = observed_daily_volume_usd
    elif fixed_fee_base == "arbitrage_notional":
        fixed_fee_notional = arbitrage_notional
    else:
        raise ValueError("fixed_fee_base must be 'arbitrage_notional' or 'observed_daily_volume'")

    fixed_fee_rate = parse_fixed_fee_rate(method)

    if method == "baseline_amm":
        transfer = 0.0
        executes = baseline_feasible
    elif fixed_fee_rate is not None:
        transfer = max(0.0, fixed_fee_rate * fixed_fee_notional)
        executes = (surplus_full - c - transfer) >= r and surplus_full > EPS
    elif method == "unconstrained":
        transfer = transfer_unconstrained(lam, surplus_full)
        executes = (surplus_full - c - transfer) >= r and surplus_full > EPS
    elif method == "strict_cap":
        transfer = transfer_strict_cap(lam, surplus_full, c, r)
        executes = baseline_feasible
    elif method == "buffered":
        transfer = transfer_buffered(lam, gamma, surplus_full, c, r, delta)
        executes = baseline_feasible
    else:
        raise ValueError(f"unknown method: {method}")

    if executes:
        x1, y1 = x_star, y_star
    else:
        x1, y1 = x, y
        transfer = 0.0

    final_price = cpmm_price(x1, y1)
    price_error = abs(math.log(final_price / target_price)) if target_price > 0 else np.nan
    lp_value = cpmm_value(x1, y1, target_price) + transfer
    hodl_value = cpmm_value(x, y, target_price)
    start_value = cpmm_value(x, y, p0)
    baseline_margin = surplus_full - c - r
    cap_binds = 0.0
    if method in {"strict_cap", "buffered"} and executes:
        cap_transfer = max(0.0, surplus_full - c - r) if method == "strict_cap" else max(0.0, (1.0 - gamma) * surplus_full - c - r - delta)
        cap_binds = float(cap_transfer + 1e-9 < lam * surplus_full)

    return {
        "method": method,
        "lambda": lam,
        "gamma": gamma if method == "buffered" else 0.0,
        "delta": delta if method == "buffered" else 0.0,
        "reservation_profit": r,
        "pool_price_start": p0,
        "target_price": target_price,
        "surplus_full": surplus_full,
        "execution_cost": c,
        "baseline_margin": baseline_margin,
        "arbitrage_notional": arbitrage_notional,
        "observed_daily_volume_usd": observed_daily_volume_usd,
        "fixed_fee_notional": fixed_fee_notional if fixed_fee_rate is not None else 0.0,
        "fixed_fee_rate": fixed_fee_rate if fixed_fee_rate is not None else 0.0,
        "baseline_feasible": float(baseline_feasible),
        "executes": float(executes),
        "transfer": transfer,
        "cap_binds": cap_binds,
        "x_start": x,
        "y_start": y,
        "x_end": x1,
        "y_end": y1,
        "lp_value": lp_value,
        "hodl_value": hodl_value,
        "start_value": start_value,
        "lp_gain_vs_hodl": (lp_value - hodl_value) / max(abs(hodl_value), EPS),
        "lp_gain_vs_start": (lp_value - start_value) / max(abs(start_value), EPS),
        "price_error": price_error,
    }


def compute_execution_cost(row: pd.Series, params: ReplayParams) -> float:
    c = params.fixed_c
    gas_price = row.get("gas_price_gwei", np.nan)
    eth_usd = row.get("eth_usd_price", np.nan)
    if pd.isna(eth_usd):
        eth_usd = row.get("external_price", np.nan)
    if not pd.isna(gas_price) and not pd.isna(eth_usd):
        c += params.gas_multiplier * float(gas_price) * 1e-9 * params.gas_units * float(eth_usd)
    return float(c)


def run_replay(df: pd.DataFrame, params: ReplayParams) -> Tuple[pd.DataFrame, pd.DataFrame]:
    rows: List[Dict[str, float]] = []
    fixed_fee_methods = [build_fixed_fee_method_name(rate) for rate in params.fixed_fee_rates]
    methods = ["baseline_amm"] + fixed_fee_methods + ["unconstrained", "strict_cap", "buffered"]

    for i in range(len(df) - 1):
        cur = df.iloc[i]
        nxt = df.iloc[i + 1]
        x = float(cur["reserve_x"])
        y = float(cur["reserve_y"])
        target_price = float(nxt["external_price"])
        p0 = cpmm_price(x, y)
        if abs(math.log(target_price / p0)) < params.min_abs_log_move:
            continue
        c = compute_execution_cost(cur, params)
        surplus = cpmm_surplus_to_target(x, y, target_price)
        if surplus < params.min_surplus:
            continue

        for r in params.fixed_r_values:
            for lam in params.lambdas:
                for method in methods:
                    out = apply_method(
                        method, lam, params.gamma, params.delta, x, y, target_price, c, float(r),
                        fixed_fee_base=params.fixed_fee_base,
                        observed_daily_volume_usd=float(cur.get("observed_daily_volume_usd", 0.0) or 0.0),
                    )
                    out.update({"date": cur["date"], "next_date": nxt["date"], "row_index": i})
                    rows.append(out)

    events = pd.DataFrame(rows)
    if events.empty:
        raise ValueError("no replay events after filters; consider lowering min_abs_log_move/min_surplus")
    summary = summarize(events)
    return events, summary


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    group_cols = ["reservation_profit", "method", "lambda", "gamma", "delta"]
    baseline = events[events["method"] == "baseline_amm"][["row_index", "lambda", "reservation_profit", "lp_value", "lp_gain_vs_hodl"]].rename(
        columns={"lp_value": "baseline_lp_value", "lp_gain_vs_hodl": "baseline_lp_gain_vs_hodl"}
    )
    merged = events.merge(baseline, on=["row_index", "lambda", "reservation_profit"], how="left")
    merged["improved_vs_baseline"] = (merged["lp_value"] > merged["baseline_lp_value"]).astype(float)
    merged["gain_improvement_vs_baseline"] = merged["lp_gain_vs_hodl"] - merged["baseline_lp_gain_vs_hodl"]

    agg = merged.groupby(group_cols, dropna=False).agg(
        events=("row_index", "count"),
        baseline_feasible_rate=("baseline_feasible", "mean"),
        participation_rate=("executes", "mean"),
        mean_lp_gain_vs_hodl=("lp_gain_vs_hodl", "mean"),
        median_lp_gain_vs_hodl=("lp_gain_vs_hodl", "median"),
        mean_lp_gain_vs_start=("lp_gain_vs_start", "mean"),
        mean_execution_cost=("execution_cost", "mean"),
        mean_arbitrage_notional=("arbitrage_notional", "mean"),
        mean_observed_daily_volume_usd=("observed_daily_volume_usd", "mean"),
        mean_fixed_fee_notional=("fixed_fee_notional", "mean"),
        mean_fixed_fee_rate=("fixed_fee_rate", "mean"),
        mean_transfer=("transfer", "mean"),
        total_transfer=("transfer", "sum"),
        positive_transfer_rate=("transfer", lambda s: float((s > EPS).mean())),
        cap_bind_rate=("cap_binds", "mean"),
        mean_price_error=("price_error", "mean"),
        improved_days_vs_baseline=("improved_vs_baseline", "mean"),
        mean_gain_improvement_vs_baseline=("gain_improvement_vs_baseline", "mean"),
    ).reset_index()

    wealth = []
    for key, g in merged.sort_values("date").groupby(group_cols, dropna=False):
        vals = 1.0 + g["lp_gain_vs_start"].to_numpy(dtype=float)
        vals = np.clip(vals, 1e-12, None)
        wealth.append((*key, float(np.prod(vals))))
    wealth_df = pd.DataFrame(wealth, columns=group_cols + ["cumulative_value_index"])
    agg = agg.merge(wealth_df, on=group_cols, how="left")
    return agg.sort_values(["reservation_profit", "lambda", "method"]).reset_index(drop=True)


def save_outputs(events: pd.DataFrame, summary: pd.DataFrame, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    events_path = os.path.join(out_dir, "lp_outcome_events.csv")
    summary_path = os.path.join(out_dir, "lp_outcome_summary.csv")
    events.to_csv(events_path, index=False)
    summary.to_csv(summary_path, index=False)

    paper = summary.copy()
    pct_cols = [
        "baseline_feasible_rate", "participation_rate", "mean_lp_gain_vs_hodl",
        "median_lp_gain_vs_hodl", "positive_transfer_rate", "cap_bind_rate",
        "improved_days_vs_baseline", "mean_gain_improvement_vs_baseline",
    ]
    for c in pct_cols:
        if c in paper.columns:
            paper[c] = 100.0 * paper[c]
    round_cols = [c for c in paper.columns if c not in {"method"}]
    paper[round_cols] = paper[round_cols].round(4)
    paper.to_csv(os.path.join(out_dir, "lp_outcome_summary_paper_units.csv"), index=False)

    print(f"Wrote {events_path}")
    print(f"Wrote {summary_path}")
    print(f"Wrote {os.path.join(out_dir, 'lp_outcome_summary_paper_units.csv')}")
    print("\nSummary:")
    with pd.option_context("display.max_columns", 60, "display.width", 220):
        print(paper.to_string(index=False))


def build_args_from_constants() -> argparse.Namespace:
    return argparse.Namespace(
        pool_csv=POOL_CSV,
        price_csv=PRICE_CSV,
        gas_csv=GAS_CSV,
        swap_json=SWAP_JSON,
        out_dir=OUT_DIR,
        lambda_values=LAMBDA_VALUES,
        gamma=GAMMA,
        delta=DELTA,
        fixed_c=FIXED_C,
        fixed_r_values=FIXED_R_VALUES,
        fixed_fee_rates=FIXED_FEE_RATES,
        fixed_fee_base=FIXED_FEE_BASE,
        restrict_replay_to_swap_window=RESTRICT_REPLAY_TO_SWAP_WINDOW,
        swap_window_start=SWAP_WINDOW_START,
        swap_window_end=SWAP_WINDOW_END,
        gas_units=GAS_UNITS,
        gas_multiplier=GAS_MULTIPLIER,
        calibrate_r_from_swaps=CALIBRATE_R_FROM_SWAPS,
        r_quantiles=R_QUANTILES,
        min_calibrated_r=MIN_CALIBRATED_R,
        max_calibrated_r=MAX_CALIBRATED_R,
        min_abs_log_move=MIN_ABS_LOG_MOVE,
        min_surplus=MIN_SURPLUS,
    )


def parse_args() -> argparse.Namespace:
    # PyCharm Run button path: no parameters means use the constants above.
    if len(sys.argv) == 1:
        print("No command-line parameters supplied; using constants inside the script.\n")
        return build_args_from_constants()

    p = argparse.ArgumentParser(description="Daily LP-outcome replay for arbitrage-aware surplus sharing")
    p.add_argument("--pool-csv", default=POOL_CSV, help="Daily pool state/depth CSV")
    p.add_argument("--price-csv", default=PRICE_CSV, help="Daily external/reference price CSV")
    p.add_argument("--gas-csv", default=GAS_CSV, help="Optional daily gas price CSV")
    p.add_argument("--swap-json", default=SWAP_JSON, help="Optional Uniswap v4 swaps JSON for R calibration")
    p.add_argument("--out-dir", default=OUT_DIR, help="Output directory")
    p.add_argument("--lambda-values", nargs="+", type=float, default=LAMBDA_VALUES, help="Sharing rates to evaluate")
    p.add_argument("--gamma", type=float, default=GAMMA, help="Retained-surplus parameter")
    p.add_argument("--delta", type=float, default=DELTA, help="Safety buffer in quote-token units")
    p.add_argument("--fixed-c", type=float, default=FIXED_C, help="Fixed execution cost in quote-token units")
    p.add_argument("--fixed-r", dest="fixed_r_values", nargs="+", type=float, default=FIXED_R_VALUES, help="One or more reservation-profit values")
    p.add_argument("--fixed-fee-rates", nargs="+", type=float, default=FIXED_FEE_RATES, help="Fixed swap-fee baselines, e.g. 0.0005 0.0030")
    p.add_argument("--fixed-fee-base", choices=["arbitrage_notional", "observed_daily_volume"], default=FIXED_FEE_BASE, help="Notional base used for fixed-fee baselines")
    p.add_argument("--restrict-replay-to-swap-window", action="store_true", default=RESTRICT_REPLAY_TO_SWAP_WINDOW, help="Restrict daily replay to SWAP_WINDOW_START/END")
    p.add_argument("--no-restrict-replay-to-swap-window", action="store_false", dest="restrict_replay_to_swap_window", help="Use all daily rows even if swap JSON is only a shorter window")
    p.add_argument("--swap-window-start", default=SWAP_WINDOW_START, help="Start date for swap-derived calibration/features")
    p.add_argument("--swap-window-end", default=SWAP_WINDOW_END, help="Exclusive end date for swap-derived calibration/features")
    p.add_argument("--gas-units", type=float, default=GAS_UNITS, help="Assumed gas units for arbitrage transaction")
    p.add_argument("--gas-multiplier", type=float, default=GAS_MULTIPLIER, help="Multiplier for gas component")
    p.add_argument("--calibrate-r-from-swaps", action="store_true", default=CALIBRATE_R_FROM_SWAPS, help="Use swap JSON to calibrate R quantiles")
    p.add_argument("--no-calibrate-r-from-swaps", action="store_false", dest="calibrate_r_from_swaps", help="Disable swap-based R calibration")
    p.add_argument("--r-quantiles", nargs="+", type=float, default=R_QUANTILES, help="Quantiles for swap-based R calibration")
    p.add_argument("--min-calibrated-r", type=float, default=MIN_CALIBRATED_R)
    p.add_argument("--max-calibrated-r", type=float, default=MAX_CALIBRATED_R)
    p.add_argument("--min-abs-log-move", type=float, default=MIN_ABS_LOG_MOVE, help="Filter tiny daily moves by |log(P_next/pool_price)|")
    p.add_argument("--min-surplus", type=float, default=MIN_SURPLUS, help="Filter tiny surplus events")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    pool_csv = resolve_path(args.pool_csv)
    price_csv = resolve_path(args.price_csv)
    gas_csv = resolve_path(args.gas_csv)
    swap_json = resolve_path(args.swap_json)
    out_dir = resolve_path(args.out_dir) or args.out_dir

    print("Input configuration:")
    print(f"  pool_csv: {pool_csv}")
    print(f"  price_csv: {price_csv}")
    print(f"  gas_csv:   {gas_csv}")
    print(f"  swap_json: {swap_json}")
    print(f"  out_dir:   {out_dir}")

    df = load_inputs(pool_csv, price_csv, gas_csv)

    if getattr(args, "restrict_replay_to_swap_window", False):
        df = filter_by_date_window(df, args.swap_window_start, args.swap_window_end, "daily replay inputs")
        print(f"  replay_window: {args.swap_window_start} to {args.swap_window_end} (end exclusive)")

    df = load_swap_daily_features(
        swap_json=swap_json,
        daily_df=df,
        gas_units=float(args.gas_units),
        gas_multiplier=float(args.gas_multiplier),
        start=args.swap_window_start,
        end=args.swap_window_end,
        out_dir=out_dir,
    )

    r_values = list(args.fixed_r_values)
    if args.calibrate_r_from_swaps:
        calibrated = calibrate_r_from_swaps(
            swap_json=swap_json,
            daily_df=df,
            gas_units=float(args.gas_units),
            gas_multiplier=float(args.gas_multiplier),
            r_quantiles=list(args.r_quantiles),
            min_r=float(args.min_calibrated_r),
            max_r=float(args.max_calibrated_r),
            out_dir=out_dir,
            start=args.swap_window_start,
            end=args.swap_window_end,
        )
        if calibrated:
            r_values = calibrated

    params = ReplayParams(
        lambdas=list(args.lambda_values),
        gamma=float(args.gamma),
        delta=float(args.delta),
        fixed_c=float(args.fixed_c),
        fixed_r_values=[float(r) for r in r_values],
        fixed_fee_rates=[float(rate) for rate in args.fixed_fee_rates],
        fixed_fee_base=str(args.fixed_fee_base),
        gas_units=float(args.gas_units),
        gas_multiplier=float(args.gas_multiplier),
        min_abs_log_move=float(args.min_abs_log_move),
        min_surplus=float(args.min_surplus),
    )

    if not all(0 <= lam <= 1 for lam in params.lambdas):
        raise ValueError("all lambda values must be in [0,1]")
    if not (0 <= params.gamma < 1):
        raise ValueError("gamma must be in [0,1)")
    if not all(r >= 0 for r in params.fixed_r_values):
        raise ValueError("all R values must be nonnegative")
    if not all(rate >= 0 for rate in params.fixed_fee_rates):
        raise ValueError("all fixed fee rates must be nonnegative")
    if params.fixed_fee_base not in {"arbitrage_notional", "observed_daily_volume"}:
        raise ValueError("fixed_fee_base must be 'arbitrage_notional' or 'observed_daily_volume'")

    print(f"\nRunning replay with R values: {params.fixed_r_values}")
    print(f"lambda values: {params.lambdas}, gamma={params.gamma}, fixed_c={params.fixed_c}, gas_multiplier={params.gas_multiplier}")
    print(f"fixed fee baselines: {params.fixed_fee_rates} using base={params.fixed_fee_base}\n")

    events, summary = run_replay(df, params)
    save_outputs(events, summary, out_dir)


if __name__ == "__main__":
    main()