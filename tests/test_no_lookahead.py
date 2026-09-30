"""A value at time t must not change when data at or after t changes (prefix invariance)."""
import numpy as np
import pandas as pd

from common import refprice


def _bars(n=600, seed=0):
    idx = pd.date_range("2026-01-01", periods=n, freq="min", tz="UTC").as_unit("ns")
    return pd.Series(np.random.default_rng(seed).uniform(1, 2, n), index=idx)


def test_reference_never_uses_containing_bar():
    s = _bars()
    t = pd.DatetimeIndex(["2026-01-01 05:10:37", "2026-01-01 05:11:00", "2026-01-01 07:59:59"]).tz_localize("UTC").as_unit("ns")
    base = refprice.lookup_bars(s, t, 0)
    for ti, v in zip(t, base):
        assert v == s[ti.floor("min") - pd.Timedelta(minutes=1)]
    s2 = s.copy()
    s2[s2.index >= t[0].floor("min")] = 99.0     # corrupt the containing bar and every later bar
    assert refprice.lookup_bars(s2, t[:1], 0)[0] == base[0]
    for d in (1, 5, 30):
        assert refprice.lookup_bars(s, t, d)[0] == s[t[0].floor("min") - pd.Timedelta(minutes=1 + d)]


def test_lido_forward_fill_only():
    ev = pd.DataFrame({"t": pd.to_datetime(["2026-01-01 12:00", "2026-01-02 12:00"], utc=True).as_unit("ns"),
                       "wsteth_per_eth": [0.80, 0.90]})
    t = pd.DatetimeIndex(["2026-01-01 11:59", "2026-01-01 12:00", "2026-01-02 11:59"]).tz_localize("UTC").as_unit("ns")
    out = refprice.lookup_lido(ev, t)
    assert np.isnan(out[0]) and out[1] == 0.80 and out[2] == 0.80


def _series(n=400):
    rng = np.random.default_rng(1)
    ts = pd.Series(pd.Timestamp("2026-01-01", tz="UTC").as_unit("ns") + pd.to_timedelta(np.repeat(np.arange(n // 2) * 12, 2), unit="s"))
    blk = pd.Series(np.repeat(np.arange(n // 2), 2))
    return ts, blk, pd.Series(rng.normal(0, 1e-3, n))


def test_trailing_offset_prefix_invariant():
    ts, blk, dev = _series()
    W = pd.Timedelta(hours=1)
    full = refprice.trailing_offset(ts, blk, dev, W, 20)
    k = 300
    trunc = refprice.trailing_offset(ts[:k], blk[:k], dev[:k], W, 20)
    assert np.allclose(full[:k].to_numpy(), trunc.to_numpy(), equal_nan=True)
    dev2 = dev.copy()
    dev2[k:] = 5.0
    again = refprice.trailing_offset(ts, blk, dev2, W, 20)
    assert np.allclose(full[:k].to_numpy(), again[:k].to_numpy(), equal_nan=True)


def test_trailing_offset_min_swaps_and_same_block():
    ts, blk, dev = _series()
    W = pd.Timedelta(hours=1)
    full = refprice.trailing_offset(ts, blk, dev, W, 20)
    assert full[:20].isna().all()                      # fewer than 20 strictly earlier swaps
    # the two swaps of one block see the same earlier-block window, so they get the same offset
    i = 100
    assert blk[i] == blk[i + 1] and full[i] == full[i + 1]


def test_e1_depth_at_is_strictly_earlier():
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "experiments"))
    from e1_cpmm_calibration import depth_at
    idx = pd.DatetimeIndex(["2026-01-01 00:00:12", "2026-01-01 00:00:24"]).tz_localize("UTC")
    s = pd.Series([1.0, 2.0], index=idx)
    t = pd.DatetimeIndex(["2026-01-01 00:00:12", "2026-01-01 00:00:13", "2026-01-01 00:00:24", "2026-01-01 00:00:11"]).tz_localize("UTC")
    out = depth_at(s, t)
    assert np.isnan(out[0]) and out[1] == 1.0 and out[2] == 1.0 and np.isnan(out[3])
