#!/usr/bin/env python3
"""Experiment A follow-up 2: strategic splitting under the block-scoped hook (post-processing of results/eA only; no
replay or simulation is executed, no test-month simulation).

1. Within one block. Every executed correction of the two E2 rules (ideal = retained k=1 d=0, buffered = delta=eps_S
   k=15 d=1), ETH/USDC and ETH/WBTC (raw and offset), is split into l = 2..cap_b transactions of its own block,
   cap_b = floor(gas_limit_b / 185,180) from eA_fu_capacity_corrections.csv.gz. Two split families for every l:
     (i)  Experiment A's transaction-scope optimum: l - 1 pieces of kappa - 1 unit plus the remainder (l up to
          ceil(a / piece), where the remainder is still positive),
     (ii) l equal pieces (floor(a / l) WAD each, the remainder on the last piece).
   Each piece is fed as its own transaction into common/fixedpoint.ScopedHookReference(scope="block"), the integer
   mirror of contracts/src/hooks/ParticipationAwareHook.sol, in Experiment A's convention: USD amounts in WAD,
   kappa = to_wad(kappa_usd), lambda / gamma in basis points, the piece's surplus as a numeraire delta (d1 = 0,
   reference = WAD). The block total is compared with F(a) in WAD units (1e-18 USD). Pool rounding of per-swap
   deltas is not in the saved files and is therefore not modelled. Check: the same pieces under scope="tx" reproduce
   Experiment A's charge(l*) exactly.
   Optimal l with gas: saving(l) = F(a) - total(l) - (l-1) c_tx + settle (1[F(a) > 0] - #pieces with r_j > 0).
2. Cross-block bracket. One piece per block, constant reference and kappa across blocks (P4 relaxes this): then each
   block is its own scope and the split is Experiment A's transaction-scope split, capped at l <= 5 and l <= 75
   blocks (one minute and 15 minutes). Both rules, every pool.
3. Every share is labelled "sum-weighted" (sum of charges / sum of F(a)) or "mean of per-correction ratios" (mean of
   charge / F(a) over corrections with F(a) > 0).

  python experiments/eA_followup2.py
"""
from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

import eA_followup as fu  # noqa: E402
import eA_strategic_split as eA  # noqa: E402
from common import fixedpoint as fp, reporting  # noqa: E402

OUT = eA.OUT
WAD = fp.WAD
RULES = {"ideal_retained_k1_d0": "ideal", "buffered_1eps_k15_d1": "buffered"}
PART1_POOLS = [("eth_usdc_005", "raw"), ("eth_wbtc_030", "raw"), ("eth_wbtc_030", "corr24h")]
CROSS_CAPS = {"l<=5 blocks": 5, "l<=75 blocks": 75}
LAM_BPS, GAM_BPS = 7500, 200                     # E2 headline, as eA.Fint(0.75, 0.02)
POOL = "p"


def run_split(h: fp.ScopedHookReference, block: int, pieces: list[int]) -> tuple[int, int]:
    """Feed the pieces as transactions 0..l-1 of one block; total charge (WAD) and number of charged pieces."""
    tot = nch = 0
    for j, p in enumerate(pieces):
        r = h.swap(POOL, block, j, p, 0, WAD)
        tot += r
        nch += r > 0
    return tot, nch


