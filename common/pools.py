"""Pool metadata, study window and train/valid/test splits (single source of truth)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
CACHE = REPO / "cache"
RESULTS = REPO / "results"

STUDY_START = pd.Timestamp("2025-07-01 00:00:00", tz="UTC")
# exclusive. Ends before the Uniswap protocol fee was switched on (2026-07-27 09:15 UTC), which raised the swap fee
# paid by the arbitrager to 1.25x the LP fee (1.17x in ETH/WBTC) (NOTES/2026-10-01_bigquery_crosscheck.md, finding 3).
STUDY_END = pd.Timestamp("2026-07-27 00:00:00", tz="UTC")
VALID_START = pd.Timestamp("2026-04-01", tz="UTC")   # DECISIONS H3: validation April-May 2026
TEST_START = pd.Timestamp("2026-06-01", tz="UTC")    # DECISIONS H3: test 1 June - 26 July 2026 (56 days)

REF_LAGS_MIN = (0, 1, 5, 10, 30, 60)
OFFSET_WINDOWS = {"6h": pd.Timedelta(hours=6), "24h": pd.Timedelta(hours=24), "7d": pd.Timedelta(days=7)}
OFFSET_MIN_SWAPS = 20


@dataclass(frozen=True)
class Pool:
    key: str
    pool_id: str
    dec0: int
    dec1: int
    fee: float                 # LP fee as a fraction
    token0: str
    token1: str
    ref_kind: str              # "binance_close" | "binance_vwap" | "lido"
    ref_symbol: str            # binance symbol, or "lido"
    ref_invert: bool           # reference is quoted inverse to pool price (token1 per token0)
    gap_multiple: float        # opportunity threshold in units of the fee (2 for USDC/USDT)
    usd_kind: str              # how to convert a token1 amount to USD

    @property
    def swaps_path(self) -> Path:
        return DATA / "swaps_data" / f"swaps_{self.key}.csv"


POOLS: dict[str, Pool] = {
    "eth_usdc_005": Pool("eth_usdc_005", "0x21c67e77068de97969ba93d4aab21826d33ca12bb9f565d8496e8fda8a82ca27",
                         18, 6, 5e-4, "ETH", "USDC", "binance_close", "ETHUSDC", False, 1.0, "usd_par"),
    "eth_wbtc_030": Pool("eth_wbtc_030", "0x54c72c46df32f2cc455e84e41e191b26ed73a29452cdd3d82f511097af9f427e",
                         18, 8, 3e-3, "ETH", "WBTC", "binance_close", "ETHBTC", False, 1.0, "btcusdt"),
    "eth_wsteth_001": Pool("eth_wsteth_001", "0x1d5b2949ece8754c2d736991c62c5162bd144f497b2212182401b9bae77e2d76",
                           18, 18, 1e-4, "ETH", "wstETH", "lido", "lido", False, 1.0, "wsteth"),
}

# Collected but excluded from the evaluation (fee tier 0.001%, not the 0.01% of the model, and the one-minute benchmark
# resolves about one basis point = ten times its fee; DECISIONS W8). Not extended to July 2025 (DECISIONS H2).
EXCLUDED_POOLS: dict[str, Pool] = {
    "usdc_usdt_0001": Pool("usdc_usdt_0001", "0x8aa4e11cbdf30eedc92100f4c8a31ff748e201d44712cc8c90d189edaa8e4e47",
                           6, 6, 1e-4, "USDC", "USDT", "binance_vwap", "USDCUSDT", False, 2.0, "usd_par"),
}


def split_of(ts: pd.Series) -> pd.Series:
    """Label timestamps (UTC) train / valid / test."""
    out = pd.Series("train", index=ts.index, dtype="object")
    out[ts >= VALID_START] = "valid"
    out[ts >= TEST_START] = "test"
    return out
