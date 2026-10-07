#!/usr/bin/env python3
"""Experiment C: block scope versus transaction scope on the observed same-block swap sequences (validation months by
default; the test months need --confirm-frozen).

1. The observed swaps of ETH/USDC and ETH/WBTC, grouped by block and ordered by log index: how often a pool has 2+ swaps
   in one block, and 2+ transactions.
2. Each block's swaps are replayed in order through common/fixedpoint.ScopedHookReference (the hook's integer mirror,
   token1 settlement included) with scope="block" and scope="tx", and once more with every swap as its own scope
   ("standalone", the charge the swap would pay alone). Hook parameters:
     - reference: the benchmark lagged by one minute (p_ref_lag_1), times the variant's trailing offset, locked at the
       scope's first valid swap;
     - lambda = 0.75, gamma = 0.02 (E2 headline);
     - kappa = R + delta + 180,214 gas x (base fee + tau_hat) valued at the locked reference (the hook's formula; the
       base token is ETH in both pools), R = E2 median regime, delta = 0 or the rolling eps_S at d = 1 (k = 1 baseline
       replay of the split, as E2's buffered rule);
     - core deltas from the observed swap amounts (swapper side; token0 of the hook = the numeraire = USDC / WBTC);
     - settlement token = the token the swapper receives (exact-input assumption; the data do not say which side was
       specified);
     - invalid oracle (no lagged benchmark): the swap is charged 0 and not accumulated, the next swap retries, as the
       hook. The transaction-scoped contract instead disables the pool for the rest of the transaction; in these data
       every swap of a block shares one minute, hence one oracle state, so the two rules coincide.
3. Arbitrage swap (E8 priority-fee definition): starts outside the fee band of the benchmark (|pre-swap deviation|
   >= gap_multiple x fee, benchmark = p_ref lag 0 times the offset) and ends closer to it.
4. Metrics, per scope, kept separate:
   a. tipped violations (Definition 3): standalone margin M = S - K >= 0 (S at the benchmark, K = 180,214 gas at the
      block's gas price + R) and charge r > M;
   b. spillover charges: r > 0 while the standalone charge is 0;
   c. surplus re-created after a counter-trade lowered the cumulative surplus within a scope, which pays nothing; two
      separately named parts, never merged:
      c1. reversal leak below the watermark: the increase of F(A_j) over F(A_{j-1}) that stays below W_{j-1} (charge
          units, share of the collected and of the standalone transfer), and A re-created below its running maximum
          (surplus units);
      c2. netting: surplus that refills a negative signed cumulative surplus X = pi . Delta_{1:j} (A = [X]^+) left by an
          earlier swap of the scope with negative surplus, e.g. a back-run after a large trade; surplus units, and the
          shortfall of those swaps' charges against their standalone charges;
   d. charges paid by non-arbitrage swaps, count and USD.
   Also: swaps charged after an earlier swap of the same block had failed open (invalid oracle).

  python experiments/eC_same_block_replay.py [--split valid] [--split test --confirm-frozen]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

import e2_sequential_replay as e2  # noqa: E402
import eA_strategic_split as eA  # noqa: E402
from common import fixedpoint as fp, minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS, RESULTS  # noqa: E402

OUT = RESULTS / "eC"
WAD = fp.WAD
POOL_VARIANTS = [("eth_usdc_005", "raw"), ("eth_wbtc_030", "corr24h"), ("eth_wbtc_030", "raw")]   # corr24h = E8 primary
PRIMARY = {("eth_usdc_005", "raw"), ("eth_wbtc_030", "corr24h")}
EPS_D = 1
SETTINGS = ["delta0", "buffered_eps_d1"]
SCOPES = ["block", "tx", "standalone"]
PID = "pool"


def eps_series(key: str, variant: str, split: str, cfg2: dict) -> tuple[pd.Series, dict]:
    """Rolling eps_S at d = 1, k = 1 (baseline replay of `split`, seeded from the preceding split), by grid minute; its
    median is asserted equal to the stored E2 summary of that split."""
    C0 = e2.make_configs(cfg2).iloc[[0]].reset_index(drop=True)
    R_usd = mg.r_regimes_usd(key, variant, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
    seed = e2.eps_seed(key, variant, R_usd, EPS_D, 1, e2.PRIOR[split], cfg2)
    g = mg.build_grid(key, split, variant, lags=(0, EPS_D))
    r = e2.simulate(g, POOLS[key], C0, cfg2, R_usd, np.nan, EPS_D, 1, seed)
    eps = eA.rolling_eps(r, g, seed, 1, EPS_D, cfg2)
    stored = pd.read_parquet(RESULTS / "e2" / f"e2_summary_{split}_lag{EPS_D}_med.parquet")
    stored = stored[(stored.pool == key) & (stored.variant == variant) & (stored.mech == "baseline")]
    chk = {"pool": key, "variant": variant, "check": f"rolling eps_S median, d={EPS_D}, k=1, {split}",
           "recomputed": float(np.nanmedian(eps)), "stored": float(stored["eps_S_usd"].iloc[0])}
    assert np.isclose(chk["recomputed"], chk["stored"], rtol=1e-9, atol=1e-9), chk
    return pd.Series(eps, index=pd.DatetimeIndex(g["t"])), chk


def load_swaps(key: str, variant: str, split: str) -> pd.DataFrame:
    pool = POOLS[key]
    al = mg.load_aligned(key)
    al = al[al["split"] == split].sort_values(["block", "log_index"]).reset_index(drop=True)
    w = mg.VARIANTS[variant]
    off = np.exp(np.nan_to_num(al[f"offset_{w}"].to_numpy(), nan=0.0)) if w else 1.0
    al["pi_bench"] = al["p_ref"] * off
    al["pi_hook"] = al["p_ref_lag_1"] * off
    pre, post = np.log(al["price_pre"] / al["pi_bench"]), np.log(al["price"] / al["pi_bench"])
    al["arb"] = ((pre.abs() >= pool.gap_multiple * pool.fee) & (post.abs() < pre.abs())).to_numpy()
    return al


def block_stats(al: pd.DataFrame, key: str, variant: str) -> dict:
    b = al.groupby("block").agg(n_swaps=("log_index", "size"), n_tx=("tx_hash", "nunique"))
    sw = b["n_swaps"].reindex(al["block"]).to_numpy()
    tx = b["n_tx"].reindex(al["block"]).to_numpy()
    return {"pool": key, "variant": variant, "n_swaps": len(al), "n_blocks": len(b),
            "share_blocks_2plus_swaps": float((b.n_swaps >= 2).mean()), "share_blocks_2plus_tx": float((b.n_tx >= 2).mean()),
            "share_swaps_in_2plus_swap_blocks": float((sw >= 2).mean()), "share_swaps_in_2plus_tx_blocks": float((tx >= 2).mean()),
            "n_tx": int(al["tx_hash"].nunique()), "share_tx_2plus_swaps": float((al.groupby("tx_hash").size() >= 2).mean()),
            "max_swaps_per_block": int(b.n_swaps.max()), "max_tx_per_block": int(b.n_tx.max()),
            "n_arb_swaps": int(al["arb"].sum()), "share_arb": float(al["arb"].mean())}


def replay(al: pd.DataFrame, key: str, kappa_const: np.ndarray, gas_units: int, tau_wei: int, lam_bps: int, gam_bps: int):
    """Run the three scopes over the ordered swaps. Returns per-swap integer results (numeraire base units)."""
    pool = POOLS[key]
    s0, s1 = 10 ** pool.dec1, 10 ** pool.dec0               # numeraire (hook token0) and base (hook token1) units
    d0 = [int(round(-x * s0)) for x in al["amount1"].to_numpy()]
    d1 = [int(round(-x * s1)) for x in al["amount0"].to_numpy()]
    ref = [int(round(p * s0 / s1 * WAD)) if np.isfinite(p) and p > 0 else None for p in al["pi_hook"].to_numpy()]
    bf = al["base_fee_wei"].astype("int64").tolist()
    blk = al["block"].astype("int64").tolist()
    txs = al["tx_hash"].tolist()
    hs = {sc: fp.ScopedHookReference(0, 0, lam_bps, gam_bps, gas_units=gas_units, tau_wei=tau_wei,
                                     scope="tx" if sc == "tx" else "block") for sc in SCOPES}
    n = len(al)
    res = {sc: {f: [0] * n for f in ("r", "tok", "esc", "esc_s", "net_s", "F", "A", "X")} for sc in SCOPES}
    for sc in SCOPES:
        res[sc]["failed_open"] = np.zeros(n, bool)
        res[sc]["after_fail"] = np.zeros(n, bool)
    for sc, h in hs.items():
        R = res[sc]
        F_prev, A_prev, A_max, W_prev, X_prev = 0, 0, 0, 0, 0
        fail_blk = None
        for i in range(n):
            settle0 = d0[i] > 0                              # the swapper receives the numeraire
            h.kappa_const = min(int(kappa_const[i]), fp.U128_MAX)
            if sc == "standalone":
                bkey, tkey = i, 0                            # every swap its own scope
            else:
                bkey, tkey = blk[i], txs[i]
            scope_key = bkey if sc != "tx" else (bkey, tkey)
            open_before = h.pools.get(PID)
            same = open_before is not None and open_before["key"] == scope_key
            if not same:
                F_prev = A_prev = A_max = W_prev = X_prev = 0
            else:
                W_prev = open_before["W"]
            r = h.swap(PID, bkey, tkey, d0[i], d1[i], ref[i], basefee=bf[i], settle_token0=settle0)
            s = h.pools.get(PID)
            if s is None or s["key"] != scope_key:          # invalid oracle, no scope opened
                R["failed_open"][i] = True
                fail_blk = blk[i]
                continue
            R["after_fail"][i] = fail_blk == blk[i] and r > 0
            assert not s["saturated"]
            A = fp.hook_surplus(s["cum0"], s["cum1"], s["ref"])
            F, _ = fp.transfer_wad(A, s["kappa"], s["lam"] * fp.BPS_TO_WAD, s["gam"] * fp.BPS_TO_WAD, 0)
            R["r"][i], R["tok"][i], R["F"][i], R["A"][i] = r, h.last_token_amount, F, A
            if same:
                R["esc"][i] = max(min(F, W_prev) - F_prev, 0)
                R["esc_s"][i] = max(min(A, A_max) - A_prev, 0)
            X = s["cum0"] + fp._div_trunc(s["cum1"] * s["ref"], WAD)     # signed cumulative surplus (A = [X]^+)
            R["X"][i] = X
            if same:
                R["net_s"][i] = max(min(X, 0) - X_prev, 0)          # refills a negative X left by a counter-trade
            F_prev, A_prev, A_max, X_prev = F, A, max(A_max, A), X
    return res


def summarize(df: pd.DataFrame, key: str, variant: str, setting: str) -> list[dict]:
    rows = []
    M = df["margin_usd"].to_numpy()
    feas = M >= 0
    arb = df["arb"].to_numpy()
    for sc in SCOPES:
        r = df[f"r_{sc}_usd"].to_numpy()
        ch = r > 0
        tipped = feas & (r > M)
        spill = ch & (df["r_standalone_usd"].to_numpy() == 0)
        tot = r.sum()
        sa = df["r_standalone_usd"].to_numpy()
        sa_tot = sa.sum()
        net = df[f"nets_{sc}_usd"].to_numpy() > 0
        short = np.clip(sa - r, 0, None)
        rows.append({
            "pool": key, "variant": variant, "primary": (key, variant) in PRIMARY, "setting": setting, "scope": sc,
            "n_swaps": len(df), "n_arb": int(arb.sum()), "n_feasible_standalone": int(feas.sum()),
            "n_charged": int(ch.sum()), "transfer_usd": tot,
            # context (not one of a-d): per-swap charge above / below the standalone charge
            "n_above_standalone": int((r > df["r_standalone_usd"].to_numpy()).sum()),
            "above_standalone_usd": float(np.clip(r - df["r_standalone_usd"].to_numpy(), 0, None).sum()),
            "n_below_standalone": int((r < df["r_standalone_usd"].to_numpy()).sum()),
            "below_standalone_usd": float(np.clip(df["r_standalone_usd"].to_numpy() - r, 0, None).sum()),
            # a. tipped violations (Definition 3)
            "a_n_tipped_violation": int(tipped.sum()), "a_share_tipped_of_feasible": tipped.sum() / feas.sum() if feas.any() else np.nan,
            "a_n_tipped_arb": int((tipped & arb).sum()), "a_n_tipped_nonarb": int((tipped & ~arb).sum()),
            # b. spillover charges
            "b_n_spillover": int(spill.sum()), "b_share_spillover_of_charged": spill.sum() / ch.sum() if ch.any() else np.nan,
            "b_spillover_usd": float(r[spill].sum()), "b_n_spillover_nonarb": int((spill & ~arb).sum()),
            # c. escaped transfer (reversal leak)
            # c1. reversal leak: surplus re-created below the positive running maximum (watermark), uncharged
            "c1_escaped_charge_usd": float(df[f"esc_{sc}_usd"].sum()),
            "c1_escaped_share_of_transfer": float(df[f"esc_{sc}_usd"].sum()) / tot if tot > 0 else np.nan,
            "c1_escaped_share_of_standalone_transfer": float(df[f"esc_{sc}_usd"].sum()) / sa_tot if sa_tot > 0 else np.nan,
            "c1_escaped_surplus_usd": float(df[f"escs_{sc}_usd"].sum()), "c1_n_swaps": int((df[f"escs_{sc}_usd"] > 0).sum()),
            # c2. netting: surplus that refills a negative signed cumulative surplus left by an earlier counter-trade in
            # the scope; charge units = shortfall against the standalone charge of those swaps
            "c2_netted_surplus_usd": float(df[f"nets_{sc}_usd"].sum()), "c2_n_swaps": int(net.sum()),
            "c2_charge_shortfall_usd": float(short[net].sum()),
            "c2_shortfall_share_of_transfer": float(short[net].sum()) / tot if tot > 0 else np.nan,
            "c2_shortfall_share_of_standalone_transfer": float(short[net].sum()) / sa_tot if sa_tot > 0 else np.nan,
            "c2_shortfall_arb_usd": float(short[net & arb].sum()), "c2_n_charged_standalone": int((net & (sa > 0)).sum()),
            # d. charges paid by non-arbitrage swaps
            "d_n_charged_nonarb": int((ch & ~arb).sum()), "d_charge_nonarb_usd": float(r[ch & ~arb].sum()),
            "d_share_transfer_nonarb": float(r[ch & ~arb].sum()) / tot if tot > 0 else np.nan,
            # oracle
            "n_failed_open": int(df[f"failed_open_{sc}"].sum()),
            "n_blocks_with_failed_open": int(df.loc[df[f"failed_open_{sc}"], "block"].nunique()),
            "n_charged_after_failed_open_same_block": int(df[f"after_fail_{sc}"].sum()),
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    ap.add_argument("--confirm-frozen", action="store_true")
    a = ap.parse_args()
    if a.split == "test" and not a.confirm_frozen:
        raise SystemExit("--split test requires --confirm-frozen")
    cfg2 = reporting.load_config(ROOT / "experiments" / "configs" / "e2.yml")
    reporting.guard_split("e2", a.split, a.confirm_frozen, cfg2)    # test: E2 config must equal the frozen one
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    tau = eA.tau_hat_wei()
    gas_units = cfg2["gas_units"] + cfg2["hook_overhead_gas"]
    lam, gam = cfg2["headline"]["lambda"], cfg2["headline"]["gamma"]
    lam_bps, gam_bps = int(round(lam * 1e4)), int(round(gam * 1e4))
    stats, summ, swaps, checks = [], [], [], []
    for key, var in POOL_VARIANTS:
        pool = POOLS[key]
        al = load_swaps(key, var, a.split)
        stats.append(block_stats(al, key, var))
        R_usd = mg.r_regimes_usd(key, var, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
        eps, chk = eps_series(key, var, a.split, cfg2)
        checks.append(chk)
        usd = al["usd_per_num"].to_numpy()
        S_usd = -(al["pi_bench"].to_numpy() * al["amount0"].to_numpy() + al["amount1"].to_numpy()) * usd
        K_usd = gas_units * al["gas_price_wei"].to_numpy() * 1e-18 * al["eth_usd"].to_numpy() + R_usd
        to_usd = usd / 10 ** pool.dec1
        for setting in SETTINGS:
            dl = np.zeros(len(al)) if setting == "delta0" else eA.eps_at(eps, al["timestamp"])
            assert np.isfinite(dl).all(), (key, var, setting, "eps_S undefined")
            kc = np.floor((R_usd + dl) / usd * 10 ** pool.dec1)
            res = replay(al, key, kc, gas_units, int(round(tau)), lam_bps, gam_bps)
            df = pd.DataFrame({"pool": key, "variant": var, "setting": setting, "block": al["block"], "log_index": al["log_index"],
                               "tx_hash": al["tx_hash"], "timestamp": al["timestamp"], "arb": al["arb"],
                               "S_usd": S_usd, "K_usd": K_usd, "margin_usd": S_usd - K_usd, "delta_usd": dl,
                               "ref_valid": al["pi_hook"].notna().to_numpy()})
            for sc in SCOPES:
                R = res[sc]
                df[f"r_{sc}_usd"] = np.array(R["r"], dtype=float) * to_usd
                df[f"token_{sc}"] = np.array(R["tok"], dtype=float)
                df[f"F_{sc}_usd"] = np.array(R["F"], dtype=float) * to_usd
                df[f"esc_{sc}_usd"] = np.array(R["esc"], dtype=float) * to_usd
                df[f"escs_{sc}_usd"] = np.array(R["esc_s"], dtype=float) * to_usd
                df[f"nets_{sc}_usd"] = np.array(R["net_s"], dtype=float) * to_usd
                df[f"X_{sc}_usd"] = np.array(R["X"], dtype=float) * to_usd
                df[f"failed_open_{sc}"] = R["failed_open"]
                df[f"after_fail_{sc}"] = R["after_fail"]
            # checks: standalone scopes have no spillover and no escape; tx and block coincide on one-swap blocks
            assert (df["esc_standalone_usd"] == 0).all() and (df["nets_standalone_usd"] == 0).all()
            one = df.groupby("block")["log_index"].transform("size") == 1
            assert (df.loc[one, "r_block_usd"] == df.loc[one, "r_standalone_usd"]).all()
            summ += summarize(df, key, var, setting)
            swaps.append(df)
            print(key, var, setting, len(df), f"{time.time() - t0:.0f}s", flush=True)
    sfx = a.split
    st_df, su_df, sw_df, ck_df = pd.DataFrame(stats), pd.DataFrame(summ), pd.concat(swaps, ignore_index=True), pd.DataFrame(checks)
    st_df.to_csv(OUT / f"eC_block_stats_{sfx}.csv", index=False)
    su_df.to_csv(OUT / f"eC_summary_{sfx}.csv", index=False)
    sw_df.to_csv(OUT / f"eC_swaps_{sfx}.csv.gz", index=False)
    ck_df.to_csv(OUT / f"eC_checks_{sfx}.csv", index=False)
    params = {"experiment": "eC", "split": a.split, "pool_variants": POOL_VARIANTS, "primary": sorted(PRIMARY),
              "lam": lam, "gamma": gam, "lam_bps": lam_bps, "gamma_bps": gam_bps, "kappa_gas_units": gas_units,
              "tau_hat_wei": tau, "R_regime": "median", "reference": "p_ref_lag_1 x exp(offset)", "eps_delay_min": EPS_D,
              "settings": SETTINGS, "scopes": SCOPES,
              "settlement_token": "token the swapper receives (exact-input assumption)",
              "arb_rule": "E8: |pre deviation| >= gap_multiple x fee and |post| < |pre| at p_ref x exp(offset)",
              "margin": "S at benchmark - (kappa_gas_units x block gas price x ETH + R)",
              "config_sha256_e2": reporting.config_hash(cfg2)}
    inputs = ([CACHE / "aligned" / f"{k}.parquet" for k in ("eth_usdc_005", "eth_wbtc_030")] + [CACHE / "block_gas.parquet"]
              + [RESULTS / "e1" / f"depth_{k}.parquet" for k in ("eth_usdc_005", "eth_wbtc_030")]
              + [RESULTS / "e2" / f"e2_summary_{a.split}_lag{EPS_D}_med.parquet", ROOT / "experiments" / "configs" / "e2.yml",
                 Path(__file__).resolve(), Path(fp.__file__).resolve(), Path(eA.__file__).resolve(), Path(e2.__file__).resolve()]
              + sorted((ROOT / "data" / "data" / "binance").glob("*_1m.csv.gz")))
    reporting.write_manifest("eC", params, inputs, a.split,
                             {"parameters": params, "outputs": {p.name: eA.sha(p) for p in sorted(OUT.glob(f"eC_*_{sfx}*"))}},
                             tag=sfx)
    for sd in ("tables", "figures"):
        p_ = OUT / sd
        if p_.exists() and not any(p_.iterdir()):
            p_.rmdir()
    with pd.option_context("display.width", 250, "display.max_columns", 60, "display.max_rows", 200):
        print(st_df.T.to_string())
        print(su_df.T.to_string())
        print(ck_df.to_string())
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
