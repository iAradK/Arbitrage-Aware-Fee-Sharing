#!/usr/bin/env python3
"""Experiment C, Q2 (post-processing, validation months only): two accumulation variants of the block-scoped hook that
stop a counter-trade of one transaction from netting another transaction's surplus.

  V1 per-transaction clipping (ScopedHookReference accumulation="tx_clip"): block accumulator = sum over the completed
     transactions of the block of [surplus of that transaction]^+, plus the current transaction's running surplus under
     the watermark rule.
  V2 per-swap clipping (accumulation="swap_clip"): block accumulator = sum over the block's swaps of [surplus]^+.

1. The swap sequences, settings (delta = 0 and buffered delta = rolling eps_S, d = 1), arbitrage labels and standalone
   margins are those of Experiment C (results/eC/eC_swaps_valid.csv.gz). Core deltas, locked reference and base fee
   are re-read from the aligned swaps (same rows, same order, asserted). The transaction scope, the current block scope
   (net) and the standalone charges are replayed again and asserted equal to Experiment C's, then V1 and V2.
2. Metrics per accumulation, as in Experiment C and kept separate: (a) tipped violations, (b) spillover charges,
   (c1) reversal leak, (c2) netting shortfall, (d) charges on non-arbitrage swaps; for all swaps and without the block
   of the 294 ETH event (ETH/USDC block 24915388). c1 uses each accumulation's own accumulator A; c2 uses the part
   that can go negative: the scope's signed surplus (net, tx scope) or the current transaction's (V1); V2 has none.
3. Transactions that swap in both directions (2+ swaps whose base-token deltas have opposite signs): charge to the
   transaction under V2 (and V1, net) above its transaction-scope charge.
4. Experiment A's within-block split check (eA_followup2) for V1 and V2: every E2 correction (ideal and buffered,
   ETH/USDC and ETH/WBTC raw / offset) split into l = 2..block capacity transactions of one block, two families.

  python experiments/eC_netting_variants.py            (test months are refused)
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

import eA_followup2 as fu2  # noqa: E402
import eA_strategic_split as eA  # noqa: E402
import eC_same_block_replay as eC  # noqa: E402
from common import fixedpoint as fp, minutegrid as mg, reporting  # noqa: E402
from common.pools import POOLS, RESULTS  # noqa: E402

OUT = RESULTS / "eC"
WAD = fp.WAD
EVENT_BLOCK = 24915388                    # 294 ETH sale and its back-run, 2026-04-19 (Experiment C)
# name -> (scope, accumulation)
ACCS = {"tx": ("tx", "net"), "block_net": ("block", "net"), "V1_tx_clip": ("block", "tx_clip"),
        "V2_swap_clip": ("block", "swap_clip"), "standalone": ("block", "net")}
PID = "pool"


def replay(al: pd.DataFrame, key: str, kc: np.ndarray, gas_units: int, tau_wei: int, lam_bps: int, gam_bps: int) -> dict:
    """Per-swap charge r (numeraire units), c1 escaped charge / surplus and c2 netted surplus for every accumulation."""
    pool = POOLS[key]
    s0, s1 = 10 ** pool.dec1, 10 ** pool.dec0
    d0 = [int(round(-x * s0)) for x in al["amount1"].to_numpy()]
    d1 = [int(round(-x * s1)) for x in al["amount0"].to_numpy()]
    ref = [int(round(p * s0 / s1 * WAD)) if np.isfinite(p) and p > 0 else None for p in al["pi_hook"].to_numpy()]
    bf = al["base_fee_wei"].astype("int64").tolist()
    blk = al["block"].astype("int64").tolist()
    txs = al["tx_hash"].tolist()
    n = len(al)
    out = {}
    for name, (scope, acc) in ACCS.items():
        h = fp.ScopedHookReference(0, 0, lam_bps, gam_bps, gas_units=gas_units, tau_wei=tau_wei, scope=scope, accumulation=acc)
        r_, esc, escs, nets = [0] * n, [0] * n, [0] * n, [0] * n
        F_prev = A_prev = A_max = 0
        N_prev, N_key = 0, None
        for i in range(n):
            h.kappa_const = min(int(kc[i]), fp.U128_MAX)
            bkey, tkey = (i, 0) if name == "standalone" else (blk[i], txs[i])
            skey = (bkey, tkey) if scope == "tx" else bkey
            before = h.pools.get(PID)
            same = before is not None and before["key"] == skey
            W_prev = before["W"] if same else 0
            if not same:
                F_prev = A_prev = A_max = 0
            r = h.swap(PID, bkey, tkey, d0[i], d1[i], ref[i], basefee=bf[i], settle_token0=d0[i] > 0)
            s = h.pools.get(PID)
            if s is None or s["key"] != skey:
                continue                                     # invalid oracle (none in the validation months)
            assert not s["saturated"]
            A = h.surplus(PID)
            F, _ = fp.transfer_wad(A, s["kappa"], s["lam"] * fp.BPS_TO_WAD, s["gam"] * fp.BPS_TO_WAD, 0)
            r_[i] = r
            if same:
                esc[i] = max(min(F, W_prev) - F_prev, 0)     # c1: increase of F(A) below the watermark
                escs[i] = max(min(A, A_max) - A_prev, 0)
            F_prev, A_prev, A_max = F, A, max(A_max, A)
            if acc != "swap_clip":                           # c2: the signed part that a counter-trade can push below 0
                nkey = (bkey, tkey) if (scope == "tx" or acc == "tx_clip") else bkey
                N = s["cum0"] + fp._div_trunc(s["cum1"] * s["ref"], WAD)
                if nkey == N_key:
                    nets[i] = max(min(N, 0) - N_prev, 0)
                N_prev, N_key = N, nkey
        out[name] = {"r": r_, "esc": esc, "escs": escs, "nets": nets}
    out["d1"] = d1
    return out


def metrics(df: pd.DataFrame, names) -> list[dict]:
    rows = []
    M = df["margin_usd"].to_numpy()
    feas = M >= 0
    arb = df["arb"].to_numpy()
    sa = df["r_standalone_usd"].to_numpy()
    for nm in names:
        r = df[f"r_{nm}_usd"].to_numpy()
        ch = r > 0
        tot, sa_tot = r.sum(), sa.sum()
        tipped = feas & (r > M)
        spill = ch & (sa == 0)
        net = df[f"nets_{nm}_usd"].to_numpy() > 0
        short = np.clip(sa - r, 0, None)
        esc = df[f"esc_{nm}_usd"].to_numpy()
        rows.append({
            "accumulation": nm, "n_swaps": len(df), "n_feasible_standalone": int(feas.sum()), "n_charged": int(ch.sum()),
            "transfer_usd": tot, "standalone_transfer_usd": sa_tot,
            "a_n_tipped_violation": int(tipped.sum()), "a_share_tipped_of_feasible": tipped.sum() / feas.sum() if feas.any() else np.nan,
            "b_n_spillover": int(spill.sum()), "b_share_spillover_of_charged": spill.sum() / ch.sum() if ch.any() else np.nan,
            "b_spillover_usd": float(r[spill].sum()),
            "c1_escaped_charge_usd": float(esc.sum()), "c1_escaped_share_of_transfer": float(esc.sum()) / tot if tot > 0 else np.nan,
            "c1_escaped_surplus_usd": float(df[f"escs_{nm}_usd"].sum()), "c1_n_swaps": int((df[f"escs_{nm}_usd"] > 0).sum()),
            "c2_n_swaps": int(net.sum()), "c2_netted_surplus_usd": float(df[f"nets_{nm}_usd"].sum()),
            "c2_charge_shortfall_usd": float(short[net].sum()),
            "c2_shortfall_share_of_standalone_transfer": float(short[net].sum()) / sa_tot if sa_tot > 0 else np.nan,
            "d_n_charged_nonarb": int((ch & ~arb).sum()), "d_charge_nonarb_usd": float(r[ch & ~arb].sum()),
            "d_share_transfer_nonarb": float(r[ch & ~arb].sum()) / tot if tot > 0 else np.nan,
            "above_standalone_usd": float(np.clip(r - sa, 0, None).sum()), "below_standalone_usd": float(short.sum()),
        })
    return rows


def both_directions(df: pd.DataFrame, d1: np.ndarray) -> list[dict]:
    """Transactions with 2+ swaps whose base-token deltas have opposite signs: excess of each accumulation's charge to the
    transaction over its transaction-scope charge (and V2 over V1)."""
    x = df.assign(sgn=np.sign(d1))
    g = x.groupby("tx_hash")
    both = (g["sgn"].transform("max") > 0) & (g["sgn"].transform("min") < 0)
    t = x[both].groupby("tx_hash")[[f"r_{n}_usd" for n in ACCS] + ["margin_usd"]].sum()
    rows = []
    for nm, base in [("V2_swap_clip", "tx"), ("V1_tx_clip", "tx"), ("block_net", "tx"), ("V2_swap_clip", "V1_tx_clip")]:
        ex = (t[f"r_{nm}_usd"] - t[f"r_{base}_usd"]).clip(lower=0)
        rows.append({"accumulation": nm, "compared_with": base, "n_tx_both_directions": len(t),
                     "n_swaps_in_them": int(both.sum()), "n_tx_with_excess": int((ex > 1e-12).sum()),
                     "excess_usd": float(ex.sum()), "excess_max_usd": float(ex.max()) if len(ex) else 0.0,
                     "charge_usd": float(t[f"r_{nm}_usd"].sum()), "charge_compared_usd": float(t[f"r_{base}_usd"].sum())})
    return rows


# ------------------------------------------------------------------ Experiment A within-block split check, V1 / V2
def split_check_one(row: tuple, accumulation: str) -> dict:
    """eA_followup2.within_block for one accumulation: every split l <= cap, each piece its own transaction."""
    a_usd, k_usd, u_usd, cap = row
    a_w, k_w = fp.to_wad(max(a_usd, 0.0)), fp.to_wad(k_usd)
    piece = k_w - max(1, fp.to_wad(u_usd))
    h = fp.ScopedHookReference(k_w, 0, fu2.LAM_BPS, fu2.GAM_BPS, scope="block", accumulation=accumulation)
    F_a, _ = fu2.run_split(h, 0, [a_w])
    blk, n_splits, n_pieces, dmin, dmax, n_nz = 1, 0, 0, 0, 0, 0
    for fam, lmax in {"i": min(cap, -(-a_w // piece)) if a_w > 0 else 1, "ii": cap}.items():
        for l in range(2, lmax + 1):
            if fam == "i":
                pieces = [piece] * (l - 1) + [a_w - (l - 1) * piece]
            else:
                q = a_w // l
                pieces = [q] * (l - 1) + [a_w - (l - 1) * q]
            tot, _ = fu2.run_split(h, blk, pieces)
            blk += 1
            d = tot - F_a
            dmin, dmax, n_nz = min(dmin, d), max(dmax, d), n_nz + (d != 0)
            n_splits, n_pieces = n_splits + 1, n_pieces + l
    return {"F_a_w": F_a, "n_splits": n_splits, "n_pieces": n_pieces, "dev_min_w": dmin, "dev_max_w": dmax, "n_nonzero_dev": n_nz}


def split_check() -> pd.DataFrame:
    corr = pd.read_csv(RESULTS / "eA" / "eA_part1_corrections.csv.gz", float_precision="round_trip")
    capc = pd.read_csv(RESULTS / "eA" / "eA_fu_capacity_corrections.csv.gz", float_precision="round_trip")
    assert (capc["a_usd"].to_numpy() == corr["a_usd"].to_numpy()).all()
    corr["cap_block"] = capc["lcap_block"].to_numpy()
    sel = corr[(corr.source == "e2_replay") & corr.setting.isin(fu2.RULES)
               & corr[["pool", "variant"]].apply(tuple, axis=1).isin(fu2.PART1_POOLS)].copy()
    args = list(zip(sel.a_usd, sel.kappa_usd, sel.unit_usd, sel.cap_block.astype(int)))
    order = np.argsort(-sel["a_usd"].to_numpy())
    rows = []
    for acc in ("tx_clip", "swap_clip"):
        with ProcessPoolExecutor() as ex:
            res = list(ex.map(partial(split_check_one, accumulation=acc), [args[i] for i in order], chunksize=8))
        back = [None] * len(args)
        for i, x in zip(order, res):
            back[i] = x
        r = pd.DataFrame(back, index=sel.index)
        assert np.allclose(r["F_a_w"].astype(float) / WAD, sel["F_a_usd"], rtol=0, atol=0)
        x = pd.concat([sel, r], axis=1)
        for (st, pool, var), g in x.groupby(["setting", "pool", "variant"], sort=False):
            Fa = g["F_a_w"].astype(float).to_numpy() / WAD
            m = Fa > 0
            # every split collects F(a) + deviation; the surviving share of the split minimising the total
            worst = (g["F_a_w"] + g["dev_min_w"]).astype(float).to_numpy() / WAD
            rows.append({"accumulation": acc, "rule": fu2.RULES[st], "setting": st, "pool": pool, "variant": var,
                         "n_corrections": len(g), "n_charged": int(m.sum()), "original_usd": Fa.sum(),
                         "survival_sum_weighted": worst.sum() / Fa.sum(),
                         "survival_mean_of_per_correction_ratios": float(np.mean(worst[m] / Fa[m])),
                         "n_splits_evaluated": int(g.n_splits.sum()), "n_pieces_evaluated": int(g.n_pieces.sum()),
                         "deviation_wei_min": int(g.dev_min_w.min()), "deviation_wei_max": int(g.dev_max_w.max()),
                         "n_splits_nonzero_deviation": int(g.n_nonzero_dev.sum())})
        print("split check", acc, "done", flush=True)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    ap.add_argument("--skip-split-check", action="store_true")
    a = ap.parse_args()
    if a.split != "valid":
        raise SystemExit("Q2 is validation-only: the test months are refused")
    t0 = time.time()
    cfg2 = reporting.load_config(ROOT / "experiments" / "configs" / "e2.yml")
    tau = eA.tau_hat_wei()
    gas_units = cfg2["gas_units"] + cfg2["hook_overhead_gas"]
    lam_bps, gam_bps = int(round(cfg2["headline"]["lambda"] * 1e4)), int(round(cfg2["headline"]["gamma"] * 1e4))
    sw = pd.read_csv(OUT / "eC_swaps_valid.csv.gz", float_precision="round_trip")
    summ, bothd, slim = [], [], []
    for (key, var), xs in sw.groupby(["pool", "variant"], sort=False):
        pool = POOLS[key]
        al = eC.load_swaps(key, var, "valid")
        R_usd = mg.r_regimes_usd(key, var, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
        usd = al["usd_per_num"].to_numpy()
        to_usd = usd / 10 ** pool.dec1
        for setting, df in xs.groupby("setting", sort=False):
            df = df.reset_index(drop=True)
            assert len(df) == len(al) and (df["block"].to_numpy() == al["block"].to_numpy()).all() \
                and (df["log_index"].to_numpy() == al["log_index"].to_numpy()).all()
            kc = np.floor((R_usd + df["delta_usd"].to_numpy()) / usd * 10 ** pool.dec1)
            res = replay(al, key, kc, gas_units, int(round(tau)), lam_bps, gam_bps)
            old = {c: df[c].to_numpy().copy() for c in ("r_tx_usd", "r_block_usd", "r_standalone_usd", "esc_block_usd",
                                                         "escs_block_usd", "nets_block_usd", "nets_tx_usd")}
            df = df[["pool", "variant", "setting", "block", "log_index", "tx_hash", "arb", "margin_usd"]].copy()
            for nm in ACCS:
                for f in ("r", "esc", "escs", "nets"):
                    df[f"{f}_{nm}_usd"] = np.array(res[nm][f], dtype=float) * to_usd
            # Experiment C's charges and its c1 / c2 quantities are reproduced exactly (same integer path)
            for new, prev in [("r_tx_usd", "r_tx_usd"), ("r_standalone_usd", "r_standalone_usd"), ("r_block_net_usd", "r_block_usd"),
                              ("esc_block_net_usd", "esc_block_usd"), ("escs_block_net_usd", "escs_block_usd"),
                              ("nets_block_net_usd", "nets_block_usd"), ("nets_tx_usd", "nets_tx_usd")]:
                assert (df[new].to_numpy() == old[prev]).all(), (key, var, setting, new)
            for sample, sel in [("all swaps", np.ones(len(df), bool)), ("without the 294 ETH block", df["block"].to_numpy() != EVENT_BLOCK)]:
                for row in metrics(df[sel].reset_index(drop=True), ACCS):
                    summ.append({"pool": key, "variant": var, "primary": (key, var) in eC.PRIMARY, "setting": setting,
                                 "sample": sample, **row})
            for row in both_directions(df, np.array(res["d1"])):
                bothd.append({"pool": key, "variant": var, "setting": setting, **row})
            slim.append(df[["pool", "variant", "setting", "block", "log_index", "tx_hash", "arb", "margin_usd"]
                           + [f"r_{nm}_usd" for nm in ACCS]])
            print(key, var, setting, f"{time.time() - t0:.0f}s", flush=True)
    su = pd.DataFrame(summ)
    bd = pd.DataFrame(bothd)
    su.to_csv(OUT / "eC_netting_variants_valid.csv", index=False)
    bd.to_csv(OUT / "eC_netting_both_directions_valid.csv", index=False)
    pd.concat(slim, ignore_index=True).to_csv(OUT / "eC_netting_swaps_valid.csv.gz", index=False)
    outs = [OUT / "eC_netting_variants_valid.csv", OUT / "eC_netting_both_directions_valid.csv", OUT / "eC_netting_swaps_valid.csv.gz"]
    if not a.skip_split_check:
        sc = split_check()
        sc.to_csv(OUT / "eC_netting_split_check_valid.csv", index=False)
        outs.append(OUT / "eC_netting_split_check_valid.csv")
    params = {"experiment": "eC_netting", "split": "valid", "accumulations": ACCS, "event_block": EVENT_BLOCK,
              "lam_bps": lam_bps, "gamma_bps": gam_bps, "kappa_gas_units": gas_units, "tau_hat_wei": tau,
              "source": "results/eC/eC_swaps_valid.csv.gz (swap order, settings, delta, arbitrage label, margin)"}
    inputs = [OUT / "eC_swaps_valid.csv.gz", OUT / "manifest_valid.json", RESULTS / "eA" / "eA_part1_corrections.csv.gz",
              RESULTS / "eA" / "eA_fu_capacity_corrections.csv.gz", ROOT / "experiments" / "configs" / "e2.yml",
              Path(__file__).resolve(), Path(fp.__file__).resolve(), Path(eC.__file__).resolve(), Path(fu2.__file__).resolve()]
    reporting.write_manifest("eC", params, inputs, "valid", {"parameters": params, "outputs": {p.name: eA.sha(p) for p in outs}},
                             tag="netting_valid", latest=False)
    for sd in ("tables", "figures"):
        p_ = OUT / sd
        if p_.exists() and not any(p_.iterdir()):
            p_.rmdir()
    with pd.option_context("display.width", 250, "display.max_columns", 60, "display.max_rows", 300):
        print(su.to_string())
        print(bd.to_string())
        if not a.skip_split_check:
            print(sc.to_string())
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
