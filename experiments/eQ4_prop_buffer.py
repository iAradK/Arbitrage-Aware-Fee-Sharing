#!/usr/bin/env python3
"""Q4, parts 1 and 2: a proportional buffer eps_P x |Delta_X| per transaction instead of the fixed buffer delta inside
kappa (validation months only; the test months are refused).

With token Y (the numeraire) as the unit of account, S_hat - S = (P_hat - P) x Delta_X exactly, so a bound eps_P on
|P_hat - P| bounds each swap's surplus error by eps_P |Delta_X|.

1. Calibration. eps_P(d = 1) uses E2's rolling eps_S procedure unchanged: at each minute, the 95th percentile over the
   baseline-feasible candidates of the k = 1 baseline replay strictly earlier within 7 days (seeded with the last days
   of the training months; all history while fewer than 20). The error is |P_hat - P| (absolute, numeraire per
   ETH) or |P_hat / P - 1| (relative; buffer eps_rel x P_hat x |Delta_X|), with P = the benchmark p_ref_0 and P_hat the
   hook reference p_ref_1 (variant offset applied, as the grid). Coverage is out of sample: the share of validation
   candidates (and of observed swaps) whose error is within the bound computed from strictly earlier data. The same
   generic quantile code reproduces E2's eps_S exactly (asserted).
2. Observed swaps of Experiment C (results/eC/eC_swaps_valid.csv.gz, joined to the aligned amounts), replayed through
   ScopedHookReference with tau_hat = 3 gwei in K_hat, for the transaction scope and block scope V1 (tx_clip):
     none   no buffer, kappa = K_hat (= R + 180,214 gas x (base fee + tau_hat) at the locked reference);
     fixed  kappa = K_hat + delta, delta = eps_S (Experiment C's buffered delta);
     prop   kappa = K_hat, each transaction's bracket [S_hat_tx - eps_P G_tx]^+ with G_tx its gross |Delta_X|
            (buffer="abs"), and the relative version (buffer="rel"); lambda applies to the buffered surplus.
   Metrics as Experiment C / Q2: (a) tipped violations, (b) spillover, (c1) reversal leak, (c2) netting shortfall,
   (d) non-arbitrage charges, with and without the 294 ETH block; the standalone charges of each rule are its own.
   Check: with no buffer and the old tau_hat the replay reproduces Q2's transaction-scope and V1 charges exactly.

  python experiments/eQ4_prop_buffer.py
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
import eC_netting_variants as q2  # noqa: E402
import eC_same_block_replay as eC  # noqa: E402
from common import fixedpoint as fp, minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS, RESULTS  # noqa: E402

OUT = RESULTS / "eQ4"
WAD = fp.WAD
TAU_Q4_WEI = 3e9                       # tau_hat = 3 gwei (Q3: training P99.75)
EPS_D = 1
POOL_VARIANTS = [("eth_usdc_005", "raw"), ("eth_wbtc_030", "corr24h"), ("eth_wbtc_030", "raw")]
RULES = ["none", "fixed", "prop_abs", "prop_rel"]
SCOPES = {"tx": ("tx", "net"), "V1": ("block", "tx_clip"), "standalone": ("block", "net")}
PID = "pool"


# ------------------------------------------------------------------ rolling quantile (E2's eps_S procedure, generic)
def rolling_q(cand_t, cand_err, cand_ok, seed_t, seed_err, query_t, cfg2) -> np.ndarray:
    """At each query time: the eps_quantile of the errors stamped strictly earlier within eps_window_days (seed first,
    then the run's candidates with cand_ok); all history if fewer than 20; 0 without history. As eA.rolling_eps."""
    ht = np.concatenate([seed_t, cand_t[cand_ok]])
    he = np.concatenate([seed_err, cand_err[cand_ok]])
    assert np.all(np.diff(ht.astype(np.int64)) >= 0)
    W = np.timedelta64(int(cfg2["eps_window_days"] * 86400e9), "ns")
    q = cfg2["eps_quantile"]
    out = np.full(len(query_t), np.nan)
    for i, t in enumerate(query_t):
        hi = int(np.searchsorted(ht, t, "left"))
        lo = min(int(np.searchsorted(ht, t - W, "left")), hi)
        win = he[lo:hi]
        out[i] = float(np.quantile(win, q)) if len(win) >= 20 else (float(np.quantile(he[:hi], q)) if hi else 0.0)
    return out


def naive_ns(t) -> np.ndarray:
    t = pd.DatetimeIndex(t)
    return (t.tz_convert(None) if t.tz is not None else t).values.astype("datetime64[ns]")


def price_seed(key, variant, R_usd, d, k, split, cfg2) -> pd.DataFrame:
    """As e2.eps_seed, with the candidates' price errors: the last eps_window_days + 1 days of `split`."""
    gv = mg.build_grid(key, split, variant, lags=(0, d))
    gv = gv[gv["t"] >= gv["t"].iloc[-1] - pd.Timedelta(days=cfg2["eps_window_days"] + 1)].reset_index(drop=True)
    C0 = e2.make_configs(cfg2).iloc[[0]].reset_index(drop=True)
    c = e2.simulate(gv, POOLS[key], C0, cfg2, R_usd, 0.0, d, k)["cand"]
    bf = c["bf"].to_numpy(bool)
    P, Ph = gv["p_ref_0"].to_numpy(), gv[f"p_ref_{d}"].to_numpy()
    return pd.DataFrame({"t": pd.DatetimeIndex(c["t"]).tz_convert("UTC")[bf], "err_usd": (c["S_hat_usd"] - c["S_usd"]).abs().to_numpy()[bf],
                         "err_signed_usd": (c["S_hat_usd"] - c["S_usd"]).to_numpy()[bf], "err_abs": np.abs(Ph - P)[bf], "err_rel": np.abs(Ph / P - 1)[bf]})


def eps_series(r: dict, g: pd.DataFrame, seed: pd.DataFrame, k: int, d: int, cfg2: dict) -> pd.DataFrame:
    """eps_S (USD), eps_P absolute (numeraire per ETH) and relative at every action step of a replay, plus per-minute
    out-of-sample coverage flags of the baseline-feasible candidates."""
    cand = r["cand"]
    tt = naive_ns(g["t"])
    cb = cand["bf"].to_numpy(bool)
    P, Ph = g["p_ref_0"].to_numpy(), g[f"p_ref_{d}"].to_numpy()
    errs = {"eps_S": (cand["S_hat_usd"] - cand["S_usd"]).abs().to_numpy(float), "eps_P_abs": np.abs(Ph - P),
            "eps_P_rel": np.abs(Ph / P - 1)}
    seeds = {"eps_S": seed["err_usd"].to_numpy(float), "eps_P_abs": seed["err_abs"].to_numpy(float),
             "eps_P_rel": seed["err_rel"].to_numpy(float)}
    st = naive_ns(seed["t"])
    ok = np.isfinite(P) & np.isfinite(Ph) & np.isfinite(g["L"].to_numpy()) & np.isfinite(g["gas_wei"].to_numpy())
    act = ok & (np.arange(len(g)) % k == 0)
    out = pd.DataFrame({"t": g["t"], "act": act, "bf": cb})
    for name in errs:
        v = np.full(len(g), np.nan)
        v[act] = rolling_q(tt, errs[name], cb, st, seeds[name], tt[act], cfg2)
        out[name] = v
        out[f"err_{name}"] = errs[name]
    return out


# ------------------------------------------------------------------ part 2: observed swaps
def replay_rule(al, key, kc, eps_int, buffer, tau_wei, gas_units, lam_bps, gam_bps) -> dict:
    """Charges, c1 and c2 quantities per scope for one rule. kc: kappa_const per swap (numeraire units); eps_int: the
    buffer's eps per swap (WAD-scaled; 0 without buffer)."""
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
    for name, (scope, acc) in SCOPES.items():
        h = fp.ScopedHookReference(0, 0, lam_bps, gam_bps, gas_units=gas_units, tau_wei=tau_wei, scope=scope,
                                   accumulation=acc, buffer=buffer)
        r_, esc, escs, nets = [0] * n, [0] * n, [0] * n, [0] * n
        F_prev = A_prev = A_max = 0
        N_prev, N_key = 0, None
        for i in range(n):
            h.kappa_const = min(int(kc[i]), fp.U128_MAX)
            h.eps_wad = int(eps_int[i])
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
                continue
            assert not s["saturated"]
            A = h.surplus(PID)
            F, _ = fp.transfer_wad(A, s["kappa"], s["lam"] * fp.BPS_TO_WAD, s["gam"] * fp.BPS_TO_WAD, 0)
            r_[i] = r
            if same:
                esc[i] = max(min(F, W_prev) - F_prev, 0)
                escs[i] = max(min(A, A_max) - A_prev, 0)
            F_prev, A_prev, A_max = F, A, max(A_max, A)
            # c2: the signed (buffered) bracket of the scope (tx) or the current transaction (V1)
            nkey = (bkey, tkey)
            N = s["cum0"] + fp._div_trunc(s["cum1"] * s["ref"], WAD) - deduction(buffer, s)
            if nkey == N_key:
                nets[i] = max(min(N, 0) - N_prev, 0)
            N_prev, N_key = N, nkey
        out[name] = {"r": r_, "esc": esc, "escs": escs, "nets": nets}
    return out


def deduction(buffer: str, s: dict) -> int:
    """The buffer the reference deducts from the scope's current bracket (as ScopedHookReference._bracket)."""
    if buffer == "abs":
        return -(-s["eps"] * s["g"] // WAD)
    if buffer == "rel":
        return -(-s["eps"] * s["ref"] * s["g"] // (WAD * WAD))
    return 0


def swap_frame(df, res, to_usd) -> pd.DataFrame:
    x = df[["pool", "variant", "block", "log_index", "tx_hash", "arb", "margin_usd"]].copy()
    for nm in SCOPES:
        for f in ("r", "esc", "escs", "nets"):
            x[f"{f}_{nm}_usd"] = np.array(res[nm][f], dtype=float) * to_usd
    return x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    a = ap.parse_args()
    if a.split != "valid":
        raise SystemExit("Q4 is validation-only: the test months are refused")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    cfg2 = reporting.load_config(ROOT / "experiments" / "configs" / "e2.yml")
    gas_units = cfg2["gas_units"] + cfg2["hook_overhead_gas"]
    lam_bps, gam_bps = int(round(cfg2["headline"]["lambda"] * 1e4)), int(round(cfg2["headline"]["gamma"] * 1e4))
    tau_old = eA.tau_hat_wei()
    sw = pd.read_csv(RESULTS / "eC" / "eC_swaps_valid.csv.gz", float_precision="round_trip")
    q2sw = pd.read_csv(RESULTS / "eC" / "eC_netting_swaps_valid.csv.gz", float_precision="round_trip")
    calib, cover, summ, cov_sw, checks, per_swap = [], [], [], [], [], []
    for key, var in POOL_VARIANTS:
        pool = POOLS[key]
        R_usd = mg.r_regimes_usd(key, var, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
        # ---- part 1: k = 1 baseline replay of the validation months, d = 1
        C0 = e2.make_configs(cfg2).iloc[[0]].reset_index(drop=True)
        seed = price_seed(key, var, R_usd, EPS_D, 1, e2.PRIOR["valid"], cfg2)
        g = mg.build_grid(key, "valid", var, lags=(0, EPS_D))
        r = e2.simulate(g, pool, C0, cfg2, R_usd, np.nan, EPS_D, 1, seed[["t", "err_usd", "err_signed_usd"]])
        es = eps_series(r, g, seed, 1, EPS_D, cfg2)
        ref_eps = eA.rolling_eps(r, g, seed, 1, EPS_D, cfg2)
        assert np.array_equal(np.nan_to_num(es["eps_S"].to_numpy(), nan=-1), np.nan_to_num(ref_eps, nan=-1)), "eps_S not reproduced"
        checks.append({"pool": key, "variant": var, "check": "generic rolling quantile reproduces eA.rolling_eps (eps_S) exactly",
                       "ok": True, "eps_S_median": float(np.nanmedian(es["eps_S"]))})
        es.assign(pool=key, variant=var).to_csv(OUT / f"eQ4_eps_series_{key}_{var}_valid.csv.gz", index=False)
        bfm = es["bf"].to_numpy() & es["act"].to_numpy()
        P = g["p_ref_0"].to_numpy()
        for name in ("eps_S", "eps_P_abs", "eps_P_rel"):
            v = es.loc[es["act"], name]
            cov = (es.loc[bfm, f"err_{name}"] <= es.loc[bfm, name]).mean()
            calib.append({"pool": key, "variant": var, "quantity": name, "d": EPS_D, "median": float(v.median()),
                          "p10": float(v.quantile(0.1)), "p90": float(v.quantile(0.9)),
                          "median_in_bp_of_price": float((es.loc[es["act"], name] / P[es["act"].to_numpy()]).median() * 1e4)
                          if name == "eps_P_abs" else (float(v.median() * 1e4) if name == "eps_P_rel" else np.nan),
                          "coverage_candidates": float(cov), "n_candidates": int(bfm.sum())})
        # ---- coverage and size on the observed swaps (surplus error vs buffer)
        al = eC.load_swaps(key, var, "valid")
        usd = al["usd_per_num"].to_numpy()
        S = -(al["pi_bench"] * al["amount0"] + al["amount1"]).to_numpy() * usd
        Sh = -(al["pi_hook"] * al["amount0"] + al["amount1"]).to_numpy() * usd
        err = np.abs(Sh - S)
        dx = np.abs(al["amount0"].to_numpy())
        at = lambda col: es.set_index(pd.DatetimeIndex(es["t"]))[col].reindex(pd.DatetimeIndex(al["timestamp"]).floor("min")).to_numpy()  # noqa: E731
        eS, eA_, eR = at("eps_S"), at("eps_P_abs"), at("eps_P_rel")
        buf = {"eps_S": eS, "eps_P_abs": eA_ * dx * usd, "eps_P_rel": eR * al["pi_hook"].to_numpy() * dx * usd}
        arb = al["arb"].to_numpy()
        for name, b in buf.items():
            for sel_name, sel in [("all swaps", np.ones(len(al), bool)), ("arbitrage swaps", arb)]:
                cov_sw.append({"pool": key, "variant": var, "buffer": name, "swaps": sel_name, "n": int(sel.sum()),
                               "coverage": float((err[sel] <= b[sel]).mean()), "buffer_median_usd": float(np.median(b[sel])),
                               "buffer_p90_usd": float(np.quantile(b[sel], 0.9)), "error_median_usd": float(np.median(err[sel])),
                               "error_p95_usd": float(np.quantile(err[sel], 0.95))})
        # ---- part 2: replay the observed swaps under each rule
        to_usd = usd / 10 ** pool.dec1
        s0, s1 = 10 ** pool.dec1, 10 ** pool.dec0
        for setting, df in sw[(sw.pool == key) & (sw.variant == var)].groupby("setting", sort=False):
            df = df.reset_index(drop=True)
            assert (df["block"].to_numpy() == al["block"].to_numpy()).all() and (df["log_index"].to_numpy() == al["log_index"].to_numpy()).all()
            if setting != "delta0":                  # Experiment C's delta is this eps_S at the swap's minute
                assert np.array_equal(df["delta_usd"].to_numpy(), eS), (key, var, "eC delta differs from eps_S")
            if setting == "delta0":
                rules = {"none": ("none", 0.0, None)}
                # check: no buffer, old tau_hat -> Q2's transaction-scope and V1 charges
                kc = np.floor(R_usd / usd * s0)
                res = replay_rule(al, key, kc, np.zeros(len(al)), "none", int(round(tau_old)), gas_units, lam_bps, gam_bps)
                qq = q2sw[(q2sw.pool == key) & (q2sw.variant == var) & (q2sw.setting == "delta0")].reset_index(drop=True)
                ok = all((np.array(res[nm]["r"], dtype=float) * to_usd == qq[col].to_numpy()).all()
                         for nm, col in [("tx", "r_tx_usd"), ("V1", "r_V1_tx_clip_usd"), ("standalone", "r_standalone_usd")])
                assert ok, (key, var, "Q2 not reproduced")
                checks.append({"pool": key, "variant": var, "check": "no buffer, tau_hat 0.05 gwei reproduces Q2 tx / V1 / standalone", "ok": ok})
            else:
                rules = {"fixed": ("none", None, None), "prop_abs": ("abs", 0.0, eA_), "prop_rel": ("rel", 0.0, eR)}
            for rule, (buffer, _, epsv) in rules.items():
                delta = df["delta_usd"].to_numpy() if rule == "fixed" else 0.0
                kc = np.floor((R_usd + delta) / usd * s0)
                if buffer == "abs":
                    eps_int = np.ceil(epsv * s0 / s1 * WAD)
                elif buffer == "rel":
                    eps_int = np.ceil(epsv * WAD)
                else:
                    eps_int = np.zeros(len(al))
                assert np.isfinite(eps_int).all()
                res = replay_rule(al, key, kc, eps_int, buffer, int(TAU_Q4_WEI), gas_units, lam_bps, gam_bps)
                x = swap_frame(df, res, to_usd)
                x["rule"] = rule
                per_swap.append(x[["pool", "variant", "rule", "block", "log_index", "r_tx_usd", "r_V1_usd", "r_standalone_usd"]])
                for sample, sel in [("all swaps", np.ones(len(x), bool)), ("without the 294 ETH block", x["block"].to_numpy() != q2.EVENT_BLOCK)]:
                    for row in q2.metrics(x[sel].reset_index(drop=True), ["tx", "V1"]):
                        summ.append({"pool": key, "variant": var, "primary": (key, var) in eC.PRIMARY, "rule": rule,
                                     "tau_hat_gwei": TAU_Q4_WEI / 1e9, "sample": sample, **row})
                print(key, var, rule, f"{time.time() - t0:.0f}s", flush=True)
    pd.DataFrame(calib).to_csv(OUT / "eQ4_calibration_valid.csv", index=False)
    pd.DataFrame(cov_sw).to_csv(OUT / "eQ4_coverage_swaps_valid.csv", index=False)
    pd.DataFrame(summ).to_csv(OUT / "eQ4_swaps_metrics_valid.csv", index=False)
    pd.DataFrame(checks).to_csv(OUT / "eQ4_checks_swaps_valid.csv", index=False)
    pd.concat(per_swap, ignore_index=True).to_csv(OUT / "eQ4_swaps_charges_valid.csv.gz", index=False)
    outs = sorted(OUT.glob("eQ4_*_valid.csv*"))
    params = {"experiment": "eQ4 parts 1-2", "split": "valid", "tau_hat_wei": TAU_Q4_WEI, "eps_d": EPS_D,
              "eps_quantile": cfg2["eps_quantile"], "eps_window_days": cfg2["eps_window_days"], "rules": RULES,
              "scopes": SCOPES, "lam_bps": lam_bps, "gamma_bps": gam_bps, "kappa_gas_units": gas_units}
    inputs = [RESULTS / "eC" / "eC_swaps_valid.csv.gz", RESULTS / "eC" / "eC_netting_swaps_valid.csv.gz",
              CACHE / "block_gas.parquet", ROOT / "experiments" / "configs" / "e2.yml", Path(__file__).resolve(),
              Path(fp.__file__).resolve(), Path(eC.__file__).resolve(), Path(q2.__file__).resolve()] \
        + [CACHE / "aligned" / f"{k}.parquet" for k in ("eth_usdc_005", "eth_wbtc_030")] \
        + sorted((ROOT / "data" / "data" / "binance").glob("*_1m.csv.gz"))
    reporting.write_manifest("eQ4", params, inputs, "valid", {"parameters": params, "outputs": {p.name: eA.sha(p) for p in outs}},
                             tag="swaps_valid", latest=False)
    for sd in ("tables", "figures"):
        p_ = OUT / sd
        if p_.exists() and not any(p_.iterdir()):
            p_.rmdir()
    with pd.option_context("display.width", 250, "display.max_columns", 40, "display.max_rows", 300):
        print(pd.DataFrame(calib).to_string())
        print(pd.DataFrame(cov_sw).to_string())
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
