#!/usr/bin/env python3
"""
Download 1-minute klines from the Binance public data dump (data.binance.vision).

Fetches monthly zips for complete months and daily zips for the current
(incomplete) month, verifies SHA256 checksums, merges everything into one
UTC-indexed series per symbol, and reports gaps.

Usage:
    pip install requests pandas pyarrow
    python download_binance_klines.py
    python download_binance_klines.py --start 2025-09-01 --end 2026-09-22 \
        --symbols ETHUSDT BTCUSDT USDCUSDT --out data/binance
"""
import argparse
import hashlib
import io
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

BASE = "https://data.binance.vision/data/spot"
COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]

session = requests.Session()


def http_get(url, retries=4):
    """GET with retries. Returns bytes, or None on 404."""
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    return None


def month_starts(start: date, end: date):
    """Yield first-of-month dates from start's month to end's month."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield date(y, m, 1)
        m += 1
        if m == 13:
            y, m = y + 1, 1


def plan_jobs(symbols, start: date, end: date):
    """Return list of (symbol, kind, label, url) covering [start, end]."""
    jobs = []
    this_month = date(end.year, end.month, 1)
    for sym in symbols:
        for ms in month_starts(start, end):
            if ms < this_month:
                label = ms.strftime("%Y-%m")
                url = f"{BASE}/monthly/klines/{sym}/1m/{sym}-1m-{label}.zip"
                jobs.append((sym, "monthly", label, url))
            else:
                # current month: daily files up to `end`
                d = max(ms, start)
                while d <= end:
                    label = d.strftime("%Y-%m-%d")
                    url = f"{BASE}/daily/klines/{sym}/1m/{sym}-1m-{label}.zip"
                    jobs.append((sym, "daily", label, url))
                    d += timedelta(days=1)
    return jobs


def fetch_one(job, raw_dir: Path):
    sym, kind, label, url = job
    dest = raw_dir / sym / f"{sym}-1m-{label}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return job, dest, "cached"

    data = http_get(url)
    if data is None:
        return job, None, "missing (404)"

    # Verify checksum if available
    chk = http_get(url + ".CHECKSUM")
    if chk:
        expected = chk.decode().split()[0].strip().lower()
        actual = hashlib.sha256(data).hexdigest()
        if expected != actual:
            return job, None, "CHECKSUM MISMATCH"

    dest.write_bytes(data)
    return job, dest, "ok"


def parse_zip(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as f:
            df = pd.read_csv(io.BytesIO(f.read()), header=None, names=COLS)
    # Drop a header row if one is present
    df = df[pd.to_numeric(df["open_time"], errors="coerce").notna()].copy()
    df["open_time"] = df["open_time"].astype("int64")
    return df


def to_datetime_utc(ts: pd.Series) -> pd.DatetimeIndex:
    """Binance spot files switched from ms to µs timestamps in 2025. Handle both."""
    ts = ts.astype("int64")
    unit_is_us = ts > 10**14  # ms ~1.7e12, µs ~1.7e15
    out = pd.Series(pd.NaT, index=ts.index, dtype="datetime64[ns, UTC]")
    out[~unit_is_us] = pd.to_datetime(ts[~unit_is_us], unit="ms", utc=True)
    out[unit_is_us] = pd.to_datetime(ts[unit_is_us], unit="us", utc=True)
    return pd.DatetimeIndex(out)


def build_series(sym: str, files, start: date, end: date) -> pd.DataFrame:
    frames = [parse_zip(p) for p in sorted(files)]
    df = pd.concat(frames, ignore_index=True)
    df.index = to_datetime_utc(df["open_time"])
    df.index.name = "timestamp"
    df = df.drop(columns=["open_time", "close_time", "ignore"])
    for c in df.columns:
        df[c] = pd.to_numeric(df[c])
    df = df[~df.index.duplicated(keep="first")].sort_index()
    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(minutes=1)
    return df.loc[lo:hi]


def report_gaps(sym: str, df: pd.DataFrame):
    full = pd.date_range(df.index.min(), df.index.max(), freq="1min", tz="UTC")
    missing = full.difference(df.index)
    pct = 100 * len(missing) / len(full)
    print(f"  {sym}: {len(df):,} rows, {df.index.min()} -> {df.index.max()}, "
          f"{len(missing):,} missing minutes ({pct:.3f}%)")
    if len(missing):
        # Collapse into contiguous runs
        s = pd.Series(missing)
        runs = (s.diff() != pd.Timedelta(minutes=1)).cumsum()
        grouped = s.groupby(runs).agg(["first", "last", "count"])
        top = grouped.sort_values("count", ascending=False).head(5)
        print("    largest gaps:")
        for _, r in top.iterrows():
            print(f"      {r['first']} -> {r['last']}  ({r['count']} min)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["ETHUSDT", "BTCUSDT", "USDCUSDT"])
    ap.add_argument("--start", default="2025-09-01")
    ap.add_argument("--end", default=None,
                    help="inclusive end date (default: yesterday UTC)")
    ap.add_argument("--out", default="data/binance")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = (datetime.strptime(args.end, "%Y-%m-%d").date() if args.end
           else datetime.now(timezone.utc).date() - timedelta(days=1))

    out = Path(args.out)
    raw_dir = out / "raw"
    out.mkdir(parents=True, exist_ok=True)

    jobs = plan_jobs(args.symbols, start, end)
    print(f"{len(jobs)} files to fetch ({start} -> {end})")

    results = {s: [] for s in args.symbols}
    problems = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(fetch_one, j, raw_dir) for j in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            job, path, status = fut.result()
            sym, kind, label, _ = job
            if path is not None:
                results[sym].append(path)
            else:
                problems.append((sym, label, status))
            if status not in ("ok", "cached") or i % 25 == 0:
                print(f"[{i}/{len(jobs)}] {sym} {label}: {status}")

    if problems:
        print("\nProblems:")
        for p in problems:
            print("  ", p)

    print("\nMerging:")
    for sym in args.symbols:
        if not results[sym]:
            print(f"  {sym}: no files downloaded, skipping")
            continue
        df = build_series(sym, results[sym], start, end)
        report_gaps(sym, df)
        csv_path = out / f"{sym}_1m.csv.gz"
        df.to_csv(csv_path)
        try:
            df.to_parquet(out / f"{sym}_1m.parquet")
        except ImportError:
            pass  # pyarrow not installed; CSV is still written
        print(f"    wrote {csv_path}")

    print("\nDone. Raw zips are kept in", raw_dir)


if __name__ == "__main__":
    main()
