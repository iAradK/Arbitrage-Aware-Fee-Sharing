#!/usr/bin/env python3
"""E6 exactness checks with fragments as separate transactions in one block (block-scoped hook).

Same opportunities, paths and integer conversion as experiments/e6_fragmentation.py (same config, seed and selection
order), but every fragment is its own transaction of one block and is charged by the block-scoped integer reference
(common.fixedpoint.ScopedHookReference). Fragment j enters as the value delta A_j - A_{j-1} in the numeraire, as in the
E7 vectors. Checks, all expected 0:
  sep_tx_vs_in_tx  per-fragment charges differ from the in-transaction watermark (fixedpoint.cumulative_charges)
  mono_exact_fail  monotone equal split whose charges do not sum to F(A_n)
  opt_exact_fail   the same for the adversarial split against the independent rule
  rev_max_fail     reversal path whose charges do not sum to max_j F(A_j)
  negative_charge  a fragment with a negative charge
  tx_scope_fail    the transaction-scoped baseline, same transactions, does not charge sum_j F(a_j)
The transaction-scoped baseline's take relative to the block scope is reported for equal splits.
Writes results/e6/tables/e6_block_scope_checks_<split>.csv and manifest_block_scope_<split>.json only.

  python experiments/e6_block_scope_check.py --split valid
  python experiments/e6_block_scope_check.py --split test --confirm-frozen
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "contracts" / "python"))
sys.path.insert(0, str(ROOT / "experiments"))

from common import cpmm, fixedpoint as fp, minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS  # noqa: E402
from fragmentation_replay import adversarial_partition  # noqa: E402
import e6_fragmentation as e6  # noqa: E402

BLOCK = 1


def bps(x: float) -> int:
    b = round(x * 10_000)
    if b * fp.BPS_TO_WAD != fp.to_wad(x):
        raise SystemExit(f"{x} is not on the basis-point grid the hook stores")
    return b


def fragment_deltas(A_w: list[int]) -> list[int]:
    return [A_w[0]] + [A_w[j] - A_w[j - 1] for j in range(1, len(A_w))]


def charge_separate_txs(A_w: list[int], K_w: int, d_w: int, lb: int, gb: int, scope: str) -> list[int]:
    h = fp.ScopedHookReference(K_w, d_w, lb, gb, scope=scope)
    return [h.swap("pool", BLOCK, j, d, 0, fp.WAD) for j, d in enumerate(fragment_deltas(A_w))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(e6.CFG))
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    ap.add_argument("--confirm-frozen", action="store_true")
    a = ap.parse_args()
    cfg = reporting.load_config(a.config)
    reporting.guard_split("e6", a.split, a.confirm_frozen, cfg)
    out = reporting.out_dir("e6")
    rng = np.random.default_rng(cfg["seed"])
    lam, gam, delta = fp.to_wad(cfg["lam"]), fp.to_wad(cfg["gamma"]), fp.to_wad(cfg["delta_usd"])
    lb, gb = bps(cfg["lam"]), bps(cfg["gamma"])
    rows = []
    for key in cfg["pools"]:
        pool = POOLS[key]
        for variant in cfg["variants"][key]:
            R_usd = mg.r_regimes_usd(key, variant, cfg["gas_units"], cfg["r_quantiles"])[cfg["r_regime"]]
            opps, info = e6.select_opportunities(key, variant, a.split, cfg, R_usd, rng)
            print(key, variant, info, flush=True)
            for n in cfg["partitions"]:
                c = {"n_opps": 0, "sep_tx_vs_in_tx": 0, "mono_exact_fail": 0, "opt_exact_fail": 0, "rev_max_fail": 0,
                     "negative_charge": 0, "tx_scope_fail": 0, "n_opt": 0, "n_rev": 0}
                ratio = []
                for o in opps.itertuples():
                    usd = o.usd_per_num
                    K_w = fp.to_wad((o.C + o.R) * usd)
                    F = lambda x: fp.transfer_wad(max(x, 0), K_w, lam, gam, delta)[0]  # noqa: E731
                    A_eq = e6.path_prefix_surplus(o, np.arange(1, n + 1) / n, pool) * usd
                    paths = [("eq", [fp.to_wad(v) for v in A_eq])]
                    if n >= 2 and A_eq[-1] >= n * cfg["min_fragment_usd"]:      # adversarial split, as in E6
                        parts, _ = adversarial_partition(float(A_eq[-1]), n, (o.C + o.R) * usd, cfg["lam"], cfg["gamma"],
                                                         cfg["delta_usd"], cfg["min_fragment_usd"])
                        cum = np.cumsum(parts)
                        tgt = np.minimum(cum / usd, cum[-1] / usd)
                        nn = np.array([float(cpmm.smallest_net_for_surplus(o.x, o.y, pool.fee, o.dirn, o.n_used, o.pi, t))
                                       for t in tgt])
                        paths.append(("opt", [fp.to_wad(v) for v in e6.path_prefix_surplus(o, nn / o.n_used, pool) * usd]))
                    if n >= 2:
                        paths.append(("rev", [fp.to_wad(v) for v in e6.reversal_prefix_surplus(o, n, pool) * usd]))
                    c["n_opps"] += 1
                    for kind, A_w in paths:
                        block = charge_separate_txs(A_w, K_w, delta, lb, gb, "block")
                        in_tx = fp.cumulative_charges(A_w, K_w, lam, gam, delta)
                        if block != [r["charge"] for r in in_tx]:
                            c["sep_tx_vs_in_tx"] += 1
                        if any(r < 0 for r in block):
                            c["negative_charge"] += 1
                        monotone = all(A_w[i] <= A_w[i + 1] for i in range(len(A_w) - 1))
                        if kind == "rev":
                            c["n_rev"] += 1
                            if sum(block) != max(r["target"] for r in in_tx):
                                c["rev_max_fail"] += 1
                        elif monotone and sum(block) != F(A_w[-1]):
                            c["opt_exact_fail" if kind == "opt" else "mono_exact_fail"] += 1
                        c["n_opt"] += kind == "opt"
                        txs = charge_separate_txs(A_w, K_w, delta, lb, gb, "tx")
                        if sum(txs) != sum(F(d) for d in fragment_deltas(A_w)):
                            c["tx_scope_fail"] += 1
                        if kind == "eq" and sum(block) > 0:
                            ratio.append(sum(txs) / sum(block))
                rows.append({"pool": key, "variant": variant, "n_fragments": n, **c,
                             "tx_scoped_over_block_scoped_equal": float(np.mean(ratio)) if ratio else np.nan})
    res = pd.DataFrame(rows)
    reporting.write_table(res, out / "tables" / f"e6_block_scope_checks_{a.split}", {"tx_scoped_over_block_scoped_equal": "{:.3f}"})
    reporting.write_manifest("e6", cfg, [CACHE / "aligned" / f"{k}.parquet" for k in cfg["pools"]], a.split,
                             {"check": "block-scoped exactness, fragments as separate transactions in one block"},
                             tag=f"block_scope_{a.split}", latest=False)
    fails = ["sep_tx_vs_in_tx", "mono_exact_fail", "opt_exact_fail", "rev_max_fail", "negative_charge", "tx_scope_fail"]
    print("VIOLATION COUNTS (expected 0):", res[fails].sum().to_dict())
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(res.to_string(index=False))


if __name__ == "__main__":
    main()
