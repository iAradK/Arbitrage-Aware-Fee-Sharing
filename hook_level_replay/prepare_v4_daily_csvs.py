#!/usr/bin/env python3
"""Convert The Graph Uniswap v4 poolDayDatas JSON into CSV inputs for run_lp_outcome_daily.py."""
import argparse, json
from pathlib import Path
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json-file', required=True)
    ap.add_argument('--out-dir', default='.')
    ap.add_argument('--fixed-depth-y', type=float, default=1_000_000.0)
    ap.add_argument('--price-field', default='token0Price')
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with open(args.json_file, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    rows = raw.get('data', {}).get('poolDayDatas', raw.get('poolDayDatas', raw if isinstance(raw, list) else []))
    if not rows:
        raise SystemExit('Could not find data.poolDayDatas in JSON')
    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(pd.to_numeric(df['date']), unit='s', utc=True).dt.date.astype(str)
    for col in ['liquidity','sqrtPrice','tick','token0Price','token1Price','volumeToken0','volumeToken1','volumeUSD','feesUSD','txCount']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.sort_values('date').reset_index(drop=True)
    if args.price_field not in df.columns:
        raise SystemExit(f'Missing price field: {args.price_field}')
    pool = pd.DataFrame({
        'date': df['date'],
        'pool_price': df[args.price_field].astype(float),
        'depth_y': float(args.fixed_depth_y),
    })
    # keep optional metadata if present
    for col in ['liquidity','volumeUSD','feesUSD','txCount']:
        if col in df.columns:
            pool[col] = df[col]
    price = pd.DataFrame({
        'date': df['date'],
        'external_price': df[args.price_field].shift(-1).astype(float),
    }).dropna()
    # Drop last pool day to match next-day reference availability
    pool = pool.iloc[:-1].copy()
    pool_path = out / 'v4_pool_daily_cpmm.csv'
    price_path = out / 'v4_nextday_price.csv'
    pool.to_csv(pool_path, index=False)
    price.to_csv(price_path, index=False)
    print(f'Wrote {pool_path} ({len(pool)} rows)')
    print(f'Wrote {price_path} ({len(price)} rows)')

if __name__ == '__main__':
    main()
