#!/usr/bin/env python3
"""
Export the wstETH/ETH exchange rate history from Lido's `TokenRebased` events.

    stEthPerToken = totalPooledEther / totalShares   (ETH per 1 wstETH)

The rate changes only when the Lido oracle reports (about once a day), so the
event list (~365 rows/year) is a complete step function. Forward-fill it onto
your minute or block grid; do not interpolate.

Event (Lido stETH contract):
    TokenRebased(uint256 indexed reportTimestamp, uint256 timeElapsed,
                 uint256 preTotalShares, uint256 preTotalEther,
                 uint256 postTotalShares, uint256 postTotalEther,
                 uint256 sharesMintedAsFees)

Usage:
    pip install requests pandas
    export ETH_RPC_URL=https://mainnet.infura.io/v3/<key>
    python download_wsteth_rate.py --start 2025-09-01 --end 2026-09-01
    python download_wsteth_rate.py --start 2025-09-01 --end 2026-09-01 --verify

Output: data/lido/wsteth_rate_events.csv with columns
    block_number, block_time, tx_hash, report_timestamp, time_elapsed,
    post_total_shares, post_total_ether, eth_per_wsteth, wsteth_per_eth

The script starts a few days BEFORE --start so that the first minute of your
period already has a rate to forward-fill from.

--verify compares the newest event's rate with wstETH.stEthPerToken() read at
the latest block (works without an archive node; the two agree unless a new
report landed in between).
"""
import argparse
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

LIDO_STETH = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
WSTETH = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"
# keccak256("TokenRebased(uint256,uint256,uint256,uint256,uint256,uint256,uint256)")
TOPIC0 = "0xff08c3ef606d198e316ef5b822193c489965899eb4e3c248cea1a4626c3eda50"
ST_ETH_PER_TOKEN = "0x035faf82"  # selector of stEthPerToken()
SLOT = 12
MAX_RANGE = [None]
_local = threading.local()


class RpcError(Exception):
    pass


def session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def _post(url, payload, retries=8):
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
    if not isinstance(j, list) or any(not isinstance(x, dict) or "id" not in x for x in j):
        raise RpcError("malformed batch response: " + str(j)[:200])
    j = sorted(j, key=lambda x: x["id"])
    if len(j) != len(calls) or any("error" in x for x in j):
        errs = [x["error"] for x in j if "error" in x]
        raise RpcError(f"batch error: {str(errs[:1])[:200]} ({len(j)}/{len(calls)} answers)")
    return [x["result"] for x in j]


