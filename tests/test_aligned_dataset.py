"""Checks on the built aligned datasets (skipped when cache/aligned is absent)."""
import numpy as np
import pandas as pd
import pytest

from common import data_io, refprice
from common.pools import CACHE, POOLS, TEST_START, VALID_START

FILES = {k: CACHE / "aligned" / f"{k}.parquet" for k in POOLS}
pytestmark = pytest.mark.skipif(not all(f.exists() for f in FILES.values()), reason="aligned datasets not built")


@pytest.mark.parametrize("key", list(POOLS))
def test_integrity_and_splits(key):
    df = pd.read_parquet(FILES[key])
    assert not df.duplicated(["tx_hash", "log_index"]).any()
    assert df["timestamp"].is_monotonic_increasing
    assert df["p_ref"].notna().all() and df["gas_price_wei"].notna().all()
    assert (df.loc[df["timestamp"] < VALID_START, "split"] == "train").all()
    assert (df.loc[(df["timestamp"] >= VALID_START) & (df["timestamp"] < TEST_START), "split"] == "valid").all()
    assert (df.loc[df["timestamp"] >= TEST_START, "split"] == "test").all()
    assert df["block_ts_match"].all()
    # orientation: median |log deviation| small (a wrongly oriented or mis-scaled pool would be far off)
    assert df["post_dev"].abs().median() < 0.005


def test_reference_recomputed_from_klines_never_uses_containing_bar():
    df = pd.read_parquet(FILES["eth_usdc_005"]).sample(200, random_state=0)
    s = refprice.bar_series(data_io.load_klines("ETHUSDC"), "binance_close")
    for _, r in df.iterrows():
        bar_open = r["timestamp"].floor("min") - pd.Timedelta(minutes=1)
        assert r["p_ref"] == s[bar_open]
        for d in (1, 5, 60):
            assert r[f"p_ref_lag_{d}"] == s[bar_open - pd.Timedelta(minutes=d)]


@pytest.mark.parametrize("key", ["eth_wbtc_030", "eth_wsteth_001"])
def test_trailing_offset_recomputed_on_a_prefix_matches(key):
    """The offset stored for a swap must equal the offset computed from the data up to that swap only."""
    df = pd.read_parquet(FILES[key])
    k = len(df) // 2
    pre = df.iloc[:k]
    again = refprice.trailing_offset(pre["timestamp"], pre["block"], pre["post_dev"], pd.Timedelta(hours=24), 20)
    a, b = again.to_numpy(), pre["offset_24h"].to_numpy()
    assert np.allclose(a, b, equal_nan=True)
