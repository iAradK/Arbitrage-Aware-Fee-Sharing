#!/usr/bin/env python3
"""E8 (Phase 2 item 2.4): an MEV tax on the priority fees that arbitrage swaps actually paid. No replay.

The replayed MEV tax (e8_baselines.py, family `mevtax`) assumes competitive priority bidding: searchers bid their whole
margin as priority fee, so a tax of t times the priority fee collects t/(1+t) of the margin. This script asks what the
tax base looks like on Ethereum mainnet, where blocks are not ordered by priority fee and searchers can pay builders
outside the gas price (direct transfers, not visible in receipts). The main quantity is the observed priority payment as a
share of the arbitrage surplus: the competitive assumption needs it near the whole margin. A static t x observed payment is
not reported, since searchers would move their payments out of the priority fee once it is taxed.

Arbitrage swaps: observed swaps of the split that start outside the fee band of the benchmark (|pre-swap deviation| >=
gap_multiple x fee, primary variant's trailing offset applied) and end closer to it. Per swap, the priority payment of its
transaction is (effective gas price - base fee) x gas used (BigQuery receipts, data/data/bq/swap_logs_decoded.parquet),
and the tax is t times that payment, in USD at the minute's ETH price. A transaction with several v4 swaps is taxed once
per swap here; such transactions often pay large tips for other activity, so the `all` rows are not interpretable and are
kept only for completeness. `single_leg` rows (one v4 swap in the transaction) are the measurement. The
swaps' own surplus S at the benchmark is reported for scale.

  python experiments/e8_mev_tax_observed.py --split test
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS  # noqa: E402

PRIMARY = {"eth_usdc_005": ("raw", None), "eth_wbtc_030": ("corr24h", "24h"), "eth_wsteth_001": ("corr24h", "24h")}
BQ = ROOT / "data" / "data" / "bq" / "swap_logs_decoded.parquet"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["valid", "test"], default="test")
    a = ap.parse_args()
    out = reporting.out_dir("e8")
    bq = pd.read_parquet(BQ, columns=["transaction_hash", "log_index", "pool", "receipt_gas_used", "receipt_effective_gas_price",
                                      "base_fee_per_gas", "single_leg", "n_swap_logs", "fee"])
    bq["tip_wei"] = (bq["receipt_effective_gas_price"].astype(float) - bq["base_fee_per_gas"].astype(float)).clip(lower=0)
    bq["prio_eth"] = bq["tip_wei"] * bq["receipt_gas_used"].astype(float) * 1e-18
    rows = []
    for key, (variant, w) in PRIMARY.items():
        pool = POOLS[key]
        al = mg.load_aligned(key)
        al = al[al["split"] == a.split]
        off = np.nan_to_num(al[f"offset_{w}"].to_numpy(), nan=0.0) if w else 0.0
        pi = al["p_ref"].to_numpy() * np.exp(off)
        pre, post = np.log(al["price_pre"].to_numpy() / pi), np.log(al["price"].to_numpy() / pi)
        arb = (np.abs(pre) >= pool.gap_multiple * pool.fee) & (np.abs(post) < np.abs(pre))
        S = -(pi * al["amount0"].to_numpy() + al["amount1"].to_numpy()) * al["usd_per_num"].to_numpy()
        sw = pd.DataFrame({"tx_hash": al["tx_hash"].str.lower().to_numpy(), "log_index": al["log_index"].astype(int).to_numpy(),
                           "arb": arb, "S_usd": S, "eth_usd": al["eth_usd"].to_numpy()})
        b = bq[bq["pool"] == key].assign(tx_hash=lambda x: x["transaction_hash"].str.lower(), log_index=lambda x: x["log_index"].astype(int))
        m = sw.merge(b, on=["tx_hash", "log_index"], how="left", validate="one_to_one")
        joined = m["prio_eth"].notna()
        assert (m.loc[joined, "fee"] == round(pool.fee * 1e6)).all(), "swap fee differs from the LP fee (protocol fee in the split?)"
        m["prio_usd"] = m["prio_eth"] * m["eth_usd"]
        for scope, sel in [("all", m["arb"] & joined), ("single_leg", m["arb"] & joined & (m["single_leg"] == True))]:  # noqa: E712
            x = m[sel]
            row = {"pool": key, "variant": variant, "split": a.split, "scope": scope, "n_arb_swaps": int(sel.sum()),
                   "join_rate": float(joined[m["arb"]].mean()), "zero_tip_share": float((x["tip_wei"] <= 0).mean()),
                   "median_tip_gwei": float(x["tip_wei"].median() * 1e-9), "priority_paid_usd": float(x["prio_usd"].sum()),
                   "arb_surplus_usd": float(x["S_usd"].clip(lower=0).sum())}
            row["priority_over_surplus"] = row["priority_paid_usd"] / row["arb_surplus_usd"] if row["arb_surplus_usd"] > 0 else np.nan
            row["zero_tip_surplus_share"] = float(x.loc[x["tip_wei"] <= 0, "S_usd"].clip(lower=0).sum()) / row["arb_surplus_usd"]
            rows.append(row)
    res = pd.DataFrame(rows)
    fmt = {c: "{:,.2f}" for c in res.columns if c.endswith("usd")}
    fmt.update({c: "{:.4f}" for c in ("join_rate", "zero_tip_share", "median_tip_gwei", "priority_over_surplus", "zero_tip_surplus_share")})
    reporting.write_table(res, out / "tables" / f"e8_mev_tax_observed_{a.split}", fmt)
    reporting.write_manifest("e8", {"analysis": "mev tax on observed bids"}, [BQ] + [CACHE / "aligned" / f"{k}.parquet" for k in PRIMARY],
                             a.split, tag=f"mev_tax_observed_{a.split}", latest=False)
    with pd.option_context("display.width", 250):
        print(res.T.to_string())


if __name__ == "__main__":
    main()
