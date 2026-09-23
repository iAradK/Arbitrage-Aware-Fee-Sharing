"""Parallel, resumable downloader for Uniswap v4 (Ethereum mainnet) swaps.

How it works:
- The date range for each pool is split into chunks (CHUNK_DAYS each).
- Chunks are downloaded in parallel threads (MAX_WORKERS at a time).
- Each chunk writes its own CSV part + state file, so the whole run is resumable:
  if it stops, just run it again.
- When all chunks of a pool are done, they are merged into swaps_data/swaps_<label>.csv.

Usage:  pip install requests   then   python fetch_swaps.py
"""
import csv
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

API_KEY = "7b29fe242115ee00cd5e439f1a12418d"
SUBGRAPH_ID = "DiYPVdygkfjDWhbxGSqAQxwBKmfKnkWQojqeM2rkLb3G"  # Uniswap v4, Ethereum mainnet
ENDPOINT = f"https://gateway.thegraph.com/api/{API_KEY}/subgraphs/id/{SUBGRAPH_ID}"

POOLS = {
    "eth_usdc_005": "0x21c67e77068de97969ba93d4aab21826d33ca12bb9f565d8496e8fda8a82ca27",
    "usdc_usdt_0001": "0x8aa4e11cbdf30eedc92100f4c8a31ff748e201d44712cc8c90d189edaa8e4e47",
    "eth_wbtc_030": "0x54c72c46df32f2cc455e84e41e191b26ed73a29452cdd3d82f511097af9f427e",
    "eth_wsteth_001": "0x1d5b2949ece8754c2d736991c62c5162bd144f497b2212182401b9bae77e2d76",
}

START_DATE = "2025-09-01"  # inclusive, UTC
END_DATE = "2026-09-01"    # exclusive, UTC
CHUNK_DAYS = 15            # smaller chunks = more parallelism
MAX_WORKERS = 6            # parallel requests; lower this if you see many 429 errors
OUT_DIR = "swaps_data"
PAGE = 1000                # keep at 1000 (the maximum)
MAX_RETRIES = 6

COLS = ["id", "timestamp", "logIndex", "amount0", "amount1", "amountUSD", "sqrtPriceX96",
        "tick", "sender", "origin"]
TX_COLS = ["id", "timestamp", "blockNumber", "gasUsed", "gasPrice"]
HEADER = COLS + [f"tx_{c}" for c in TX_COLS]

