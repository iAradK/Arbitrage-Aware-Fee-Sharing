#!/usr/bin/env python3
"""
Download per-block base fee and priority-fee percentiles using eth_feeHistory.

Faster than v1:
  * several requests in flight at once (--workers), written to disk in block order
  * date -> block lookup uses a slot-time estimate (about 10-15 calls, not ~50)
  * no fixed sleep between calls; it backs off only when the provider says 429
  * still resumable and still shrinks a request that the provider rejects

Usage:
    pip install requests
    export ETH_RPC_URL=https://mainnet.infura.io/v3/<key>
    python download_fee_history.py --start 2025-09-01 --end 2026-09-01
    python download_fee_history.py --start-block 25656293 --end-block 25663469 --workers 4

Output: data/gas/fee_history.csv with columns
    block_number, base_fee_wei, gas_used_ratio, tip_p10_wei, tip_p50_wei,
    tip_p90_wei, gas_price_p50_wei (= base_fee + tip_p50), plus *_gwei columns.

eth_feeHistory does not return timestamps. Join on block_number with your swap
data or with BigQuery's crypto_ethereum.blocks.
"""
import argparse
import csv
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

PERCENTILES = [10, 50, 90]
SLOT = 12  # seconds per block on Ethereum mainnet
_local = threading.local()


class RpcError(Exception):
    pass


def session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def rpc(url, method, params, retries=8):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(retries):
        try:
            r = session().post(url, json=payload, timeout=60)
        except requests.RequestException:
            time.sleep(min(30, 2 ** attempt))
            continue
        if r.status_code == 429:
            wait = min(30, 1.5 ** (attempt + 2))
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(min(30, 2 ** attempt))
            continue
        try:
            j = r.json()
        except ValueError:
            raise RpcError(f"non-JSON response (HTTP {r.status_code})")
        if "error" in j:
            msg = str(j["error"])
            m = msg.lower()
            if "rate limit" in m or "too many" in m or "capacity" in m:
                time.sleep(min(30, 1.5 ** (attempt + 2)))
                continue
            raise RpcError(msg)
        return j["result"]
    raise RpcError("too many retries")


# ---------- date -> block ----------

