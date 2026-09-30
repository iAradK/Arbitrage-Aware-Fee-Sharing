"""Daily fees, TVL and volume of the four pools for the test months (external review 1.1(d)).

Queries the Uniswap v4 subgraph (Ethereum mainnet) on The Graph for poolDayDatas between 2026-07-01 and 2026-08-31
(62 UTC days), plus each pool's fee tier and hook address. The field names are first checked against the schema by
introspection. Everything needed to reproduce the fetch is saved next to the raw responses: the fetch time, the
subgraph ID, the endpoint with the key redacted and the exact query texts.

The API key is read from the GRAPH_API_KEY environment variable and is never written to disk.

Usage (from the repo root):
  set GRAPH_API_KEY=...        (PowerShell: $env:GRAPH_API_KEY = "...")
  .venv/Scripts/python.exe data/fetch_pool_day_data.py
Then run experiments/fees_tvl_check.py (offline).
"""
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.pools import POOLS  # noqa: E402

SUBGRAPH_ID = "DiYPVdygkfjDWhbxGSqAQxwBKmfKnkWQojqeM2rkLb3G"  # Uniswap v4, Ethereum mainnet (same as fetch_swaps.py)
ENDPOINT = "https://gateway.thegraph.com/api/{key}/subgraphs/id/" + SUBGRAPH_ID
START, END = "2026-07-01", "2026-09-01"                        # inclusive, exclusive (UTC days)
OUT = ROOT / "data" / "data" / "subgraph" / "pool_day_data_jul_aug_2026.json"

DAY_FIELDS = ["date", "feesUSD", "tvlUSD", "volumeUSD", "txCount"]
POOL_FIELDS = ["id", "feeTier", "hooks", "tickSpacing", "totalValueLockedUSD"]
REQUIRED_DAY = {"date", "feesUSD", "tvlUSD", "volumeUSD"}
Q_SCHEMA = """{
  poolDayData: __type(name: "PoolDayData") { fields { name } }
  pool: __type(name: "Pool") { fields { name } }
}"""


def ts(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def post(url: str, query: str) -> dict:
    for attempt in range(4):
        try:
            r = requests.post(url, json={"query": query}, timeout=60)
            r.raise_for_status()
            body = r.json()
            if "errors" in body:
                raise SystemExit(f"GraphQL error: {json.dumps(body['errors'])[:500]}\nquery:\n{query}")
            return body["data"]
        except requests.RequestException as e:
            if attempt == 3:
                raise SystemExit(f"request failed after 4 attempts: {type(e).__name__}")   # no URL: it contains the key
            time.sleep(2 ** attempt)


def main():
    key = os.environ.get("GRAPH_API_KEY", "").strip()
    if not key:
        raise SystemExit("set GRAPH_API_KEY to your The Graph API key first (it is not stored anywhere)")
    url = ENDPOINT.format(key=key)
    t0, t1 = ts(START), ts(END)

    schema = post(url, Q_SCHEMA)
    day_have = {f["name"] for f in schema["poolDayData"]["fields"]}
    pool_have = {f["name"] for f in schema["pool"]["fields"]}
    missing = REQUIRED_DAY - day_have
    if missing:
        raise SystemExit(f"PoolDayData lacks {sorted(missing)}. Available fields: {sorted(day_have)}")
    day_fields = [f for f in DAY_FIELDS if f in day_have]
    pool_fields = [f for f in POOL_FIELDS if f in pool_have]

    queries, pools = {}, {}
    for k, p in POOLS.items():
        pid = p.pool_id.lower()
        q_pool = f'{{ pool(id: "{pid}") {{ {" ".join(pool_fields)} }} }}'
        q_days = (f'{{ poolDayDatas(first: 100, orderBy: date, orderDirection: asc, '
                  f'where: {{pool: "{pid}", date_gte: {t0}, date_lt: {t1}}}) {{ {" ".join(day_fields)} }} }}')
        queries[k] = {"pool": q_pool, "poolDayDatas": q_days}
        pools[k] = {"pool_id": pid, "pool": post(url, q_pool)["pool"], "poolDayDatas": post(url, q_days)["poolDayDatas"]}
        print(f"{k}: {len(pools[k]['poolDayDatas'])} days, feeTier {pools[k]['pool'].get('feeTier') if pools[k]['pool'] else 'pool not found'}")

    rec = {"fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "subgraph_id": SUBGRAPH_ID, "endpoint": ENDPOINT.format(key="<KEY>"),
           "date_range_utc": {"start_inclusive": START, "end_exclusive": END, "date_gte": t0, "date_lt": t1},
           "schema_query": Q_SCHEMA, "schema_fields": {"PoolDayData": sorted(day_have), "Pool": sorted(pool_have)},
           "queries": queries, "pools": pools}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rec, indent=1))
    assert key not in OUT.read_text()
    print(f"saved {OUT.relative_to(ROOT)}  sha256 {hashlib.sha256(OUT.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
