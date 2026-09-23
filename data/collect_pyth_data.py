import os
import time
import json
import requests
import pandas as pd

from datetime import datetime, timezone


# ============================================================
# Configuration
# ============================================================

BASE_URL = "https://benchmarks.pyth.network/v1/shims/tradingview/history"

SYMBOL = "Crypto.ETH/USD"
RESOLUTION = "1"

# Jan 1, 2026 00:00:00 UTC
START_TS = 1767225600

# Apr 1, 2026 00:00:00 UTC
END_TS = 1775001600

# Daily chunks avoid "Too many datapoints to return"
CHUNK_DAYS = 1

OUT_DIR = "pyth_eth_usd_tradingview_2026_01_01_to_2026_04_01_1min"
CHECKPOINT_DIR = os.path.join(OUT_DIR, "daily_chunks")
LOG_FILE = os.path.join(OUT_DIR, "download.log")
FINAL_CSV = os.path.join(
    OUT_DIR,
    "pyth_eth_usd_2026_01_01_to_2026_04_01_1min.csv"
)

REQUEST_TIMEOUT = 60
SLEEP_BETWEEN_REQUESTS = 1.0


# ============================================================
# Helpers
# ============================================================

def utc_string(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def log(message: str) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    line = f"[{datetime.now(timezone.utc).isoformat()}] {message}"
    print(line, flush=True)

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_chunk(start_ts: int, end_ts: int) -> pd.DataFrame:
    params = {
        "symbol": SYMBOL,
        "resolution": RESOLUTION,
        "from": start_ts,
        "to": end_ts,
    }

    response = requests.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)

    log(f"GET {response.url}")
    log(f"HTTP {response.status_code}")

    response.raise_for_status()
    data = response.json()

    if data.get("s") != "ok":
        raise RuntimeError(f"Non-ok response: {json.dumps(data)[:1000]}")

    timestamps = data.get("t", [])
    opens = data.get("o", [])
    highs = data.get("h", [])
    lows = data.get("l", [])
    closes = data.get("c", [])

    rows = []

    for i, ts in enumerate(timestamps):
        rows.append({
            "timestamp": int(ts),
            "datetime": utc_string(int(ts)),
            "pyth_open": opens[i] if i < len(opens) else None,
            "pyth_high": highs[i] if i < len(highs) else None,
            "pyth_low": lows[i] if i < len(lows) else None,
            "pyth_close": closes[i] if i < len(closes) else None,
        })

    return pd.DataFrame(rows)


def merge_chunks() -> None:
    frames = []

    for filename in sorted(os.listdir(CHECKPOINT_DIR)):
        if not filename.endswith(".csv"):
            continue

        path = os.path.join(CHECKPOINT_DIR, filename)
        frames.append(pd.read_csv(path))

    if not frames:
        log("No chunks found to merge.")
        return

    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates(subset=["timestamp"], keep="last")
    merged = merged.sort_values("timestamp")

    merged.to_csv(FINAL_CSV, index=False)

    log(f"Final CSV written: {FINAL_CSV}")
    log(f"Final rows: {len(merged)}")

    if len(merged) > 0:
        log(f"First row: {merged.iloc[0].to_dict()}")
        log(f"Last row: {merged.iloc[-1].to_dict()}")


# ============================================================
# Main
# ============================================================

def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)

    log("Starting Pyth TradingView ETH/USD download.")
    log(f"Window: {utc_string(START_TS)} to {utc_string(END_TS)}")
    log(f"Resolution: {RESOLUTION}")
    log(f"Chunk size: {CHUNK_DAYS} day(s)")

    chunk_seconds = CHUNK_DAYS * 24 * 60 * 60

    cur = START_TS
    chunk_id = 1

    while cur < END_TS:
        nxt = min(cur + chunk_seconds, END_TS)

        checkpoint = os.path.join(
            CHECKPOINT_DIR,
            f"chunk_{chunk_id:03d}_{cur}_{nxt}.csv"
        )

        if os.path.exists(checkpoint):
            log(
                f"Skipping existing chunk {chunk_id}: "
                f"{utc_string(cur)} to {utc_string(nxt)}"
            )
        else:
            log(
                f"Fetching chunk {chunk_id}: "
                f"{utc_string(cur)} to {utc_string(nxt)}"
            )

            try:
                df = fetch_chunk(cur, nxt)
                df.to_csv(checkpoint, index=False)

                log(
                    f"Saved chunk {chunk_id}: "
                    f"{len(df)} rows to {checkpoint}"
                )

            except Exception as e:
                log(f"ERROR in chunk {chunk_id}: {e}")
                log("Stopping. You can rerun the script and it will resume.")
                raise

            time.sleep(SLEEP_BETWEEN_REQUESTS)

        cur = nxt
        chunk_id += 1

    log("All chunks downloaded. Merging.")
    merge_chunks()
    log("Done.")


if __name__ == "__main__":
    main()