def block_ts(url, n):
    return int(rpc(url, "eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)


def block_at_or_after(url, ts, head):
    """Smallest block with timestamp >= ts, via slot estimate then bracketed binary search."""
    guess = max(1, head - (block_ts(url, head) - ts) // SLOT)
    guess = min(guess, head)
    for _ in range(4):  # refine the estimate
        diff = ts - block_ts(url, guess)
        if abs(diff) <= 5 * SLOT:
            break
        guess = min(head, max(1, guess + diff // SLOT))
    lo, hi = max(1, guess - 100), min(head, guess + 100)
    # make sure the bracket is valid, else widen to the full range
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


# ---------- fee history ----------

def fee_history(url, first, last):
    count = last - first + 1
    res = rpc(url, "eth_feeHistory", [hex(count), hex(last), PERCENTILES])
    oldest = int(res["oldestBlock"], 16)
    base = [int(x, 16) for x in res["baseFeePerGas"]]
    ratio = res["gasUsedRatio"]
    reward = res.get("reward") or [[] for _ in ratio]
    rows = []
    for i in range(len(ratio)):
        tips = [int(x, 16) for x in reward[i]] if reward[i] else [0, 0, 0]
        b = base[i]
        rows.append({
            "block_number": oldest + i,
            "base_fee_wei": b,
            "gas_used_ratio": ratio[i],
            "tip_p10_wei": tips[0],
            "tip_p50_wei": tips[1],
            "tip_p90_wei": tips[2],
            "gas_price_p50_wei": b + tips[1],
            "base_fee_gwei": b / 1e9,
            "tip_p50_gwei": tips[1] / 1e9,
            "gas_price_p50_gwei": (b + tips[1]) / 1e9,
        })
    return rows


def fee_history_adaptive(url, first, last):
    """Fetch first..last; if the provider rejects the size, split and retry."""
    try:
        return fee_history(url, first, last)
    except RpcError as e:
        if last - first + 1 <= 64:
            raise
        mid = (first + last) // 2
        print(f"  error on {first}-{last} ({str(e)[:70]}); splitting")
        return (fee_history_adaptive(url, first, mid)
                + fee_history_adaptive(url, mid + 1, last))


FIELDS = ["block_number", "base_fee_wei", "gas_used_ratio", "tip_p10_wei",
          "tip_p50_wei", "tip_p90_wei", "gas_price_p50_wei",
          "base_fee_gwei", "tip_p50_gwei", "gas_price_p50_gwei"]


def edge_blocks_in_file(path):
    """(first_block, last_block) already in the CSV, or (None, None) if empty/new."""
    if not path.exists() or path.stat().st_size == 0:
        return None, None
    with open(path, "rb") as f:
        head = f.read(4096).decode(errors="ignore").splitlines()
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 4096))
        tail = f.read().decode(errors="ignore").strip().splitlines()
    try:
        return int(head[1].split(",")[0]), int(tail[-1].split(",")[0])
    except (ValueError, IndexError):
        return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("ETH_RPC_URL"))
    ap.add_argument("--start", help="UTC date YYYY-MM-DD")
    ap.add_argument("--end", help="UTC date YYYY-MM-DD (exclusive)")
    ap.add_argument("--start-block", type=int)
    ap.add_argument("--end-block", type=int, help="inclusive")
    ap.add_argument("--chunk", type=int, default=1024, help="blocks per call")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel requests; lower it if you see 429 messages")
    ap.add_argument("--out", default="data/gas/fee_history.csv")
    args = ap.parse_args()
    if not args.rpc:
        raise SystemExit("Provide --rpc or set ETH_RPC_URL")

    head = int(rpc(args.rpc, "eth_blockNumber", []), 16)
    if args.start_block is not None:
        b0, b1 = args.start_block, min(args.end_block or head, head)
    else:
        to_ts = lambda s: int(datetime.strptime(s, "%Y-%m-%d")
                              .replace(tzinfo=timezone.utc).timestamp())
        b0 = block_at_or_after(args.rpc, to_ts(args.start), head)
        b1 = min(block_at_or_after(args.rpc, to_ts(args.end), head) - 1, head)
    print(f"Blocks {b0:,} -> {b1:,} ({b1 - b0 + 1:,} blocks)")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    first_in_file, done = edge_blocks_in_file(out)
    if first_in_file is not None:
        if first_in_file > b0:
            raise SystemExit(
                f"{out} already holds blocks {first_in_file:,} -> {done:,}, but you asked for a range "
                f"starting at {b0:,}.\nResuming would silently skip {first_in_file - b0:,} blocks. "
                f"Either delete/rename that file, or fetch the missing head into a new file:\n"
                f"  python download_fee_history.py --start-block {b0} --end-block {first_in_file - 1} "
                f"--out <new_file.csv>\nand then merge the two files.")
        if done >= b0:
            print(f"Resuming after block {done:,}")
            b0 = done + 1
    if b0 > b1:
        print("Nothing to do.")
        return
    write_header = not out.exists() or out.stat().st_size == 0

    ranges = [(a, min(a + args.chunk - 1, b1)) for a in range(b0, b1 + 1, args.chunk)]
    total = b1 - b0 + 1
    t0 = time.time()
    written = 0

    with open(out, "a", newline="") as f, ThreadPoolExecutor(args.workers) as ex:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        it = iter(ranges)
        inflight = deque()
        # keep a bounded window in flight; results are written strictly in order
        for _ in range(args.workers * 2):
            r = next(it, None)
            if r:
                inflight.append(ex.submit(fee_history_adaptive, args.rpc, *r))
        n = 0
        while inflight:
            rows = inflight.popleft().result()
            w.writerows(rows)
            f.flush()
            written += len(rows)
            r = next(it, None)
            if r:
                inflight.append(ex.submit(fee_history_adaptive, args.rpc, *r))
            n += 1
            if n % 25 == 0 or not inflight:
                rate = written / max(1e-9, time.time() - t0)
                eta = (total - written) / max(1e-9, rate)
                print(f"  {written:,}/{total:,} blocks "
                      f"({100 * written / total:.1f}%), {rate:,.0f} blocks/s, "
                      f"ETA {eta / 60:.1f} min")
    print(f"Done -> {out}")


if __name__ == "__main__":
    main()