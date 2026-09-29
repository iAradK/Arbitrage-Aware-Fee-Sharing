"""Per-minute lookahead-free market state for one pool: benchmark and lagged hook reference, trailing offset,
gas, numeraire conversions, real pool price and E1 depth. Also candidate construction and calibration helpers
shared by E2 (replay) and E4 (oracle delay).

Timing convention: a grid time t sits on a minute boundary. Every input at t comes from information stamped
strictly before t: the last completed 1-minute bar (open = t - 1 min), the forward-filled Lido rate, swaps and
blocks with timestamp < t, and the E1 depth known strictly before t.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import cpmm, data_io, refprice
from .pools import CACHE, OFFSET_MIN_SWAPS, OFFSET_WINDOWS, POOLS, RESULTS, STUDY_END, STUDY_START, TEST_START, VALID_START

SPLIT_RANGE = {"train": (STUDY_START + pd.Timedelta(hours=2), VALID_START), "valid": (VALID_START, TEST_START),
               "test": (TEST_START, STUDY_END)}
VARIANTS = {"raw": None, "corr6h": "6h", "corr24h": "24h", "corr7d": "7d"}
_bars_cache: dict = {}
_lido_cache: dict = {}


def _bars():
    if not _bars_cache:
        for s, k in [("ETHUSDC", "close"), ("ETHUSDT", "close"), ("ETHBTC", "close"), ("BTCUSDT", "close"), ("USDCUSDT", "binance_vwap")]:
            _bars_cache[s] = refprice.bar_series(data_io.load_klines(s), k)
        _lido_cache["ev"] = data_io.load_lido()
    return _bars_cache, _lido_cache["ev"]


def asof_before(times: pd.DatetimeIndex, values: np.ndarray, t) -> np.ndarray:
    """Last value stamped strictly before each t (NaN if none)."""
    idx = times.searchsorted(pd.DatetimeIndex(t), side="left") - 1
    return np.where(idx >= 0, values[np.clip(idx, 0, None)], np.nan)


def load_aligned(pool_key: str) -> pd.DataFrame:
    return pd.read_parquet(CACHE / "aligned" / f"{pool_key}.parquet")


def grid_times(split: str) -> pd.DatetimeIndex:
    a, b = SPLIT_RANGE[split]
    return pd.date_range(a, b - pd.Timedelta(minutes=1), freq="min").as_unit("ns")


def trailing_offset_at(al: pd.DataFrame, window: str | None, t) -> np.ndarray:
    """Median signed post-swap deviation over swaps in the trailing window, from swaps stamped strictly before t."""
    if window is None:
        return np.zeros(len(t))
    ts = pd.DatetimeIndex(al["timestamp"])
    s = pd.Series(al["post_dev"].to_numpy(), index=ts).rolling(OFFSET_WINDOWS[window], min_periods=OFFSET_MIN_SWAPS).median().dropna()
    return np.nan_to_num(asof_before(s.index, s.to_numpy(), t), nan=0.0)      # undefined -> raw (DECISIONS D12)


def build_grid(pool_key: str, split: str, variant: str = "raw", lags=(0, 1, 5), with_depth: bool = True) -> pd.DataFrame:
    pool = POOLS[pool_key]
    bars, lido = _bars()
    t = grid_times(split)
    al = load_aligned(pool_key)
    g = pd.DataFrame({"t": t})
    off = trailing_offset_at(al, VARIANTS[variant], t)
    g["offset"] = off
    for d in lags:
        raw = (refprice.lookup_lido(lido, t, d, "wsteth_per_eth") if pool.ref_kind == "lido"
               else refprice.lookup_bars(bars[pool.ref_symbol], t, d))
        g[f"p_ref_{d}"] = raw * np.exp(off)
    # real pool price: post-swap price of the last swap before t
    sw = pd.DatetimeIndex(al["timestamp"])
    g["p_real"] = asof_before(sw, al["price"].to_numpy(), t)
    # gas at the last block before t
    bg = pd.read_parquet(CACHE / "block_gas.parquet", columns=["timestamp", "gas_price_wei", "gas_source"])
    bts = pd.DatetimeIndex(bg["timestamp"])
    g["gas_wei"] = asof_before(bts, bg["gas_price_wei"].to_numpy(), t)
    g["gas_real"] = asof_before(bts, (bg["gas_source"] == "fee_history").to_numpy().astype(float), t) > 0.5
    # numeraire and USD conversions
    eth_usd = refprice.lookup_bars(bars["ETHUSDC"], t)
    g["eth_usd"] = eth_usd
    if pool_key == "eth_usdc_005":
        g["eth_in_num"], g["usd_per_num"] = eth_usd, 1.0
    elif pool_key == "usdc_usdt_0001":
        g["eth_in_num"], g["usd_per_num"] = refprice.lookup_bars(bars["ETHUSDT"], t), 1.0
    elif pool_key == "eth_wbtc_030":
        g["eth_in_num"], g["usd_per_num"] = refprice.lookup_bars(bars["ETHBTC"], t), refprice.lookup_bars(bars["BTCUSDT"], t)
    else:
        g["eth_in_num"] = refprice.lookup_lido(lido, t, 0, "wsteth_per_eth")
        g["usd_per_num"] = eth_usd * refprice.lookup_lido(lido, t, 0, "eth_per_wsteth")
    if with_depth:
        dep = pd.read_parquet(RESULTS / "e1" / f"depth_{pool_key}.parquet")
        g["L"] = dep.set_index("timestamp")["L_trailing"].reindex(t).to_numpy()
    return g


# ---------------------------------------------------------------- costs, regimes, candidates
def gas_cost_num(gas_wei, eth_in_num, gas_units):
    """C = gas_units * gas price * ETH price, in numeraire units."""
    return gas_units * np.asarray(gas_wei) * 1e-18 * np.asarray(eth_in_num)


def swap_surplus_proxy(pool_key: str, variant: str, gas_units: float, split: str = "train") -> pd.DataFrame:
    """Per real swap: gross surplus S of the swap's own balance delta valued at the reference (numeraire and USD),
    post-gas surplus, and whether it moved the price toward the reference."""
    al = load_aligned(pool_key)
    al = al[al["split"] == split]
    w = VARIANTS[variant]
    off = np.nan_to_num(al[f"offset_{w}"].to_numpy(), nan=0.0) if w else 0.0
    pi = al["p_ref"].to_numpy() * np.exp(off)
    S = -(pi * al["amount0"].to_numpy() + al["amount1"].to_numpy())
    C = gas_cost_num(al["gas_price_wei"].to_numpy(), al["eth_in_num"].to_numpy(), gas_units)
    pre, post = np.log(al["price_pre"].to_numpy() / pi), np.log(al["price"].to_numpy() / pi)
    out = pd.DataFrame({"S": S, "C": C, "toward": np.abs(post) < np.abs(pre), "usd_per_num": al["usd_per_num"].to_numpy()})
    out["post_gas_usd"] = (out["S"] - out["C"]) * out["usd_per_num"]
    return out


def r_regimes_usd(pool_key: str, variant: str, gas_units: float, quantiles) -> dict:
    """Low/median/high reservation payoff (USD): quantiles of the positive post-gas surplus proxy on train swaps that
    moved the price toward the reference (DECISIONS D6)."""
    p = swap_surplus_proxy(pool_key, variant, gas_units, "train")
    x = p.loc[p["toward"] & (p["post_gas_usd"] > 0), "post_gas_usd"]
    return {name: float(x.quantile(q)) for name, q in zip(["low", "median", "high"], quantiles)}


def real_state_candidates(g: pd.DataFrame, pool_key: str, gas_units: float, R_usd: float, d: int) -> pd.DataFrame:
    """Full price-correcting candidate against the benchmark from the real pool state at each minute.
    True surplus values it at the benchmark bar (lag 0); the estimate values the same flows at the bar lagged by d."""
    pool = POOLS[pool_key]
    ok = g["L"].notna() & g["p_real"].notna() & g["p_ref_0"].notna() & g[f"p_ref_{d}"].notna()
    x = (g["L"] / np.sqrt(g["p_real"])).to_numpy()
    y = (g["L"] * np.sqrt(g["p_real"])).to_numpy()
    pib = g["p_ref_0"].to_numpy()
    dirn, n0 = cpmm.full_correction_net(x, y, pool.fee, pib)
    S, tr = cpmm.surplus_of(x, y, pool.fee, dirn, n0, pib)
    S_hat = np.maximum(g[f"p_ref_{d}"].to_numpy() * tr["d0"] + tr["d1"], 0.0)
    C = gas_cost_num(g["gas_wei"].to_numpy(), g["eth_in_num"].to_numpy(), gas_units)
    usd = g["usd_per_num"].to_numpy()
    R = R_usd / usd
    out = pd.DataFrame({"t": g["t"], "S": S, "S_hat": S_hat, "C": C, "R": R, "usd": usd, "notional": tr["notional"],
                        "n0": n0, "valid": ok.to_numpy() & (n0 > 0)})
    out["baseline_feasible"] = out["valid"] & ((out["S"] - out["C"]) >= out["R"])
    return out


def eps_S_usd(c: pd.DataFrame, q: float = 0.95) -> float:
    """eps_S(d) = P-quantile of |S_hat - S| (USD) over baseline-feasible candidates."""
    m = c["baseline_feasible"]
    return float(((c["S_hat"] - c["S"]).abs() * c["usd"])[m].quantile(q)) if m.any() else 0.0
