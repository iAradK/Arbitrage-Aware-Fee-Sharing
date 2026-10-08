#!/usr/bin/env python3
"""Decode the BigQuery export of Uniswap v4 Swap logs (bigquery/v4_swap_logs.sql) into data/data/bq/swap_logs_decoded.parquet.

Swap(bytes32 indexed id, address indexed sender, int128 amount0, int128 amount1, uint160 sqrtPriceX96,
     uint128 liquidity, int24 tick, uint24 fee): the six non-indexed fields are the six 32-byte words of `data`.
Integers are kept as decimal strings (they exceed int64) plus float copies (*_f). Block timestamps come from the
BigQuery blocks exports in data/data/gas (bigquery/blocks.sql, read by common.data_io.load_blocks). Rows keep the
export's order. Read by experiments/e8_mev_tax_observed.py.

Usage (from the repository root):
    python data/decode_bq_swaps.py data/data/bq/<swap-log export>.csv
    python data/decode_bq_swaps.py <export>.csv --check     # compare with the existing parquet instead of writing it
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.data_io import load_blocks  # noqa: E402
from common.pools import POOLS  # noqa: E402

OUT = ROOT / "data" / "data" / "bq" / "swap_logs_decoded.parquet"
INT_COLS = ["block_number", "log_index", "n_swap_logs", "receipt_gas_used", "receipt_effective_gas_price", "base_fee_per_gas"]
FIELDS = [("amount0", True), ("amount1", True), ("sqrtPriceX96", False), ("liquidity", False), ("tick", True), ("fee", False)]


def word(data: str, i: int, signed: bool) -> int:
    v = int(data[2 + 64 * i: 2 + 64 * (i + 1)], 16)
    return v - (1 << 256) if signed and v >= 1 << 255 else v


def decode(raw: pd.DataFrame) -> pd.DataFrame:
    d = raw.drop(columns="data").copy()
    for c in INT_COLS:
        d[c] = d[c].astype("int64")
    d["max_priority_fee_per_gas"] = d["max_priority_fee_per_gas"].astype(float)   # empty for legacy transactions
    vals = {name: [word(x, i, signed) for x in raw["data"]] for i, (name, signed) in enumerate(FIELDS)}
    for name in ("amount0", "amount1", "sqrtPriceX96", "liquidity"):
        d[name] = [str(v) for v in vals[name]]
    d["tick"] = pd.Series(vals["tick"], dtype="int64")
    d["fee"] = pd.Series(vals["fee"], dtype="int64")
    for name in ("liquidity", "amount0", "amount1"):
        d[f"{name}_f"] = [float(v) for v in vals[name]]
    ts = load_blocks().set_index("block")["timestamp"]
    d["timestamp"] = d["block_number"].map(ts)
    if d["timestamp"].isna().any():
        raise SystemExit(f"{d.timestamp.isna().sum()} swaps have no block timestamp; export the missing blocks first")
    d["month"] = d["timestamp"].dt.strftime("%Y-%m")
    by_id = {p.pool_id: k for k, p in POOLS.items()}
    d["pool"] = d["pool_id"].map(by_id)
    if d["pool"].isna().any():
        raise SystemExit("export contains a pool that is not in common.pools.POOLS")
    d["single_leg"] = d["n_swap_logs"] == 1
    d["tip_gwei"] = (d["receipt_effective_gas_price"] - d["base_fee_per_gas"]) / 1e9
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("csv", help="BigQuery export of bigquery/v4_swap_logs.sql")
    ap.add_argument("--check", action="store_true", help="compare with the existing parquet instead of writing it")
    a = ap.parse_args()
    d = decode(pd.read_csv(a.csv, dtype=str))   # empty fields (to_address of a contract creation, legacy priority fee) -> NA
    if a.check:
        pd.testing.assert_frame_equal(d, pd.read_parquet(OUT), check_exact=True)
        print(f"identical to {OUT.relative_to(ROOT)} ({len(d):,} rows)")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    d.to_parquet(OUT, index=False)
    print(f"{len(d):,} swaps -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
