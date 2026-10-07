#!/usr/bin/env python3
"""Conformance vectors for the final hook (block scope V1 with per-transaction clipping and the relative proportional
buffer): the 48 E7 multi-swap sequences (validation months, results/e7/vectors.json) in three modes, expected marginal
charges from common.fixedpoint.ScopedHookReference(scope="block", accumulation="tx_clip", buffer="rel"):
  a  all fragments in one transaction of one block
  b  every fragment its own transaction, all in one block
  c  every fragment its own transaction, over 2-4 consecutive blocks (contiguous groups, random cut points)
The E7 fragments are numeraire values v; to exercise the buffer every fragment becomes a two-token swap
(delta0, delta1) = (-2v, +3v) at a reference of 1.2 (so the product truncation also matters), with gross token1
volume 3|v|. eps_rel = 2,830,000 ppb (0.283%), settlement in token0. Writes contracts/test/hooks/vectors/vectors_v1.json (tracked: BlockScopeConformanceTest reads it).

  python experiments/e7_v1_vectors.py
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import fixedpoint as fp  # noqa: E402
from common.pools import RESULTS  # noqa: E402

SEED = 20261007
WAD = fp.WAD
REF = 12 * 10**17
EPS_PPB = 2_830_000
OUT = ROOT / "contracts" / "test" / "hooks" / "vectors"   # tracked: the conformance test's input


def run(seq, d0, d1, blocks, txs):
    lam, gam = int(seq["lambda_wad"]) // fp.BPS_TO_WAD, int(seq["gamma_wad"]) // fp.BPS_TO_WAD
    h = fp.ScopedHookReference(int(seq["k_hat_wad"]) + int(seq["delta_wad"]), 0, lam, gam, scope="block",
                               accumulation="tx_clip", buffer="rel", eps_wad=EPS_PPB * 10**9)
    return [h.swap("p", b, t, a0, a1, oracle_price_wad=REF) for a0, a1, b, t in zip(d0, d1, blocks, txs)]


def main():
    seqs = json.load(open(RESULTS / "e7" / "vectors.json"))["sequences"]
    rng = random.Random(SEED)
    out = []
    for i, s in enumerate(seqs):
        v = [int(x) for x in s["deltas"]]
        d0, d1 = [-2 * x for x in v], [3 * x for x in v]
        n = len(v)
        m = rng.randint(2, min(4, n))
        cuts = sorted(rng.sample(range(1, n), m - 1))
        blocks_c, b = [], 0
        for j in range(n):
            if b < len(cuts) and j == cuts[b]:
                b += 1
            blocks_c.append(b)
        ea = run(s, d0, d1, [0] * n, [0] * n)
        eb = run(s, d0, d1, [0] * n, list(range(n)))
        ec = run(s, d0, d1, blocks_c, list(range(n)))
        out.append({"index": i, "kind": s["kind"], "n": n, "k_hat_wad": str(int(s["k_hat_wad"]) + int(s["delta_wad"])),
                    "lambda_bps": int(s["lambda_wad"]) // fp.BPS_TO_WAD, "gamma_bps": int(s["gamma_wad"]) // fp.BPS_TO_WAD,
                    "d0": [str(x) for x in d0], "d1": [str(x) for x in d1], "blocks_c": blocks_c,
                    "expected_a": [str(x) for x in ea], "expected_b": [str(x) for x in eb], "expected_c": [str(x) for x in ec]})
    OUT.mkdir(parents=True, exist_ok=True)
    doc = {"source": "results/e7/vectors.json (sequences, validation months)", "seed": SEED, "reference_wad": str(REF),
           "eps_rel_ppb": EPS_PPB, "accumulation": "tx_clip", "buffer": "rel", "n_sequences": len(out),
           "n_fragments": sum(x["n"] for x in out), "sequences": out}
    (OUT / "vectors_v1.json").write_text(json.dumps(doc, indent=1))
    diff = lambda a, b: sum(1 for x in out for p, q in zip(x[a], x[b]) if p != q)  # noqa: E731
    charged = lambda a: sum(1 for x in out for p in x[a] if p != "0")  # noqa: E731
    print(f"{len(out)} sequences, {doc['n_fragments']} fragments; charged a/b/c {charged('expected_a')}/{charged('expected_b')}/"
          f"{charged('expected_c')}; fragments differing a vs b {diff('expected_a', 'expected_b')}, b vs c {diff('expected_b', 'expected_c')}")


if __name__ == "__main__":
    main()
