#!/usr/bin/env python3
"""
Download all Uniswap v4 swap events for one pool over a fixed UTC date range.

Example:
    python fetch_v4_swaps.py \
      --endpoint "https://gateway.thegraph.com/api/YOUR_API_KEY/subgraphs/id/YOUR_SUBGRAPH_ID" \
      --pool-id "0xe500210c7ea6bfd9f69dce044b09ef384ec2b34832f132baec3b418208e3a657" \
      --start 2025-07-01 \
      --end 2025-08-01 \
      --out swaps_2025_07.csv

The end date is exclusive. Thus, --start 2025-07-01 --end 2025-08-01
retrieves the entire month of July 2025 in UTC.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


QUERY = """
query SwapsPage(
  $poolId: String!
  $startTimestamp: BigInt!
  $endTimestamp: BigInt!
  $lastId: String!
  $pageSize: Int!
) {
  swaps(
    first: $pageSize
    orderBy: id
    orderDirection: asc
    where: {
      pool: $poolId
      timestamp_gte: $startTimestamp
      timestamp_lt: $endTimestamp
      id_gt: $lastId
    }
  ) {
    id
    timestamp
    sender
    origin
    amount0
    amount1
    amountUSD
    sqrtPriceX96
    tick
    logIndex
    transaction {
      id
      blockNumber
      timestamp
      gasUsed
      gasPrice
    }
  }
}
"""


CSV_FIELDS = [
    "swap_id",
    "timestamp",
    "datetime_utc",
    "sender",
    "origin",
    "amount0",
    "amount1",
    "amountUSD",
    "sqrtPriceX96",
    "tick",
    "logIndex",
    "transaction_hash",
    "blockNumber",
    "transaction_timestamp",
    "transaction_datetime_utc",
    "gasUsed",
    "gasPrice",
]


def parse_utc_date(value: str) -> int:
    """Parse YYYY-MM-DD or an ISO-8601 datetime and return a UTC Unix timestamp."""
    text = value.strip()

    try:
        if len(text) == 10:
            dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date/time {value!r}. Use YYYY-MM-DD or ISO-8601."
        ) from exc

    return int(dt.timestamp())


def utc_iso(timestamp: str | int | None) -> str:
    if timestamp in (None, ""):
        return ""
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()


def graphql_request(
    session: requests.Session,
    endpoint: str,
    variables: dict[str, Any],
    timeout: int,
    max_retries: int,
) -> list[dict[str, Any]]:
    """Run one GraphQL page request with exponential-backoff retries."""
    delay = 2.0

    for attempt in range(1, max_retries + 1):
        try:
            response = session.post(
                endpoint,
                json={"query": QUERY, "variables": variables},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == max_retries:
                raise RuntimeError(f"Request failed after {max_retries} attempts: {exc}") from exc
            print(
                f"Request error on attempt {attempt}/{max_retries}: {exc}. "
                f"Retrying in {delay:.0f}s...",
                file=sys.stderr,
            )
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue

        errors = payload.get("errors")
        if errors:
            message = json.dumps(errors, ensure_ascii=False)

            # Retry transient gateway/indexer failures, but fail fast for a bad query.
            transient_markers = (
                "too far behind",
                "indexer not available",
                "Unavailable",
                "timeout",
                "temporarily",
                "Bad Gateway",
                "Service Unavailable",
            )
            is_transient = any(marker.lower() in message.lower() for marker in transient_markers)

            if is_transient and attempt < max_retries:
                print(
                    f"GraphQL/indexer error on attempt {attempt}/{max_retries}: "
                    f"{message}. Retrying in {delay:.0f}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue

            raise RuntimeError(f"GraphQL returned errors: {message}")

        data = payload.get("data")
        if not isinstance(data, dict) or "swaps" not in data:
            raise RuntimeError(f"Unexpected GraphQL response: {payload}")

        swaps = data["swaps"]
        if not isinstance(swaps, list):
            raise RuntimeError(f"Unexpected swaps value: {swaps!r}")

        return swaps

    raise RuntimeError("Unreachable retry state")


def flatten_swap(swap: dict[str, Any]) -> dict[str, Any]:
    tx = swap.get("transaction") or {}

    return {
        "swap_id": swap.get("id", ""),
        "timestamp": swap.get("timestamp", ""),
        "datetime_utc": utc_iso(swap.get("timestamp")),
        "sender": swap.get("sender", ""),
        "origin": swap.get("origin", ""),
        "amount0": swap.get("amount0", ""),
        "amount1": swap.get("amount1", ""),
        "amountUSD": swap.get("amountUSD", ""),
        "sqrtPriceX96": swap.get("sqrtPriceX96", ""),
        "tick": swap.get("tick", ""),
        "logIndex": swap.get("logIndex", ""),
        "transaction_hash": tx.get("id", ""),
        "blockNumber": tx.get("blockNumber", ""),
        "transaction_timestamp": tx.get("timestamp", ""),
        "transaction_datetime_utc": utc_iso(tx.get("timestamp")),
        "gasUsed": tx.get("gasUsed", ""),
        "gasPrice": tx.get("gasPrice", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download all Uniswap v4 swaps for a pool and UTC date range."
    )
    parser.add_argument("--endpoint", required=True, help="The Graph GraphQL endpoint URL.")
    parser.add_argument("--pool-id", required=True, help="Uniswap v4 pool ID.")
    parser.add_argument(
        "--start",
        required=True,
        type=parse_utc_date,
        help="Inclusive UTC start date/time, e.g. 2025-07-01.",
    )
    parser.add_argument(
        "--end",
        required=True,
        type=parse_utc_date,
        help="Exclusive UTC end date/time, e.g. 2025-08-01.",
    )
    parser.add_argument("--out", required=True, help="Output CSV filename.")
    parser.add_argument(
        "--jsonl",
        help="Optional JSON Lines output preserving the original nested records.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=1000,
        help="GraphQL page size, maximum 1000. Default: 1000.",
    )
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds.")
    parser.add_argument("--max-retries", type=int, default=8, help="Retries per page.")

    args = parser.parse_args()

    if not 1 <= args.page_size <= 1000:
        parser.error("--page-size must be between 1 and 1000.")
    if args.end <= args.start:
        parser.error("--end must be later than --start.")

    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    jsonl_path = Path(args.jsonl) if args.jsonl else None
    if jsonl_path:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "uniswap-v4-swap-downloader/1.0",
        }
    )

    total = 0
    page_number = 0
    last_id = ""

    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        writer.writeheader()

        jsonl_file = (
            jsonl_path.open("w", encoding="utf-8") if jsonl_path else None
        )

        try:
            while True:
                page_number += 1
                variables = {
                    "poolId": args.pool_id.lower(),
                    # BigInt variables are safest as JSON strings.
                    "startTimestamp": str(args.start),
                    "endTimestamp": str(args.end),
                    "lastId": last_id,
                    "pageSize": args.page_size,
                }

                swaps = graphql_request(
                    session=session,
                    endpoint=args.endpoint,
                    variables=variables,
                    timeout=args.timeout,
                    max_retries=args.max_retries,
                )

                if not swaps:
                    break

                for swap in swaps:
                    writer.writerow(flatten_swap(swap))
                    if jsonl_file:
                        jsonl_file.write(json.dumps(swap, ensure_ascii=False) + "\n")

                total += len(swaps)
                new_last_id = swaps[-1]["id"]

                if new_last_id == last_id:
                    raise RuntimeError(
                        "Pagination cursor did not advance; aborting to avoid an infinite loop."
                    )

                last_id = new_last_id
                print(
                    f"Page {page_number}: fetched {len(swaps):,} swaps; "
                    f"total {total:,}; last_id={last_id}",
                    file=sys.stderr,
                )

                if len(swaps) < args.page_size:
                    break

        finally:
            if jsonl_file:
                jsonl_file.close()

    print(f"Wrote {total:,} swaps to {output_path}")
    if jsonl_path:
        print(f"Wrote original records to {jsonl_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