def within_block(row: tuple) -> dict:
    """All splits of one correction into l <= cap transactions of one block."""
    a_usd, k_usd, u_usd, ctx, st, cap, l_star_tx, ch_star_tx = row
    a_w, k_w = fp.to_wad(max(a_usd, 0.0)), fp.to_wad(k_usd)
    piece = k_w - max(1, fp.to_wad(u_usd))
    h = fp.ScopedHookReference(k_w, 0, LAM_BPS, GAM_BPS, scope="block")
    F_a, _ = run_split(h, 0, [a_w])
    assert F_a == eA.Fint(0.75, 0.02)(a_w, k_w)
    blk, n_splits, n_pieces, dmin, dmax, n_nz = 1, 0, 0, 0, 0, 0
    best_sav, l_best, ch_best, min_tot = 0.0, 1, F_a, F_a
    fams = {"i": min(cap, -(-a_w // piece)) if a_w > 0 else 1, "ii": cap}
    for fam, lmax in fams.items():
        for l in range(2, lmax + 1):
            if fam == "i":
                pieces = [piece] * (l - 1) + [a_w - (l - 1) * piece]
            else:
                q = a_w // l
                pieces = [q] * (l - 1) + [a_w - (l - 1) * q]
            assert sum(pieces) == a_w and min(pieces) >= 0
            tot, nch = run_split(h, blk, pieces)
            blk += 1
            d = tot - F_a
            dmin, dmax, n_nz = min(dmin, d), max(dmax, d), n_nz + (d != 0)
            n_splits, n_pieces = n_splits + 1, n_pieces + l
            min_tot = min(min_tot, tot)
            sav = (F_a - tot) / WAD - (l - 1) * ctx + st * ((F_a > 0) - nch)
            if sav > best_sav + 1e-12:
                best_sav, l_best, ch_best = sav, l, tot
    # check: the transaction-scope optimum of Experiment A under scope="tx" (one piece per transaction)
    ht = fp.ScopedHookReference(k_w, 0, LAM_BPS, GAM_BPS, scope="tx")
    pcs = [a_w] if l_star_tx == 1 else [piece] * (l_star_tx - 1) + [max(a_w - (l_star_tx - 1) * piece, 0)]
    tx_tot = sum(ht.swap(POOL, 0, j, p, 0, WAD) for j, p in enumerate(pcs))
    return {"F_a_w": F_a, "n_splits": n_splits, "n_pieces": n_pieces, "dev_min_w": dmin, "dev_max_w": dmax,
            "n_nonzero_dev": n_nz, "min_total_w": min_tot, "l_star_block": l_best, "charge_star_block_w": ch_best,
            "saving_star_block_usd": best_sav, "tx_check_ok": float(tx_tot) / WAD == ch_star_tx}


def labelled(base: dict, charge: np.ndarray, Fa: np.ndarray, extra: dict | None = None) -> list[dict]:
    """Two rows: sum-weighted share and mean of per-correction ratios (over F(a) > 0)."""
    m = Fa > 0
    sw = charge[m].sum() / Fa[m].sum() if m.any() else np.nan
    mr = float(np.mean(charge[m] / Fa[m])) if m.any() else np.nan
    return [{**base, "aggregation": "sum-weighted", "share": sw, "surviving_usd": charge.sum(), **(extra or {})},
            {**base, "aggregation": "mean of per-correction ratios", "share": mr, "surviving_usd": np.nan, **(extra or {})}]


def main():
    corr = pd.read_csv(OUT / "eA_part1_corrections.csv.gz", float_precision="round_trip")
    capc = pd.read_csv(OUT / "eA_fu_capacity_corrections.csv.gz", float_precision="round_trip")
    capsum = pd.read_csv(OUT / "eA_fu_capacity_summary.csv", float_precision="round_trip")
    assert len(capc) == len(corr) and (capc[["source", "setting", "pool", "variant", "timestamp"]].values
                                       == corr[["source", "setting", "pool", "variant", "timestamp"]].values).all()
    assert (capc["a_usd"].to_numpy() == corr["a_usd"].to_numpy()).all()
    corr["cap_block"] = capc["lcap_block"].to_numpy()
    q = lambda x, p: float(np.quantile(x, p)) if len(x) else np.nan  # noqa: E731
    rows = []

    # ---- 1. within one block
    sel = corr[(corr.source == "e2_replay") & corr.setting.isin(RULES)
               & corr[["pool", "variant"]].apply(tuple, axis=1).isin(PART1_POOLS)].copy()
    args = list(zip(sel.a_usd, sel.kappa_usd, sel.unit_usd, sel.c_tx_usd, sel.settle_usd, sel.cap_block.astype(int),
                    sel.l_star.astype(int), sel.charge_star_usd))
    # largest corrections first so the pool stays busy
    order = np.argsort(-sel["a_usd"].to_numpy())
    with ProcessPoolExecutor() as ex:
        res = list(ex.map(within_block, [args[i] for i in order], chunksize=8))
    res_sorted = [None] * len(args)
    for i, r in zip(order, res):
        res_sorted[i] = r
    r1 = pd.DataFrame(res_sorted, index=sel.index)
    assert r1["tx_check_ok"].all(), "scope='tx' does not reproduce Experiment A's charge(l*)"
    assert np.allclose(r1["F_a_w"].astype(float) / WAD, sel["F_a_usd"], rtol=0, atol=0)
    sel = pd.concat([sel, r1], axis=1)
    for (st, pool, var), x in sel.groupby(["setting", "pool", "variant"], sort=False):
        Fa = x["F_a_w"].astype(float).to_numpy() / WAD
        base = {"part": "1 within one block", "scope": "block (ScopedHookReference, scope=block)",
                "source": "e2_replay", "setting": st, "rule": RULES[st], "pool": pool, "variant": var,
                "l_cap": f"block capacity ({int(x.cap_block.min())}-{int(x.cap_block.max())})",
                "split": "any l <= cap: (i) l-1 pieces of kappa-1 unit + remainder, (ii) l equal pieces",
                "n_corrections": len(x), "n_charged": int((Fa > 0).sum()), "original_usd": Fa.sum()}
        ext = {"n_splits_evaluated": int(x.n_splits.sum()), "n_pieces_evaluated": int(x.n_pieces.sum()),
               "deviation_wei_min": int(x.dev_min_w.min()), "deviation_wei_max": int(x.dev_max_w.max()),
               "n_splits_nonzero_deviation": int(x.n_nonzero_dev.sum()),
               "l_star_with_gas_max": int(x.l_star_block.max()), "n_l_star_with_gas_gt1": int((x.l_star_block > 1).sum()),
               "tx_scope_check_mismatches": int((~x.tx_check_ok).sum())}
        mt = x["min_total_w"].astype(float).to_numpy() / WAD
        cs = x["charge_star_block_w"].astype(float).to_numpy() / WAD
        rows += labelled({**base, "optimum": "split minimising the block total (gas ignored)"}, mt, Fa, ext)
        rows += labelled({**base, "optimum": "l* with gas (c_tx per extra tx, settlement per charged piece)"}, cs, Fa, ext)

    # ---- 2. cross-block bracket (constant reference and kappa)
    F2 = eA.Fint(0.75, 0.02)
    e2 = corr[(corr.source == "e2_replay") & corr.setting.isin(RULES)]
    for (st, pool, var), x in e2.groupby(["setting", "pool", "variant"], sort=False):
        caps = {**CROSS_CAPS, "l<=10": 10, "l<=50": 50}
        ch = {n: [] for n in caps}
        lst = {n: [] for n in caps}
        for r in x.itertuples():
            c, sv = fu.saving_path(r.a_usd, r.kappa_usd, r.unit_usd, r.c_tx_usd, r.settle_usd, F2)
            assert fu.best_l(sv, None) == r.l_star
            for n, cp in caps.items():
                lc = fu.best_l(sv, cp)
                ch[n].append(float(c[lc - 1]) / WAD)
                lst[n].append(lc)
        Fa = x["F_a_usd"].to_numpy()
        old = capsum[(capsum.source == "e2_replay") & (capsum.setting == st) & (capsum.pool == pool) & (capsum.variant == var)].iloc[0]
        for n in ("l<=10", "l<=50"):                 # reproduces the existing capacity table
            assert np.array(ch[n]).sum() / Fa.sum() == old[f"surviving_share_{n}"], (st, pool, var, n)
        m = Fa > 0
        for n, cp in CROSS_CAPS.items():
            l_u, l_c = x["l_star"].to_numpy()[m], np.array(lst[n])[m]
            bind = l_u > cp
            base = {"part": "2 cross-block bracket", "scope": "block, one piece per block, constant reference and kappa",
                    "source": "e2_replay", "setting": st, "rule": RULES[st], "pool": pool, "variant": var, "l_cap": n,
                    "split": "l-1 pieces of kappa-1 unit + remainder, l <= cap blocks", "optimum": "l* with gas",
                    "n_corrections": len(x), "n_charged": int(m.sum()), "original_usd": Fa.sum()}
            ext = {"l_star_median_charged": q(l_c, 0.5), "l_star_p95_charged": q(l_c, 0.95),
                   "l_star_max_charged": int(l_c.max()) if m.any() else np.nan, "n_cap_binds": int(bind.sum()),
                   "transfer_share_cap_binds": Fa[m][bind].sum() / Fa.sum() if Fa.sum() > 0 else np.nan}
            rows += labelled(base, np.array(ch[n]), Fa, ext)

    df = pd.DataFrame(rows)
    lead = ["part", "rule", "setting", "pool", "variant", "scope", "l_cap", "split", "optimum", "aggregation", "share",
            "n_corrections", "n_charged", "original_usd", "surviving_usd"]
    df = df[lead + [c for c in df.columns if c not in lead]]
    path = OUT / "eA_fu2_block_scope.csv"
    df.to_csv(path, index=False)
    gas = eA.gas_constants()
    reporting.write_manifest("eA", {"followup_of": "eA", "part1_pools": PART1_POOLS, "rules": RULES, "cross_caps": CROSS_CAPS,
                                    "lam_bps": LAM_BPS, "gamma_bps": GAM_BPS, "extra_tx_gas": gas["extra_tx_gas"]},
                             [OUT / "eA_part1_corrections.csv.gz", OUT / "eA_fu_capacity_corrections.csv.gz",
                              OUT / "eA_fu_capacity_summary.csv", Path(__file__).resolve(), Path(fu.__file__).resolve(),
                              Path(eA.__file__).resolve(), Path(fp.__file__).resolve()], "test",
                             {"outputs": {path.name: eA.sha(path)},
                              "not_modelled": "pool rounding of per-swap deltas (no pool state in the saved files)"},
                             tag="followup2", latest=False)
    for sd in ("tables", "figures"):
        p_ = OUT / sd
        if p_.exists() and not any(p_.iterdir()):
            p_.rmdir()
    with pd.option_context("display.width", 250, "display.max_columns", 40, "display.max_rows", 200, "display.max_colwidth", 30):
        print(df.drop(columns=["scope", "split"]).to_string())


if __name__ == "__main__":
    main()