def block_ts(url, n):
    return int(rpc(url, "eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)


def block_at_or_after(url, ts, head):
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


def fetch_block_times(url, blocks, workers, batch=50):
    """{block: timestamp}. Uses batches, and falls back to single calls if a batch fails."""
    groups = [blocks[i:i + batch] for i in range(0, len(blocks), batch)]

    def one(group):
        try:
            res = rpc_batch(url, [("eth_getBlockByNumber", [hex(b), False]) for b in group])
            return {b: int(r["timestamp"], 16) for b, r in zip(group, res)}
        except (RpcError, KeyError, TypeError) as e:
            print(f"    batch failed ({str(e)[:110]}); using single calls for {len(group)} blocks")
            return {b: block_ts(url, b) for b in group}

    out = {}
    with ThreadPoolExecutor(workers) as ex:
        for part in ex.map(one, groups):
            out.update(part)
    print(f"    block timestamps: {len(out):,}/{len(blocks):,}")
    return out


def get_logs(url, a, b):
    """eth_getLogs for TokenRebased over [a, b]; learns the provider's range limit."""
    limit = MAX_RANGE[0]
    if limit and b - a + 1 > limit:
        out = []
        for s in range(a, b + 1, limit):
            out += get_logs(url, s, min(s + limit - 1, b))
        return out
    params = [{"address": LIDO_STETH, "topics": [TOPIC0],
               "fromBlock": hex(a), "toBlock": hex(b)}]
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
            return get_logs(url, a, b)
        mid = (a + b) // 2
        print(f"    splitting {a:,}-{b:,} ({str(e)[:70]})")
        return get_logs(url, a, mid) + get_logs(url, mid + 1, b)


def decode(log):
    # topics[1] = reportTimestamp (indexed); data = 6 words
    d = log["data"][2:]
    w = [int(d[i:i + 64], 16) for i in range(0, 6 * 64, 64)]
    return {
        "block_number": int(log["blockNumber"], 16),
        "tx_hash": log["transactionHash"],
        "log_index": int(log["logIndex"], 16),
        "report_timestamp": int(log["topics"][1], 16),
        "time_elapsed": w[0],
        "post_total_shares": w[3],
        "post_total_ether": w[4],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("ETH_RPC_URL"))
    ap.add_argument("--start", default="2025-09-01")
    ap.add_argument("--end", default=None, help="exclusive end date (default: now)")
    ap.add_argument("--pad-days", type=int, default=5,
                    help="extra days before --start so the first minutes have a rate")
    ap.add_argument("--chunk", type=int, default=50_000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--out", default="data/lido")
    args = ap.parse_args()
    if not args.rpc:
        raise SystemExit("Provide --rpc or set ETH_RPC_URL")

    parse = lambda d: datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = parse(args.start) - timedelta(days=args.pad_days)
    end = parse(args.end) if args.end else datetime.now(timezone.utc)

    head = int(rpc(args.rpc, "eth_blockNumber", []), 16)
    b0 = block_at_or_after(args.rpc, int(start.timestamp()), head)
    b1 = min(block_at_or_after(args.rpc, int(end.timestamp()), head) - 1, head)
    print(f"Block range {b0:,} -> {b1:,} ({b1 - b0 + 1:,} blocks)")

    ranges = [(a, min(a + args.chunk - 1, b1)) for a in range(b0, b1 + 1, args.chunk)]
    rows = []
    with ThreadPoolExecutor(args.workers) as ex:
        for i, logs in enumerate(ex.map(lambda r: get_logs(args.rpc, *r), ranges), 1):
            rows.extend(decode(l) for l in logs)
            if i % 10 == 0 or i == len(ranges):
                print(f"  {i}/{len(ranges)} queries, events so far: {len(rows):,}")
    if not rows:
        raise SystemExit("No TokenRebased events found. Check the RPC and date range.")

    df = pd.DataFrame(rows).sort_values(["block_number", "log_index"]).reset_index(drop=True)

    blocks = sorted(df["block_number"].unique().tolist())
    ts = fetch_block_times(args.rpc, blocks, args.workers)
    df["block_time"] = pd.to_datetime(df["block_number"].map(ts), unit="s", utc=True)

    df["eth_per_wsteth"] = [pe * 10**18 // ps / 1e18
                            for pe, ps in zip(df["post_total_ether"], df["post_total_shares"])]
    df["wsteth_per_eth"] = 1.0 / df["eth_per_wsteth"]
    cols = ["block_number", "block_time", "tx_hash", "report_timestamp", "time_elapsed",
            "post_total_shares", "post_total_ether", "eth_per_wsteth", "wsteth_per_eth"]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "wsteth_rate_events.csv"
    df[cols].to_csv(path, index=False)

    gaps = df["block_time"].diff().dropna()
    print(f"{len(df):,} events -> {path}")
    print(f"first: {df['block_time'].iloc[0]}  rate {df['eth_per_wsteth'].iloc[0]:.8f}")
    print(f"last:  {df['block_time'].iloc[-1]}  rate {df['eth_per_wsteth'].iloc[-1]:.8f}")
    print(f"gap between reports: median {gaps.median()}, max {gaps.max()}")
    if (df["eth_per_wsteth"].diff().dropna() < 0).any():
        print("NOTE: the rate decreased at least once (a negative rebase). Check those rows.")

    if args.verify:
        raw = rpc(args.rpc, "eth_call", [{"to": WSTETH, "data": ST_ETH_PER_TOKEN}, "latest"])
        onchain = int(raw, 16) / 1e18
        ev = df["eth_per_wsteth"].iloc[-1]
        print(f"verify: stEthPerToken() now = {onchain:.8f}, last event = {ev:.8f}, "
              f"relative diff = {abs(onchain - ev) / ev:.2e}")


if __name__ == "__main__":
    main()