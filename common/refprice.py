"""Lookahead-free reference prices and trailing offsets.

Convention: a 1-minute bar opened at T is only known at T + 60 s. The reference for a timestamp t is
the last *completed* bar: open = floor_minute(t) - 1 min. A lag of d minutes shifts that on the minute grid.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MIN = pd.Timedelta(minutes=1)


def last_completed_open(t: pd.Series | pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Open time of the last completed 1-minute bar at each timestamp."""
    t = pd.DatetimeIndex(t)
    return t.floor("min") - MIN


def bar_series(kl: pd.DataFrame, kind: str) -> pd.Series:
    """Per-bar reference value indexed by bar open. VWAP = quote_volume/volume (close if no volume)."""
    if kind == "binance_vwap":
        v = (kl["quote_volume"] / kl["volume"]).where(kl["volume"] > 0, kl["close"])
        return v.astype(float)
    return kl["close"].astype(float)


def lookup_bars(series: pd.Series, t, lag_min: int = 0) -> np.ndarray:
    """Value of the last completed bar (shifted back by lag_min minutes) at each t. NaN if missing."""
    opens = last_completed_open(t) - lag_min * MIN
    return series.reindex(opens).to_numpy()


def lookup_lido(events: pd.DataFrame, t, lag_min: int = 0, col: str = "wsteth_per_eth") -> np.ndarray:
    """Forward-filled (never interpolated) Lido rate as of t - lag."""
    ev = events.sort_values("t")
    q = pd.DataFrame({"t": pd.DatetimeIndex(t) - lag_min * MIN})
    q["_i"] = np.arange(len(q))
    q = q.sort_values("t")
    m = pd.merge_asof(q, ev[["t", col]], on="t", direction="backward")
    out = np.empty(len(q))
    out[q["_i"].to_numpy()] = m[col].to_numpy()
    return out


def trailing_offset(ts: pd.Series, block: pd.Series, post_dev: pd.Series, window: pd.Timedelta,
                    min_swaps: int = 20) -> pd.Series:
    """Median signed post-swap log deviation over swaps in [t - window, t), strictly earlier *blocks*.

    Swaps must be sorted by (block, log_index). Because all swaps of a block share one timestamp and
    consecutive blocks are >= 1 s apart, a left-closed time window excludes the current block.
    Returns NaN where fewer than `min_swaps` earlier swaps exist in the window.
    """
    s = pd.Series(post_dev.to_numpy(), index=pd.DatetimeIndex(ts))
    r = s.rolling(window, closed="left")
    med = r.median()
    cnt = r.count()
    out = med.where(cnt >= min_swaps)
    return pd.Series(out.to_numpy(), index=post_dev.index)
