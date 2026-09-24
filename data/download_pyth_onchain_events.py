#!/usr/bin/env python3
"""
Export Pyth `PriceFeedUpdate` events from an Ethereum RPC and compute on-chain
staleness (block timestamp - publishTime) for each update.

Event (Pyth EVM contract):
    PriceFeedUpdate(bytes32 indexed id, uint64 publishTime, int64 price, uint64 conf)

Faster than v1:
  * date -> block lookup takes ~11 calls instead of ~50
  * eth_getLogs uses big block ranges (default 50,000). A single feed has only a
    few dozen events per day, so large ranges are cheap; if the provider rejects
    a range (too many results / too wide) it is split automatically
  * chunks and block-timestamp batches run in parallel (--workers)

Usage:
    pip install requests pandas
    export ETH_RPC_URL=https://mainnet.infura.io/v3/<key>
    python download_pyth_onchain_events.py --start 2025-09-01 --end 2026-09-01

Output: data/pyth/onchain_<FEED>_events.csv.gz with columns
    block_number, block_time, tx_hash, log_index, publish_time, price, conf,
    price_usd, staleness_s

Caveats:
  * Pyth is pull-based: an event exists only when someone pushed an update. The
    gaps between events are part of what an oracle-delay study measures.
  * The Pyth Core upgrade (26 Aug 2026) changed aggregation and signing. Docs say
    the ABI is unchanged and existing addresses were upgraded in place, but new
    contracts were also deployed at new addresses. Check which address your
    protocol reads and pass it with --address (run again for each address).
  * Verify the feed IDs below on Pyth's price feed page.
"""
import argparse
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

PYTH_ETH_MAINNET = "0x4305FB66699C3B2702D4d05CF36551390A4c69C6"
# keccak256("PriceFeedUpdate(bytes32,uint64,int64,uint64)")
TOPIC0 = "0xd06a6b7f4918494b3719217d1802786c1f5112a6c1d88fe2cfec00b4584f6aec"
FEEDS = {
    "ETH_USD": "0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "BTC_USD": "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
}
EXPO = -8
SLOT = 12
_local = threading.local()


class RpcError(Exception):
    pass


def session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def _post(url, payload, retries=8):
    """POST with backoff on 429/5xx/network errors. Returns parsed JSON."""
    for attempt in range(retries):
        try:
            r = session().post(url, json=payload, timeout=120)
        except requests.RequestException:
            time.sleep(min(30, 2 ** attempt))
            continue
        if r.status_code == 429:
            time.sleep(min(30, 1.5 ** (attempt + 2)))
            continue
        if r.status_code >= 500:
            time.sleep(min(30, 2 ** attempt))
            continue
        try:
            return r.json()
        except ValueError:
            raise RpcError(f"non-JSON response (HTTP {r.status_code})")
    raise RpcError("too many retries")


def rpc(url, method, params):
    j = _post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in j:
        raise RpcError(str(j["error"]))
    return j["result"]


def rpc_batch(url, calls):
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
               for i, (m, p) in enumerate(calls)]
    j = _post(url, payload)
    if isinstance(j, dict):  # provider rejected the batch as a whole
        raise RpcError(str(j.get("error", j)))
    j = sorted(j, key=lambda x: x["id"])
    if any("error" in x for x in j):
        raise RpcError(str(next(x["error"] for x in j if "error" in x)))
    return [x["result"] for x in j]


# ---------- date -> block ----------