_local = threading.local()
_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def ts(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def fmt(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def build_query(pool, from_ts, end_ts, skip):
    return f"""{{
      swaps(first: {PAGE}, skip: {skip}, orderBy: timestamp, orderDirection: asc,
        where: {{ pool: "{pool}", timestamp_gte: "{from_ts}", timestamp_lt: "{end_ts}" }}) {{
        id timestamp logIndex amount0 amount1 amountUSD sqrtPriceX96 tick sender origin
        transaction {{ id timestamp blockNumber gasUsed gasPrice }}
      }}
    }}"""


def fetch_page(pool, cursor, end, skip):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session().post(ENDPOINT, json={"query": build_query(pool, cursor, end, skip)},
                               timeout=90)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                msg = str(data["errors"])
                if "auth error" in msg:  # retrying won't help; fail fast
                    raise SystemExit(f"Auth problem with API key: {msg}")
                raise RuntimeError(f"GraphQL errors: {msg}")
            return data["data"]["swaps"]
        except SystemExit:
            raise
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            wait = 2 ** attempt
            log(f"  retry {attempt} ({e}); waiting {wait}s")
            time.sleep(wait)


def download_chunk(label, pool, start, end):
    """Download swaps with start <= timestamp < end into their own part file."""
    part_dir = os.path.join(OUT_DIR, label, "parts")
    os.makedirs(part_dir, exist_ok=True)
    name = f"{fmt(start)}_{fmt(end)}"
    csv_path = os.path.join(part_dir, f"{name}.csv")
    state_path = os.path.join(part_dir, f"{name}.state.json")
    tag = f"[{label} {name}]"

    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
        if state["done"]:
            return state["rows"]
        with open(csv_path, "r+b") as f:
            f.truncate(state["csv_bytes"])  # drop rows written after the last checkpoint
    else:
        with open(csv_path, "w", newline="") as f:
            csv.writer(f).writerow(HEADER)
        state = {"cursor": start, "skip": 0, "ids_at_cursor": [], "rows": 0,
                 "csv_bytes": os.path.getsize(csv_path), "done": False}

    cursor, skip = state["cursor"], state["skip"]
    ids_at_cursor = set(state["ids_at_cursor"])

    while True:
        batch = fetch_page(pool, cursor, end, skip)
        new_rows = [s for s in batch if s["id"] not in ids_at_cursor]

        with open(csv_path, "a", newline="") as f:
            w = csv.writer(f)
            for s in new_rows:
                tx = s.get("transaction") or {}
                w.writerow([s.get(c) for c in COLS] + [tx.get(c) for c in TX_COLS])
            f.flush()
            csv_bytes = f.tell()

        state["rows"] += len(new_rows)
        done = len(batch) < PAGE
        if not done:
            last_ts = int(batch[-1]["timestamp"])
            if last_ts == cursor:
                # Whole page shares one timestamp: page through it with skip.
                ids_at_cursor |= {s["id"] for s in batch}
                skip = len(ids_at_cursor)
            else:
                ids_at_cursor = {s["id"] for s in batch if int(s["timestamp"]) == last_ts}
                cursor, skip = last_ts, 0

        state.update(cursor=cursor, skip=skip, ids_at_cursor=sorted(ids_at_cursor),
                     csv_bytes=csv_bytes, done=done)
        with open(state_path, "w") as f:
            json.dump(state, f)

        if done:
            log(f"{tag} done: {state['rows']} swaps")
            return state["rows"]
        log(f"{tag} {state['rows']} swaps so far, up to "
            f"{datetime.fromtimestamp(cursor, timezone.utc):%Y-%m-%d %H:%M}")


def chunks(start, end):
    step = CHUNK_DAYS * 86400
    out, t = [], start
    while t < end:
        out.append((t, min(t + step, end)))
        t += step
    return out


def merge_pool(label, pool_chunks):
    """Concatenate a pool's chunk files (in time order) into one CSV."""
    part_dir = os.path.join(OUT_DIR, label, "parts")
    out_path = os.path.join(OUT_DIR, f"swaps_{label}.csv")
    total = 0
    with open(out_path, "w", newline="") as out:
        out.write(",".join(HEADER) + "\r\n")
        for s, e in pool_chunks:
            with open(os.path.join(part_dir, f"{fmt(s)}_{fmt(e)}.csv"), newline="") as f:
                next(f)  # skip header
                for line in f:
                    out.write(line)
                    total += 1
    log(f"[{label}] merged {total} swaps -> {out_path}")


def main():
    start, end = ts(START_DATE), ts(END_DATE)
    pool_chunks = chunks(start, end)
    tasks = [(label, pool, s, e) for label, pool in POOLS.items() for s, e in pool_chunks]
    log(f"{len(tasks)} chunks across {len(POOLS)} pools, {MAX_WORKERS} workers")

    failed = set()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(download_chunk, *t): t for t in tasks}
        for fut in as_completed(futures):
            label, _, s, e = futures[fut]
            try:
                fut.result()
            except BaseException as err:
                failed.add(label)
                log(f"[{label} {fmt(s)}_{fmt(e)}] FAILED: {err}")

    for label in POOLS:
        if label in failed:
            log(f"[{label}] has failed chunks; run the script again to finish them.")
        else:
            merge_pool(label, pool_chunks)


if __name__ == "__main__":
    main()
