#!/usr/bin/env python3
"""Block-scope conformance vectors: the 48 E7 multi-swap sequences (validation months, results/e7/vectors.json) run in
three modes, with expected charges from the block-scoped integer reference (common.fixedpoint.ScopedHookReference):
  a  all fragments in one transaction of one block
  b  every fragment its own transaction, all in one block
  c  every fragment its own transaction, spread over 2-4 consecutive blocks (contiguous groups, random cut points)
Modes a and b must reproduce the E7 expected charges (in-transaction watermark). Fragments enter as token0
(numeraire) deltas with reference 1.0 and settle in token0, as in E7. Writes results/e7_block_scope/vectors.json only;
contracts/test/hooks/BlockScopeConformance.t.sol replays it against ParticipationAwareHook.

  python experiments/e7_block_scope_vectors.py
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

SEED = 20261006
WAD = fp.WAD
OUT = RESULTS / "e7_block_scope"


def run(seq: dict, blocks: list[int], txs: list[int]) -> list[int]:
    lam, gam = int(seq["lambda_wad"]) // fp.BPS_TO_WAD, int(seq["gamma_wad"]) // fp.BPS_TO_WAD
    h = fp.ScopedHookReference(int(seq["k_hat_wad"]), int(seq["delta_wad"]), lam, gam)
    return [h.swap("p", b, t, int(d), 0, oracle_price_wad=WAD) for d, b, t in zip(seq["deltas"], blocks, txs)]


def main():
    src = RESULTS / "e7" / "vectors.json"
    seqs = json.load(open(src))["sequences"]
    rng = random.Random(SEED)
    out = []
    for i, s in enumerate(seqs):
        for f in ("lambda_wad", "gamma_wad"):
            assert int(s[f]) % fp.BPS_TO_WAD == 0, f"{f} is not a whole number of basis points"
        n = len(s["deltas"])
        m = rng.randint(2, min(4, n))                                  # number of blocks in mode c
        cuts = sorted(rng.sample(range(1, n), m - 1))
        blocks_c, b = [], 0
        for j in range(n):
            if b < len(cuts) and j == cuts[b]:
                b += 1
            blocks_c.append(b)
        ea = run(s, [0] * n, [0] * n)
        eb = run(s, [0] * n, list(range(n)))
        ec = run(s, blocks_c, list(range(n)))
        e7 = [int(x) for x in s["expected_charges"]]
        assert ea == e7 and eb == e7, f"sequence {i}: one-block modes differ from the E7 expected charges"
        assert sum(ec) >= 0 and all(x >= 0 for x in ec)
        out.append({"index": i, "pool": s["pool"], "kind": s["kind"], "n": n, "k_hat_wad": s["k_hat_wad"],
                    "delta_wad": s["delta_wad"], "lambda_bps": int(s["lambda_wad"]) // fp.BPS_TO_WAD,
                    "gamma_bps": int(s["gamma_wad"]) // fp.BPS_TO_WAD, "deltas": s["deltas"], "blocks_c": blocks_c,
                    "n_blocks_c": m, "expected_a": [str(x) for x in ea], "expected_b": [str(x) for x in eb],
                    "expected_c": [str(x) for x in ec]})
    OUT.mkdir(parents=True, exist_ok=True)
    doc = {"source": "results/e7/vectors.json (sequences, validation months)", "seed": SEED, "n_sequences": len(out),
           "n_fragments": sum(x["n"] for x in out), "sequences": out}
    (OUT / "vectors.json").write_text(json.dumps(doc, indent=1))
    nc = sum(1 for x in out for a, c in zip(x["expected_a"], x["expected_c"]) if a != c)
    print(f"{len(out)} sequences, {doc['n_fragments']} fragments; mode c differs from one block in {nc} fragments; "
          f"blocks in mode c: {sorted({x['n_blocks_c'] for x in out})}")


if __name__ == "__main__":
    main()