def block_ts(url, n):
    return int(rpc(url, "eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)


def block_at_or_after(url, ts, head):
    """Smallest block with timestamp >= ts (slot estimate + bracketed binary search)."""
    guess = min(head, max(1, head - (block_ts(url, head) - ts) // SLOT))
    for _ in range(4):
        diff = ts - block_ts(url, guess)
        if abs(diff) <= 5 * SLOT:
            break
        guess = min(head, max(1, guess + diff // SLOT))
    lo, hi = max(1, guess - 100), min(head, guess + 100)
    if block_ts(url, lo) >= ts:
        lo = 1
    if block_ts(url, hi) < ts:
        hi = head
    while lo < hi:
        mid = (lo + hi) // 2
        if block_ts(url, mid) < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ---------- logs ----------

MAX_RANGE = [None]  # block-range limit learned from the provider's error message


def get_logs(url, address, feed_id, a, b):
    """eth_getLogs over [a, b]. Learns the provider's max range from the first
    error ("... exceeds limit of N") and splits into pieces of that size; falls
    back to halving if the error message has no limit."""
    limit = MAX_RANGE[0]
    if limit and b - a + 1 > limit:
        out = []
        for s in range(a, b + 1, limit):
            out += get_logs(url, address, feed_id, s, min(s + limit - 1, b))
        return out
    params = [{
        "address": address,
        "topics": [TOPIC0, feed_id],
        "fromBlock": hex(a),
        "toBlock": hex(b),
    }]
    try:
        return rpc(url, "eth_getLogs", params)
    except RpcError as e:
        if b - a < 200:
            raise
        m = re.search(r"limit of (\d+)", str(e))
        if m and int(m.group(1)) < b - a + 1:
            if MAX_RANGE[0] != int(m.group(1)):
                MAX_RANGE[0] = int(m.group(1))
                print(f"    provider block-range limit: {MAX_RANGE[0]:,}")
            return get_logs(url, address, feed_id, a, b)
        mid = (a + b) // 2
        print(f"    splitting {a:,}-{b:,} ({str(e)[:70]})")
        return (get_logs(url, address, feed_id, a, mid)
                + get_logs(url, address, feed_id, mid + 1, b))


def to_signed(x, bits=256):
    return x - (1 << bits) if x >= (1 << (bits - 1)) else x


def decode(log):
    data = log["data"][2:]
    words = [int(data[i:i + 64], 16) for i in range(0, 192, 64)]
    return {
        "block_number": int(log["blockNumber"], 16),
        "tx_hash": log["transactionHash"],
        "log_index": int(log["logIndex"], 16),
        "publish_time": words[0],
        "price": to_signed(words[1]),   # int64 is sign-extended to 256 bits
        "conf": words[2],
        "block_ts_hint": int(log["blockTimestamp"], 16) if log.get("blockTimestamp") else None,
    }


def fetch_block_times(url, blocks, workers, batch=100):
    """{block_number: timestamp} for the given blocks, using batched requests."""
    groups = [blocks[i:i + batch] for i in range(0, len(blocks), batch)]

    def one(group):
        res = rpc_batch(url, [("eth_getBlockByNumber", [hex(b), False]) for b in group])
        return {b: int(r["timestamp"], 16) for b, r in zip(group, res)}

    out = {}
    with ThreadPoolExecutor(workers) as ex:
        for i, part in enumerate(ex.map(one, groups), 1):
            out.update(part)
            if i % 10 == 0 or i == len(groups):
                print(f"    block timestamps: {len(out):,}/{len(blocks):,}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("ETH_RPC_URL"))
    ap.add_argument("--address", default=PYTH_ETH_MAINNET)
    ap.add_argument("--feeds", nargs="+", default=["ETH_USD"],
                    help=f"names from {list(FEEDS)} or raw 0x feed ids")
    ap.add_argument("--start", default="2025-09-01")
    ap.add_argument("--end", default=None, help="exclusive end date (default: now)")
    ap.add_argument("--chunk", type=int, default=50_000,
                    help="blocks per eth_getLogs call; auto-splits if rejected")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--expo", type=int, default=EXPO)
    ap.add_argument("--out", default="data/pyth")
    args = ap.parse_args()
    if not args.rpc:
        raise SystemExit("Provide --rpc or set ETH_RPC_URL")

    to_ts = lambda d: int(datetime.strptime(d, "%Y-%m-%d")
                          .replace(tzinfo=timezone.utc).timestamp())
    end_ts = to_ts(args.end) if args.end else int(datetime.now(timezone.utc).timestamp())

    head = int(rpc(args.rpc, "eth_blockNumber", []), 16)
    b0 = block_at_or_after(args.rpc, to_ts(args.start), head)
    b1 = min(block_at_or_after(args.rpc, end_ts, head) - 1, head)
    print(f"Block range {b0:,} -> {b1:,} ({b1 - b0 + 1:,} blocks)")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ranges = [(a, min(a + args.chunk - 1, b1)) for a in range(b0, b1 + 1, args.chunk)]

    for feed in args.feeds:
        feed_id = FEEDS.get(feed, feed)
        name = feed if feed in FEEDS else feed_id[:10]
        print(f"Feed {name} ({feed_id}), {len(ranges)} log queries")

        def one(r):
            return get_logs(args.rpc, args.address, feed_id, *r)

        rows = []
        with ThreadPoolExecutor(args.workers) as ex:
            for i, logs in enumerate(ex.map(one, ranges), 1):
                rows.extend(decode(l) for l in logs)
                if i % 5 == 0 or i == len(ranges):
                    print(f"  {i}/{len(ranges)} queries, events so far: {len(rows):,}")

        if not rows:
            print("  no events found. Check --address, feed id and block range.")
            continue

        df = pd.DataFrame(rows)
        if df["block_ts_hint"].notna().all():
            df["block_time_s"] = df["block_ts_hint"].astype("int64")
        else:
            blocks = sorted(df["block_number"].unique().tolist())
            ts = fetch_block_times(args.rpc, blocks, args.workers)
            df["block_time_s"] = df["block_number"].map(ts)

        df["block_time"] = pd.to_datetime(df["block_time_s"], unit="s", utc=True)
        df["price_usd"] = df["price"] * (10.0 ** args.expo)
        df["staleness_s"] = df["block_time_s"] - df["publish_time"]
        df = df.sort_values(["block_number", "log_index"]).reset_index(drop=True)
        cols = ["block_number", "block_time", "tx_hash", "log_index", "publish_time",
                "price", "conf", "price_usd", "staleness_s"]
        path = out / f"onchain_{name}_events.csv.gz"
        df[cols].to_csv(path, index=False)
        print(f"  {len(df):,} events -> {path}")
        print(df["staleness_s"].describe(percentiles=[.5, .9, .99]).round(2).to_string())


if __name__ == "__main__":
    main()