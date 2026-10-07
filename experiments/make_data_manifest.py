"""Regenerate results/data_manifest.json: the inventory (SHA-256, bytes) of every raw input the pipeline reads, the study
window and the splits from common/pools.py, and the known gaps. Experiment manifests copy the hashes from this file.

  python experiments/make_data_manifest.py
"""
from __future__ import annotations

import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.data_io import sha256  # noqa: E402
from common.pools import EXCLUDED_POOLS, POOLS, RESULTS, STUDY_END, STUDY_START, TEST_START, VALID_START  # noqa: E402


def months(a, b):
    """Whole or partial months of [a, b) as YYYY-MM, first and last."""
    r = pd.period_range(a.tz_convert(None), (b - pd.Timedelta(seconds=1)).tz_convert(None), freq="M")
    return [str(r[0]), str(r[-1])]


def main():
    rel = lambda p: str(Path(p).relative_to(ROOT)).replace("\\", "/")
    gas = sorted(glob.glob(str(ROOT / "data" / "data" / "gas" / "bq-results-*.csv")))
    tips = sorted(glob.glob(str(ROOT / "data" / "data" / "gas" / "fee_history*.csv")))
    logical = {
        "swaps": {k: rel(p.swaps_path) for k, p in POOLS.items()},
        "binance_1m": {s: f"data/data/binance/{s}_1m.csv.gz" for s in ["ETHUSDC", "ETHBTC", "USDCUSDT", "ETHUSDT", "BTCUSDT"]},
        "lido_wsteth_rate": "data/data/lido/wsteth_rate_events.csv",
        "fee_history_tips": [rel(p) for p in tips],
        "blocks_bigquery": [rel(p) for p in gas],
        "pyth_onchain": "data/data/pyth/onchain_ETH_USD_events.csv.gz",
    }
    files = []
    for v in logical.values():
        if isinstance(v, dict):
            files += list(v.values())
        elif isinstance(v, list):
            files += v
        else:
            files.append(v)
    m = {
        "note": "Inventory of the raw inputs read by the pipeline (experiments/make_data_manifest.py). Experiment manifests copy "
                "these hashes. Window and splits come from common/pools.py.",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "study_window_utc": {"start": STUDY_START.isoformat(), "end_exclusive": STUDY_END.isoformat()},
        "splits": {"train": months(STUDY_START, VALID_START), "valid": months(VALID_START, TEST_START),
                   "test": months(TEST_START, STUDY_END) + [f"test ends {STUDY_END.isoformat()} (exclusive)"]},
        "pools": {k: {"file": rel(p.swaps_path), "reference": f"{p.ref_kind}:{p.ref_symbol}", "fee": p.fee} for k, p in POOLS.items()},
        "excluded_pools": {k: {"file": rel(p.swaps_path), "reason": "DECISIONS W8/H2; not extended to 2025-07"} for k, p in EXCLUDED_POOLS.items()},
        "logical_names": logical,
        "known_gaps": {
            "swap_gas_used": "tx_gasUsed == 0 in every subgraph swap row; gas units are a configured parameter (E5/E7). Receipt gas "
                             "of every swap transaction is in data/data/bq/swap_logs_decoded.parquet (NOTES/2026-10-01_bigquery_crosscheck.md).",
            "fee_history_tips": "real tips for every block of the window (fee_history_2025_07_08.csv + fee_history_head.csv, 2025-07-01 to "
                                "2026-07-31); blocks from 2026-08-01 have no tips and lie outside the window.",
            "pyth_onchain": "complete over the window (monthly counts equal the BigQuery logs); events end 2026-08-17.",
            "protocol_fee": "Uniswap protocol fee on from 2026-07-27 09:15 UTC (swap fee 1.25x LP fee); the window ends 2026-07-27 00:00 UTC.",
            "binance_extent": "consolidated Binance files run 2025-07-01 to 2026-09-22; clipped to the window by load_klines.",
        },
        "excluded_from_pipeline": {
            "data/data/gas/BQ_gas_values.csv": "partial earlier export (2025-09-01..2025-09-17), superseded by blocks_bigquery",
            "data/data/binance/raw/**": "raw daily/monthly zips; consolidated *_1m.csv.gz files are used instead",
            "data/swaps_data/*/parts/**": "resumable download parts; combined swaps_<pool>.csv files are used",
            "data/data/bq/**": "BigQuery cross-check exports (validation only, not read by the experiments)",
            "archive/data_superseded/**": "previous versions of raw files",
        },
        "files": {f: {"bytes": (ROOT / f).stat().st_size, "sha256": sha256(ROOT / f)} for f in files},
    }
    out = RESULTS / "data_manifest.json"
    out.write_text(json.dumps(m, indent=1))
    print(f"wrote {out} ({len(files)} files)")


if __name__ == "__main__":
    main()
