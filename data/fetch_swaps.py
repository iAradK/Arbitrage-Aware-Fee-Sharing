"""Download one year of Uniswap v4 (Ethereum mainnet) swaps for several pools.

- Writes each page to CSV as it goes (low memory use).
- Resumable: if interrupted, just run it again and it continues where it stopped.
- Retries automatically on timeouts / temporary errors.

Usage:  pip install requests   then   python fetch_swaps.py
"""
import csv
import json
import os
import time
from datetime import datetime, timezone

import requests

API_KEY = "7b29fe242115ee00cd5e439f1a12418d"
SUBGRAPH_ID = "DiYPVdygkfjDWhbxGSqAQxwBKmfKnkWQojqeM2rkLb3G"  # Uniswap v4, Ethereum mainnet
ENDPOINT = f"https://gateway.thegraph.com/api/{API_KEY}/subgraphs/id/{SUBGRAPH_ID}"

POOLS = {
    "eth_usdc_005": "0x21c67e77068de97969ba93d4aab21826d33ca12bb9f565d8496e8fda8a82ca27",
    "usdc_usdt_0001": "0x8aa4e11cbdf30eedc92100f4c8a31ff748e201d44712cc8c90d189edaa8e4e47",
    "eth_wbtc_030": "0x54c72c46df32f2cc455e84e41e191b26ed73a29452cdd3d82f511097af9f427e",
    "eth_wsteth_001": "0x1d5b2949ece8754c2d736991c62c5162bd144f497b2212182401b9bae77e2d76",  # optional
}

START_DATE = "2025-09-1"  # inclusive, UTC
END_DATE = "2026-09-1"    # exclusive, UTC
OUT_DIR = "swaps_data"
PAGE = 1000
DELAY_S = 0.3
MAX_RETRIES = 6

COLS = ["id", "timestamp", "logIndex", "amount0", "amount1", "amountUSD", "sqrtPriceX96",
        "tick", "sender", "origin"]
TX_COLS = ["id", "timestamp", "blockNumber", "gasUsed", "gasPrice"]
HEADER = COLS + [f"tx_{c}" for c in TX_COLS]


def ts(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def build_query(pool, from_ts, end_ts):
    return f"""{{
      swaps(first: {PAGE}, orderBy: timestamp, orderDirection: asc,
        where: {{ pool: "{pool}", timestamp_gte: "{from_ts}", timestamp_lt: "{end_ts}" }}) {{
        id timestamp logIndex amount0 amount1 amountUSD sqrtPriceX96 tick sender origin
        transaction {{ id timestamp blockNumber gasUsed gasPrice }}
      }}
    }}"""


def fetch_page(pool, cursor, end):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(ENDPOINT, json={"query": build_query(pool, cursor, end)}, timeout=90)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data["data"]["swaps"]
        except Exception as e:  # network error, timeout, indexer hiccup
            if attempt == MAX_RETRIES:
                raise
            wait = 2 ** attempt
            print(f"  attempt {attempt} failed ({e}); retrying in {wait}s")
            time.sleep(wait)


def download_pool(label, pool, start, end):
    os.makedirs(OUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUT_DIR, f"swaps_{label}.csv")
    state_path = os.path.join(OUT_DIR, f"state_{label}.json")

    # Resume from saved state, trimming any rows written after the last saved checkpoint.
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
        if state.get("done"):
            print(f"[{label}] already complete, skipping.")
            return
        with open(csv_path, "r+b") as f:
            f.truncate(state["csv_bytes"])
        print(f"[{label}] resuming from {datetime.fromtimestamp(state['cursor'], timezone.utc)}")
    else:
        with open(csv_path, "w", newline="") as f:
            csv.writer(f).writerow(HEADER)
        state = {"cursor": start, "ids_at_cursor": [], "rows": 0,
                 "csv_bytes": os.path.getsize(csv_path), "done": False}

    cursor = state["cursor"]
    ids_at_cursor = set(state["ids_at_cursor"])

    while True:
        batch = fetch_page(pool, cursor, end)
        new_rows = [s for s in batch if s["id"] not in ids_at_cursor]

        with open(csv_path, "a", newline="") as f:
            w = csv.writer(f)
            for s in new_rows:
                tx = s.get("transaction") or {}
                w.writerow([s.get(c) for c in COLS] + [tx.get(c) for c in TX_COLS])
            f.flush()
            csv_bytes = f.tell()

        state["rows"] += len(new_rows)
        last_ts = int(batch[-1]["timestamp"]) if batch else cursor
        done = len(batch) < PAGE

        if not done:
            if last_ts == cursor and not new_rows:
                print(f"  warning: >={PAGE} swaps at timestamp {cursor}; skipping ahead 1s")
                last_ts, ids_at_cursor = cursor + 1, set()
            elif last_ts == cursor:
                ids_at_cursor |= {s["id"] for s in batch}
            else:
                ids_at_cursor = {s["id"] for s in batch if int(s["timestamp"]) == last_ts}
            cursor = last_ts

        state.update(cursor=cursor, ids_at_cursor=sorted(ids_at_cursor),
                     csv_bytes=csv_bytes, done=done)
        with open(state_path, "w") as f:
            json.dump(state, f)

        print(f"[{label}] +{len(new_rows)} (total {state['rows']}) up to "
              f"{datetime.fromtimestamp(last_ts, timezone.utc):%Y-%m-%d %H:%M}")
        if done:
            print(f"[{label}] finished: {state['rows']} swaps -> {csv_path}")
            return
        time.sleep(DELAY_S)


def main():
    start, end = ts(START_DATE), ts(END_DATE)
    for label, pool in POOLS.items():
        download_pool(label, pool, start, end)


if __name__ == "__main__":
    main()
