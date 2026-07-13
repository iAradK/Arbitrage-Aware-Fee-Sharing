from pathlib import Path
import pandas as pd
import numpy as np


# ============================================================
# Paths
# ============================================================

ROOT = Path(__file__).resolve().parent / "./pyth_eth_usd_tradingview_2026_01_01_to_2026_04_01_1min"

PYTH_CSV = (
    ROOT
    / "pyth_eth_usd_2026_01_01_to_2026_04_01_1min.csv"
)

BINANCE_CSVS = [
    ROOT / "ETHUSDT-1m-2026-01.csv",
    ROOT / "ETHUSDT-1m-2026-02.csv",
    ROOT / "ETHUSDT-1m-2026-03.csv",
]

OUT_DIR = ROOT / "external_price_data"
OUT_DIR.mkdir(exist_ok=True)

OUT_CSV = OUT_DIR / "eth_usd_pyth_oracle_vs_binance_actual_2026_01_to_04.csv"
OUT_SUMMARY = OUT_DIR / "oracle_error_summary.csv"


# ============================================================
# Binance loader
# ============================================================

BINANCE_COLS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "num_trades",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
]


def load_binance_csv(path: Path) -> pd.DataFrame:
    print(f"Reading Binance file: {path}", flush=True)

    df = pd.read_csv(path, header=None, names=BINANCE_COLS)

    # Binance public data may use milliseconds or microseconds.
    max_ts = int(df["open_time"].max())
    if max_ts > 10**15:
        unit = "us"
    else:
        unit = "ms"

    df["datetime"] = pd.to_datetime(df["open_time"], unit=unit, utc=True)

    numeric_cols = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_asset_volume",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
    ]

    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["num_trades"] = pd.to_numeric(df["num_trades"], errors="coerce")

    df = df.rename(columns={
        "close": "binance_eth_usd_price",
        "open": "binance_open",
        "high": "binance_high",
        "low": "binance_low",
        "volume": "binance_volume",
    })

    return df[
        [
            "datetime",
            "open_time",
            "binance_open",
            "binance_high",
            "binance_low",
            "binance_eth_usd_price",
            "binance_volume",
            "quote_asset_volume",
            "num_trades",
        ]
    ]


# ============================================================
# Main
# ============================================================

def main():
    print("Loading Pyth data...", flush=True)
    pyth = pd.read_csv(PYTH_CSV)

    pyth["datetime"] = pd.to_datetime(pyth["datetime"], utc=True)

    # Keep only the fields needed for the experiment.
    pyth = pyth[
        [
            "datetime",
            "timestamp",
            "pyth_open",
            "pyth_high",
            "pyth_low",
            "pyth_close",
        ]
    ].copy()

    pyth = pyth.rename(columns={
        "pyth_close": "pyth_oracle_price"
    })

    print(f"Pyth rows: {len(pyth)}", flush=True)
    print(f"Pyth range: {pyth['datetime'].min()} to {pyth['datetime'].max()}", flush=True)

    print("Loading Binance data...", flush=True)
    binance_frames = [load_binance_csv(path) for path in BINANCE_CSVS]
    binance = pd.concat(binance_frames, ignore_index=True)

    binance = binance.sort_values("datetime")
    binance = binance.drop_duplicates(subset=["datetime"], keep="last")

    start = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")
    end = pd.Timestamp("2026-04-01 00:00:00", tz="UTC")

    binance = binance[(binance["datetime"] >= start) & (binance["datetime"] < end)]

    print(f"Binance rows: {len(binance)}", flush=True)
    print(f"Binance range: {binance['datetime'].min()} to {binance['datetime'].max()}", flush=True)

    print("Merging Pyth and Binance by minute...", flush=True)

    df = pd.merge(
        pyth,
        binance,
        on="datetime",
        how="inner",
    )

    df = df.sort_values("datetime")
    df = df.drop_duplicates(subset=["datetime"], keep="last")

    # Oracle error relative to Binance actual market price.
    df["oracle_error_abs"] = (
        df["pyth_oracle_price"] - df["binance_eth_usd_price"]
    ).abs()

    df["oracle_error_signed"] = (
        df["pyth_oracle_price"] - df["binance_eth_usd_price"]
    )

    df["oracle_error_bps"] = (
        10000.0 * df["oracle_error_abs"] / df["binance_eth_usd_price"]
    )

    df["oracle_error_signed_bps"] = (
        10000.0 * df["oracle_error_signed"] / df["binance_eth_usd_price"]
    )

    # Stale oracle prices.
    for delay in [1, 5, 10, 30, 60]:
        df[f"pyth_oracle_delay_{delay}m"] = df["pyth_oracle_price"].shift(delay)

        df[f"oracle_error_delay_{delay}m_abs"] = (
            df[f"pyth_oracle_delay_{delay}m"] - df["binance_eth_usd_price"]
        ).abs()

        df[f"oracle_error_delay_{delay}m_bps"] = (
            10000.0
            * df[f"oracle_error_delay_{delay}m_abs"]
            / df["binance_eth_usd_price"]
        )

    # Drop first 60 rows only if you want all delayed columns fully populated.
    # For now we keep them, with NaN in delayed columns.
    df.to_csv(OUT_CSV, index=False)

    print(f"Saved merged external price file: {OUT_CSV}", flush=True)
    print(f"Merged rows: {len(df)}", flush=True)
    print(f"Range: {df['datetime'].min()} to {df['datetime'].max()}", flush=True)

    # Summary table.
    summary_rows = []

    for label, col in [
        ("delta_0_current_pyth", "oracle_error_bps"),
        ("delay_1m", "oracle_error_delay_1m_bps"),
        ("delay_5m", "oracle_error_delay_5m_bps"),
        ("delay_10m", "oracle_error_delay_10m_bps"),
        ("delay_30m", "oracle_error_delay_30m_bps"),
        ("delay_60m", "oracle_error_delay_60m_bps"),
    ]:
        s = df[col].dropna()

        summary_rows.append({
            "series": label,
            "count": len(s),
            "mean_bps": s.mean(),
            "median_bps": s.median(),
            "p90_bps": s.quantile(0.90),
            "p95_bps": s.quantile(0.95),
            "p99_bps": s.quantile(0.99),
            "max_bps": s.max(),
        })

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(OUT_SUMMARY, index=False)

    print(f"Saved oracle error summary: {OUT_SUMMARY}", flush=True)
    print(summary.to_string(index=False), flush=True)

    # Quick sanity checks.
    expected_rows = 90 * 24 * 60
    print("", flush=True)
    print("Sanity checks:", flush=True)
    print(f"Expected 90-day 1-min rows: {expected_rows}", flush=True)
    print(f"Actual merged rows: {len(df)}", flush=True)
    print(f"Missing rows vs expected: {expected_rows - len(df)}", flush=True)

    if len(df) > 0:
        print("", flush=True)
        print("First rows:", flush=True)
        print(df.head().to_string(index=False), flush=True)

        print("", flush=True)
        print("Last rows:", flush=True)
        print(df.tail().to_string(index=False), flush=True)


if __name__ == "__main__":
    main()