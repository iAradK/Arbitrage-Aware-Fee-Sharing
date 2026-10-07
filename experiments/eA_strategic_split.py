#!/usr/bin/env python3
"""Experiment A (post-processing): what a strategic arbitrager avoids under transaction-scoped accounting, and how
often the planned block-scoped accounting would overcharge a later trader. No downloads, no config changes; nothing
under results/e2 or results/e6 is written.

F(a) = min{lam a, (1-gamma) [a - kappa]^+} is evaluated with the integer reference (common/fixedpoint.transfer_wad,
USD amounts in WAD, round down), kappa = K_hat_tx + delta.

Part 1 (transaction scope). A correction with estimated surplus a is cut into l - 1 pieces of kappa - 1 base unit
(each below the charging threshold, so uncharged) plus one remainder that pays F(remainder):
  charge(l) = F(max(a - (l-1)(kappa - 1 unit), 0)),  saving(l) = F(a) - charge(l) - (l-1) c_tx + settlement adjustment,
c_tx = (21,000 + swap gas + hook gas) x gas price x ETH price. Only the piece with a positive charge pays the settlement
gas, so a split whose remainder is uncharged saves the settlement of the unsplit swap. l* = argmax, smallest on ties.
  (a) E6 fragmentation sample (test months, baseline-feasible, with depth): ETH/USDC raw, ETH/WBTC raw and corr24h;
      a = S at the swap's reference; K_hat_tx at the block's base fee + tau_hat (Eq. 9, as e6_cross_tx_split);
      delta = 0 and delta = rolling eps_S at d = 1 (k = 1 baseline replay, value at the swap's minute).
  (b) Executed corrections of two E2 replays (median regime, lam = 0.75, gamma = 0.02 = the E2 headline):
      retained margin at k = 1, d = 0, and buffered delta = eps_S at k = 15, d = 1. Each correction keeps its executed
      size; the path is not re-simulated under splitting. The replays saved only totals, so the frozen simulate() is
      re-executed in memory on the baseline plus that one configuration (outputs asserted equal to the stored E2
      summaries). a is recovered exactly from the executed transfer r > 0 as F^{-1}(r) = max(r/lam, kappa + r/(1-gamma)),
      with the run's own K_hat (180,214 gas at the minute's gas price + R) and its rolling eps_S; r = 0 corrections
      cannot gain from splitting.

Part 2 (block scope). Test-month swaps grouped by (pool, block) in log-index order; reference locked at the first swap
of the block; A_prev = signed sum of the earlier swaps' estimated surplus, W_prev their watermark;
  overcharge_j = max([F(A_prev + a_j)]^+ - W_prev, 0) - F(a_j), floored at 0, for each price-correcting swap j.
Proposition 3 check: overcharge_j <= (1-gamma) kappa.

Part 3. Partial corrections (still deviated by more than the fee afterwards): is the next price-correcting swap from
a different tx.origin within 1 or 2 blocks?

  python experiments/eA_strategic_split.py
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

import e2_sequential_replay as e2  # noqa: E402
import e6_fragmentation as e6  # noqa: E402
from e6_cross_tx_split import best_two_tx_saving  # noqa: E402
from common import fixedpoint as fp, minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS, REPO, RESULTS  # noqa: E402

OUT = RESULTS / "eA"
GDIR = RESULTS / "e7" / "gas_contract_8ca840a"
INTRINSIC_GAS = 21_000
WAD = fp.WAD
POOL_VARIANTS = [("eth_usdc_005", "raw"), ("eth_wbtc_030", "raw"), ("eth_wbtc_030", "corr24h"),
                 ("eth_wsteth_001", "raw"), ("eth_wsteth_001", "corr24h")]
SAMPLE_VARIANTS = [("eth_usdc_005", "raw"), ("eth_wbtc_030", "raw"), ("eth_wbtc_030", "corr24h")]   # Section 5.4.2
EPS_D = 1                                    # buffered delta = rolling eps_S at d = 1
REPLAYS = {  # setting -> (mechanism, cadence k, delay d, stored summary tag)
    "ideal_retained_k1_d0": ("retained", 1, 0, "test_lag0_med"),
    "buffered_1eps_k15_d1": ("buffered_1eps", 15, 1, "test_lag1_k15_med"),
}


# ------------------------------------------------------------------ constants
def gas_constants() -> dict:
    iso = pd.read_csv(GDIR / "hook_gas_isolated.csv")
    swap = int(iso[(iso.n == 1) & (iso.hook == 0)].gas.iloc[0])
    hook = int(iso[(iso.n == 1) & (iso.hook == 1)].gas.iloc[0]) - swap
    st = pd.read_csv(GDIR / "hook_gas_settled.csv").set_index("case")["gas"]
    settle = int(st["charged_hook"] - st["uncharged_hook"])
    return {"swap_gas": swap, "hook_gas": hook, "settle_gas": settle, "extra_tx_gas": INTRINSIC_GAS + swap + hook,
            "source": "results/e7/gas_contract_8ca840a/hook_gas_isolated.csv (n=1, hook=0: cold swap, Foundry test pool; "
                      "hook = (n=1, hook=1) - swap) and hook_gas_settled.csv (charged_hook - uncharged_hook)"}


def tau_hat_wei() -> float:
    """Training-median priority fee of the fee-history blocks (as e6_cross_tx_split / e7 smin)."""
    b = pd.read_parquet(CACHE / "block_gas.parquet", columns=["timestamp", "tip_p50_wei", "gas_source"])
    train = (pd.DatetimeIndex(b["timestamp"]) < mg.VALID_START) & (b["gas_source"] == "fee_history").to_numpy()
    return float(b.loc[train, "tip_p50_wei"].median())


def unit_usd(pool_key: str, usd_per_num) -> np.ndarray:
    """One base unit of the numeraire token (the pool's quote token; token0 in the hook's convention) in USD."""
    return 10.0 ** (-POOLS[pool_key].dec1) * np.asarray(usd_per_num, dtype=float)


class Fint:
    """Integer F on USD WAD amounts: transfer_wad(a, kappa, lam, gamma, delta = 0), kappa already includes delta."""

    def __init__(self, lam: float, gam: float):
        self.lam, self.gam = lam, gam
        self.lw, self.gw = fp.to_wad(lam), fp.to_wad(gam)

    def __call__(self, a_w: int, k_w: int) -> int:
        return fp.transfer_wad(max(int(a_w), 0), int(k_w), self.lw, self.gw, 0)[0]

    def cap(self, k_w: int) -> int:                  # (1-gamma) kappa, Proposition 3 bound
        return fp.mul_wad_down(int(k_w), WAD - self.gw)


# ------------------------------------------------------------------ Part 1
def split_one(a_usd: float, k_usd: float, u_usd: float, ctx_usd: float, settle_usd: float, F: Fint) -> dict:
    a_w, k_w = fp.to_wad(max(a_usd, 0.0)), fp.to_wad(k_usd)
    u_w = max(1, fp.to_wad(u_usd))
    piece = k_w - u_w
    assert piece > 0, "kappa below one base unit"
    F_a = F(a_w, k_w)
    out = {"F_a_w": F_a, "l_star": 1, "charge_star_w": F_a, "saving_star_usd": 0.0}
    if F_a == 0:                                     # nothing to avoid; a split only costs
        out.update(charge2_w=0, saving2_usd=-ctx_usd, l_zero=1)
        return out
    l_max = -(-a_w // k_w) + 1                        # ceil(a / kappa) + 1
    best, l_best, ch_best, l_zero = 0.0, 1, F_a, None
    for l in range(2, l_max + 1):
        ch = F(max(a_w - (l - 1) * piece, 0), k_w)
        sav = (F_a - ch) / WAD - (l - 1) * ctx_usd + (settle_usd if ch == 0 else 0.0)
        if l == 2:
            out.update(charge2_w=ch, saving2_usd=sav)
        if sav > best + 1e-12:
            best, l_best, ch_best = sav, l, ch
        if ch == 0:                                   # further pieces only add cost
            l_zero = l
            break
    out.update(l_star=l_best, charge_star_w=ch_best, saving_star_usd=best, l_zero=l_zero if l_zero else np.nan)
    return out


def run_split(df: pd.DataFrame, F: Fint, gas: dict) -> pd.DataFrame:
    """df: a_usd, kappa_usd, unit_usd, gas_price_wei, eth_usd."""
    ctx = gas["extra_tx_gas"] * df["gas_price_wei"].to_numpy() * 1e-18 * df["eth_usd"].to_numpy()
    st = gas["settle_gas"] * df["gas_price_wei"].to_numpy() * 1e-18 * df["eth_usd"].to_numpy()
    rows = [split_one(a, k, u, c, s, F) for a, k, u, c, s in
            zip(df["a_usd"].to_numpy(), df["kappa_usd"].to_numpy(), df["unit_usd"].to_numpy(), ctx, st)]
    r = pd.DataFrame(rows, index=df.index)
    out = df.copy()
    out["c_tx_usd"], out["settle_usd"] = ctx, st
    out["F_a_usd"] = r["F_a_w"].astype(float) / WAD
    out["charge_star_usd"] = r["charge_star_w"].astype(float) / WAD
    out["charge2_usd"] = r["charge2_w"].astype(float) / WAD
    out["l_star"], out["l_zero"] = r["l_star"].astype(int), r["l_zero"]
    out["saving_star_usd"], out["saving2_usd"] = r["saving_star_usd"], r["saving2_usd"]
    # best general two-transaction split (breakpoints of Proposition prop:cross-tx, float), as a cross-check of l = 2
    a, k = np.maximum(df["a_usd"].to_numpy(float), 0.0), df["kappa_usd"].to_numpy(float)
    s2, _ = best_two_tx_saving(a, k, F.lam, F.gam) if len(a) else (np.zeros(0), None)
    out["best2_general_gross_saving_usd"] = s2
    return out


def summarize_split(c: pd.DataFrame, setting: str, pool: str, variant: str, extra: dict) -> dict:
    q = lambda x, p: float(np.quantile(x, p)) if len(x) else np.nan  # noqa: E731
    ch = c["F_a_usd"] > 0
    sp = c["l_star"] > 1
    tot = c["F_a_usd"].sum()
    Fch = c.loc[ch]
    return {"setting": setting, "pool": pool, "variant": variant, **extra,
            "n_corrections": len(c), "n_charged": int(ch.sum()),
            "kappa_median_usd": q(c["kappa_usd"], 0.5), "c_tx_median_usd": q(c["c_tx_usd"], 0.5),
            "original_usd": tot, "strategic_usd": c["charge_star_usd"].sum(), "l2_usd": c["charge2_usd"].sum(),
            "surviving_share": c["charge_star_usd"].sum() / tot if tot > 0 else np.nan,
            "surviving_share_l2": c["charge2_usd"].sum() / tot if tot > 0 else np.nan,
            "surviving_share_l2_bestsplit": (tot - c["best2_general_gross_saving_usd"].sum()) / tot if tot > 0 else np.nan,
            "share_split_pays": float(sp.mean()) if len(c) else np.nan,
            "share_split_pays_of_charged": float(sp[ch].mean()) if ch.any() else np.nan,
            "share_l2_pays_of_charged": float((Fch["saving2_usd"] > 0).mean()) if ch.any() else np.nan,
            "net_saving_median_usd_charged": q(Fch["saving_star_usd"], 0.5), "net_saving_p95_usd_charged": q(Fch["saving_star_usd"], 0.95),
            "net_saving_median_usd_splitters": q(c.loc[sp, "saving_star_usd"], 0.5), "net_saving_p95_usd_splitters": q(c.loc[sp, "saving_star_usd"], 0.95),
            "net_saving_total_usd": c["saving_star_usd"].sum(),
            "l_star_median_charged": q(Fch["l_star"], 0.5), "l_star_p95_charged": q(Fch["l_star"], 0.95),
            "l_star_max": int(c["l_star"].max()) if len(c) else np.nan,
            "l_star_median_splitters": q(c.loc[sp, "l_star"], 0.5), "l_star_p95_splitters": q(c.loc[sp, "l_star"], 0.95)}


# ------------------------------------------------------------------ rolling eps_S (replicates e2.simulate)
def rolling_eps(r: dict, g: pd.DataFrame, seed: pd.DataFrame, k: int, d: int, cfg2: dict) -> np.ndarray:
    """eps_S at each action step, exactly as simulate(): P95 of |S_hat - S| over baseline candidates strictly earlier
    within eps_window_days (seed of the preceding split first), all history if fewer than 20."""
    cand = r["cand"]
    tt = pd.DatetimeIndex(g["t"]).tz_convert(None).values.astype("datetime64[ns]")
    st = pd.DatetimeIndex(seed["t"])
    st = st.tz_convert(None) if st.tz is not None else st
    cb = cand["bf"].to_numpy(bool)
    ht = np.concatenate([st.values.astype("datetime64[ns]"), tt[cb]])
    he = np.concatenate([seed["err_usd"].to_numpy(float), (cand["S_hat_usd"] - cand["S_usd"]).abs().to_numpy(float)[cb]])
    assert np.all(np.diff(ht.astype(np.int64)) >= 0)
    W = np.timedelta64(int(cfg2["eps_window_days"] * 86400e9), "ns")
    ok = np.isfinite(g["p_ref_0"].to_numpy()) & np.isfinite(g[f"p_ref_{d}"].to_numpy()) & np.isfinite(g["L"].to_numpy()) \
        & np.isfinite(g["gas_wei"].to_numpy())
    act = ok & (np.arange(len(g)) % k == 0)
    eps = np.full(len(g), np.nan)
    tq = cfg2["eps_quantile"]
    for i in np.flatnonzero(act):
        hi = int(np.searchsorted(ht, tt[i], "left"))
        lo = min(int(np.searchsorted(ht, tt[i] - W, "left")), hi)
        win = he[lo:hi]
        eps[i] = float(np.quantile(win, tq)) if len(win) >= 20 else (float(np.quantile(he[:hi], tq)) if hi else 0.0)
    return eps


def run_replay(pool_key: str, variant: str, mech: str, k: int, d: int, cfg2: dict, lam: float, gam: float):
    """Re-execute the frozen E2 simulate() on [baseline, target configuration]; returns (result, grid, eps, R, cfg row)."""
    C_all = e2.make_configs(cfg2)
    j = C_all.index[(C_all["mech"] == mech) & (C_all["lam"] == lam) & (C_all["gamma"] == gam)][0]
    C = C_all.loc[[0, j]].reset_index(drop=True)
    R_usd = mg.r_regimes_usd(pool_key, variant, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
    seed = e2.eps_seed(pool_key, variant, R_usd, d, k, e2.PRIOR["test"], cfg2)
    g = mg.build_grid(pool_key, "test", variant, lags=(0, d))
    r = e2.simulate(g, POOLS[pool_key], C, cfg2, R_usd, np.nan, d, k, seed)
    eps = rolling_eps(r, g, seed, k, d, cfg2)
    return r, g, eps, R_usd, C


def eps_series(pool_key: str, variant: str, cfg2: dict) -> tuple[pd.Series, dict]:
    """Rolling eps_S at d = EPS_D, k = 1 (baseline replay of the test months), indexed by grid minute."""
    C0 = e2.make_configs(cfg2).iloc[[0]].reset_index(drop=True)
    R_usd = mg.r_regimes_usd(pool_key, variant, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
    seed = e2.eps_seed(pool_key, variant, R_usd, EPS_D, 1, e2.PRIOR["test"], cfg2)
    g = mg.build_grid(pool_key, "test", variant, lags=(0, EPS_D))
    r = e2.simulate(g, POOLS[pool_key], C0, cfg2, R_usd, np.nan, EPS_D, 1, seed)
    eps = rolling_eps(r, g, seed, 1, EPS_D, cfg2)
    stored = pd.read_parquet(RESULTS / "e2" / f"e2_summary_test_lag{EPS_D}_med.parquet")
    stored = stored[(stored.pool == pool_key) & (stored.variant == variant) & (stored.mech == "baseline")]
    chk = {"pool": pool_key, "variant": variant, "check": f"rolling eps_S median, d={EPS_D}, k=1",
           "recomputed": float(np.nanmedian(eps)), "simulate_table": float(r["table"]["eps_S_used_median_usd"].iloc[0]),
           "stored": float(stored["eps_S_usd"].iloc[0])}
    assert np.isclose(chk["recomputed"], chk["simulate_table"], rtol=1e-12, atol=1e-12), chk
    assert np.isclose(chk["recomputed"], chk["stored"], rtol=1e-9, atol=1e-9), chk
    return pd.Series(eps, index=pd.DatetimeIndex(g["t"])), chk


def eps_at(series: pd.Series, ts) -> np.ndarray:
    """eps_S of the grid minute containing ts (computed from candidates strictly before that minute)."""
    return series.reindex(pd.DatetimeIndex(ts).floor("min")).to_numpy()


# ------------------------------------------------------------------ Part 2 / 3 inputs
def load_swaps(pool_key: str, variant: str, cfg6: dict, tau: float) -> pd.DataFrame:
    al = mg.load_aligned(pool_key)
    al = al[al["split"] == "test"].copy()
    raw = pd.read_csv(POOLS[pool_key].swaps_path, usecols=["id", "sender", "origin"])
    al["id"] = al["tx_hash"] + "-" + al["log_index"].astype(str)
    al = al.merge(raw, on="id", how="left")
    w = mg.VARIANTS[variant]
    off = np.nan_to_num(al[f"offset_{w}"].to_numpy(), nan=0.0) if w else 0.0
    al["pi"] = al["p_ref"] * np.exp(off)
    thr = cfg6["gap_multiple"][pool_key] * POOLS[pool_key].fee
    pre, post = np.log(al["price_pre"] / al["pi"]).abs(), np.log(al["price"] / al["pi"]).abs()
    al["pc"] = (pre > thr) & (post < pre)                      # price-correcting (E6 / Section 5.4.2 definition)
    al["partial"] = al["pc"] & (post > thr)
    return al.sort_values(["block", "log_index"]).reset_index(drop=True)


def part2(al: pd.DataFrame, pool_key: str, variant: str, R_usd: float, eps: pd.Series, tau: float, cfg6: dict, F: Fint,
          setting: str) -> pd.DataFrame:
    gas_units = cfg6["gas_units"] + cfg6["hook_overhead_gas"]
    al = al.copy()
    al["kappa0_usd"] = gas_units * (al["base_fee_wei"] + tau) * 1e-18 * al["eth_usd"] + R_usd
    al["delta_usd"] = 0.0 if setting == "delta0" else eps_at(eps, al["timestamp"])
    blocks = al.loc[al["pc"], "block"].unique()
    sub = al[al["block"].isin(blocks)]
    rows = []
    for b, grp in sub.groupby("block", sort=False):
        pi_lock = grp["pi"].iloc[0]                                      # reference locked at the first swap of the block
        s_usd = (-(pi_lock * grp["amount0"].to_numpy() + grp["amount1"].to_numpy()) * grp["usd_per_num"].to_numpy())
        kap = grp["kappa0_usd"].iloc[0] + grp["delta_usd"].iloc[0]
        if not np.isfinite(kap):
            continue
        k_w = fp.to_wad(kap)
        cum, W = 0, 0                                                    # signed cumulative surplus (WAD), watermark
        origins, txs = grp["origin"].to_numpy(), grp["tx_hash"].to_numpy()
        pos_prev, orig_prev, tx_prev = False, set(), set()
        for i, (s, pc) in enumerate(zip(s_usd, grp["pc"].to_numpy())):
            s_w = int(round(s * WAD))
            if pc:
                a_w = max(s_w, 0)
                F_own = F(a_w, k_w)
                ch_block = max(F(max(cum + s_w, 0), k_w) - W, 0)
                over = max(ch_block - F_own, 0)
                margin = max(a_w - k_w, 0)
                rows.append({"pool": pool_key, "variant": variant, "setting": setting, "block": int(b),
                             "log_index": int(grp["log_index"].iloc[i]), "pos_in_block": i, "n_in_block": len(grp),
                             "origin": origins[i], "a_usd": s, "A_prev_usd": cum / WAD, "W_prev_usd": W / WAD, "kappa_usd": kap,
                             "F_own_usd": F_own / WAD, "charge_block_usd": ch_block / WAD, "overcharge_usd": over / WAD,
                             "undercharge_usd": max(F_own - ch_block, 0) / WAD,
                             "margin_usd": margin / WAD, "remaining_margin_usd": (margin - F_own) / WAD,
                             "preceded_pos": pos_prev,
                             "prev_other_origin": any(o != origins[i] for o in orig_prev),
                             "prev_same_tx": txs[i] in tx_prev,
                             "violation_spec": over > margin, "violation_total": over > margin - F_own,
                             "prop3_bound_usd": F.cap(k_w) / WAD, "prop3_violation": over > F.cap(k_w) + 2})
            cum += s_w
            W = max(W, F(max(cum, 0), k_w))
            pos_prev |= s_w > 0
            orig_prev.add(origins[i])
            tx_prev.add(txs[i])
    return pd.DataFrame(rows)


def summarize_part2(p: pd.DataFrame, by: str | None = None) -> pd.DataFrame:
    def one(x: pd.DataFrame) -> dict:
        o = x["overcharge_usd"]
        pos = o[o > 0]
        q = lambda v, pp: float(np.quantile(v, pp)) if len(v) else np.nan  # noqa: E731
        return {"n_pc_swaps": len(x), "share_preceded_pos": x["preceded_pos"].mean(),
                "n_overcharged": int((o > 0).sum()), "share_overcharged": (o > 0).mean(),
                "overcharge_median_usd_pos": q(pos, 0.5), "overcharge_p95_usd_pos": q(pos, 0.95),
                "overcharge_max_usd": float(o.max()) if len(o) else np.nan,
                "overcharge_median_usd_all": q(o, 0.5), "overcharge_p95_usd_all": q(o, 0.95), "overcharge_total_usd": o.sum(),
                "share_violation_spec": x["violation_spec"].mean(), "n_violation_spec": int(x["violation_spec"].sum()),
                "share_violation_total": x["violation_total"].mean(), "n_violation_total": int(x["violation_total"].sum()),
                "n_prop3_violation": int(x["prop3_violation"].sum()),
                "max_overcharge_over_bound": float((o / x["prop3_bound_usd"]).max()) if len(o) else np.nan,
                "n_undercharged": int((x["undercharge_usd"] > 0).sum()), "kappa_median_usd": q(x["kappa_usd"], 0.5)}
    keys = ["pool", "variant", "setting"] + ([by] if by else [])
    return pd.DataFrame([{**dict(zip(keys, k if isinstance(k, tuple) else (k,))), **one(x)} for k, x in p.groupby(keys, sort=False)])


def part3(al: pd.DataFrame, pool_key: str, variant: str) -> dict:
    pc = al[al["pc"]].reset_index(drop=True)
    nxt_block, nxt_origin = pc["block"].shift(-1), pc["origin"].shift(-1)
    m = pc["partial"].to_numpy() & nxt_block.notna().to_numpy()
    gap = (nxt_block - pc["block"])[m]
    diff = (nxt_origin != pc["origin"])[m]
    n = int(pc["partial"].sum())
    out = {"pool": pool_key, "variant": variant, "n_pc_swaps": len(pc), "n_partial": n, "n_partial_with_next": int(m.sum())}
    for lbl, sel in [("same_block", gap == 0), ("within_1", gap <= 1), ("within_2", gap <= 2)]:
        out[f"share_next_{lbl}"] = float(sel.sum()) / n if n else np.nan
        out[f"share_next_{lbl}_other_origin"] = float((sel & diff).sum()) / n if n else np.nan
        out[f"share_next_{lbl}_same_origin"] = float((sel & ~diff).sum()) / n if n else np.nan
    return out


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ main
def main():
    t0 = time.time()
    cfg2 = reporting.load_config(ROOT / "experiments" / "configs" / "e2.yml")
    cfg6 = reporting.load_config(ROOT / "experiments" / "configs" / "e6.yml")
    reporting.guard_split("e2", "test", True, cfg2)          # read-only: configs equal the frozen ones of the source runs
    reporting.guard_split("e6", "test", True, cfg6)
    OUT.mkdir(parents=True, exist_ok=True)
    gas, tau = gas_constants(), tau_hat_wei()
    lam2, gam2 = cfg2["headline"]["lambda"], cfg2["headline"]["gamma"]
    F6, F2 = Fint(cfg6["lam"], cfg6["gamma"]), Fint(lam2, gam2)
    checks = []
    print("gas", gas, "tau_hat", tau, flush=True)

    # rolling eps_S at d = 1 (k = 1) per pool and variant
    eps = {}
    for key, var in POOL_VARIANTS:
        eps[(key, var)], chk = eps_series(key, var, cfg2)
        checks.append(chk)
        print("eps", key, var, round(chk["recomputed"], 4), f"{time.time() - t0:.0f}s", flush=True)

    # ---------------- Part 1(a): E6 fragmentation sample
    corr, summ = [], []
    for key, var in SAMPLE_VARIANTS:
        R_usd = mg.r_regimes_usd(key, var, cfg6["gas_units"], cfg6["r_quantiles"])[cfg6["r_regime"]]
        c_all, _ = e6.all_candidates(key, var, "test", cfg6, R_usd)
        sam = c_all[(c_all["n_used"] > 0) & (c_all["S"] - c_all["C"] >= c_all["R"])].reset_index(drop=True)
        k0 = (cfg6["gas_units"] + cfg6["hook_overhead_gas"]) * (sam["base_fee_wei"] + tau) * 1e-18 * sam["eth_usd"] + R_usd
        for setting in ["delta0", f"delta_eps_d{EPS_D}"]:
            dl = 0.0 if setting == "delta0" else eps_at(eps[(key, var)], sam["timestamp"])
            df = pd.DataFrame({"timestamp": sam["timestamp"], "block": sam["block"], "a_usd": sam["S_usd"],
                               "kappa_usd": k0 + dl, "delta_usd": dl, "unit_usd": unit_usd(key, sam["usd_per_num"]),
                               "gas_price_wei": sam["gas_price_wei"], "eth_usd": sam["eth_usd"]})
            assert df["kappa_usd"].notna().all(), (key, var, setting)
            c = run_split(df, F6, gas)
            c.insert(0, "source", "e6_sample")
            c.insert(1, "setting", setting)
            c.insert(2, "pool", key)
            c.insert(3, "variant", var)
            corr.append(c)
            summ.append(summarize_split(c, f"e6_sample_{setting}", key, var,
                                        {"lam": cfg6["lam"], "gamma": cfg6["gamma"], "R_usd": R_usd, "delta_median_usd": float(np.median(dl))}))
        print("part1a", key, var, len(sam), f"{time.time() - t0:.0f}s", flush=True)

    # ---------------- Part 1(b): executed corrections of two E2 replays
    for setting, (mech, k, d, tag) in REPLAYS.items():
        stored = pd.read_parquet(RESULTS / "e2" / f"e2_summary_{tag}.parquet")
        for key, var in POOL_VARIANTS:
            r, g, ep, R_usd, C = run_replay(key, var, mech, k, d, cfg2, lam2, gam2)
            st = stored[(stored.pool == key) & (stored.variant == var) & (stored.regime == "median") & (stored.mech == mech)
                        & (stored.lam == lam2) & (stored.gamma == gam2)].iloc[0]
            tab = r["table"].iloc[1]
            chk = {"pool": key, "variant": var, "check": f"{setting}: protection_usd / n_executed",
                   "recomputed": float(tab["protection_usd"]), "stored": float(st["protection_usd"]),
                   "n_exec_recomputed": int(tab["n_executed"]), "n_exec_stored": int(st["n_executed"])}
            assert np.isclose(chk["recomputed"], chk["stored"], rtol=1e-12, atol=1e-9) and chk["n_exec_recomputed"] == chk["n_exec_stored"], chk
            ex = r["step_exec"][:, 1]
            rr = r["step_protection"][ex, 1].astype(float)
            gx = g.loc[ex].reset_index(drop=True)
            dm = float(C["dmult"].iloc[1])
            usd = gx["usd_per_num"].to_numpy()
            Cst_usd = mg.gas_cost_num(gx["gas_wei"].to_numpy(), gx["eth_in_num"].to_numpy(), float(C["gas"].iloc[1])) * usd
            dl = ep[ex] * dm
            assert np.isfinite(dl).all()
            kap = Cst_usd + R_usd + dl
            a = np.where(rr > 0, np.maximum(rr / lam2, kap + rr / (1 - gam2)), 0.0)    # F^{-1}(r); r = 0 -> no charge
            fl = np.minimum(lam2 * a, (1 - gam2) * np.maximum(a - kap, 0.0))
            assert np.allclose(fl, rr, rtol=1e-5, atol=1e-6), "inversion of the executed transfer failed"
            checks.append(chk)
            df = pd.DataFrame({"timestamp": gx["t"], "a_usd": a, "kappa_usd": kap, "delta_usd": dl, "r_executed_usd": rr,
                               "unit_usd": unit_usd(key, usd), "gas_price_wei": gx["gas_wei"].to_numpy(),
                               "eth_usd": gx["eth_in_num"].to_numpy() * usd})
            c = run_split(df, F2, gas)
            c.insert(0, "source", "e2_replay")
            c.insert(1, "setting", setting)
            c.insert(2, "pool", key)
            c.insert(3, "variant", var)
            corr.append(c)
            s = summarize_split(c, setting, key, var, {"lam": lam2, "gamma": gam2, "R_usd": R_usd,
                                                       "delta_median_usd": float(np.median(dl)) if len(dl) else np.nan})
            s["stored_total_usd"] = chk["stored"]
            summ.append(s)
            print("part1b", setting, key, var, int(ex.sum()), round(s["surviving_share"], 4), f"{time.time() - t0:.0f}s", flush=True)

    corr_df = pd.concat(corr, ignore_index=True)
    summ_df = pd.DataFrame(summ)
    corr_df.to_csv(OUT / "eA_part1_corrections.csv.gz", index=False)
    summ_df.to_csv(OUT / "eA_part1_summary.csv", index=False)

    # ---------------- Part 2 and Part 3
    p2, p3 = [], []
    for key, var in POOL_VARIANTS:
        R_usd = mg.r_regimes_usd(key, var, cfg6["gas_units"], cfg6["r_quantiles"])[cfg6["r_regime"]]
        al = load_swaps(key, var, cfg6, tau)
        miss = float(al["origin"].isna().mean())
        checks.append({"pool": key, "variant": var, "check": "swaps without sender/origin after join", "recomputed": miss})
        for setting in ["delta0", f"delta_eps_d{EPS_D}"]:
            p2.append(part2(al, key, var, R_usd, eps[(key, var)], tau, cfg6, F6, setting))
        p3.append(part3(al, key, var))
        print("part2/3", key, var, int(al["pc"].sum()), f"{time.time() - t0:.0f}s", flush=True)
    p2_df = pd.concat(p2, ignore_index=True)
    p2_df.to_csv(OUT / "eA_part2_swaps.csv.gz", index=False)
    s2 = summarize_part2(p2_df)
    s2.to_csv(OUT / "eA_part2_summary.csv", index=False)
    pp = p2_df[p2_df["preceded_pos"]].copy()
    pp["prev_origin"] = np.where(pp["prev_other_origin"], "different", "same")
    s2s = summarize_part2(pp, "prev_origin")
    s2s.to_csv(OUT / "eA_part2_by_sender.csv", index=False)
    p3_df = pd.DataFrame(p3)
    p3_df.to_csv(OUT / "eA_part3_summary.csv", index=False)
    ck = pd.DataFrame(checks)
    ck.to_csv(OUT / "eA_checks.csv", index=False)

    # ---------------- manifest
    params = {"experiment": "eA", "lam_e6": cfg6["lam"], "gamma_e6": cfg6["gamma"], "lam_e2": lam2, "gamma_e2": gam2,
              "R_regime": "median", "eps_quantile": cfg2["eps_quantile"], "eps_window_days": cfg2["eps_window_days"],
              "eps_delay_min": EPS_D, "khat_gas_units": cfg6["gas_units"] + cfg6["hook_overhead_gas"], "tau_hat_wei": tau,
              **{k_: v for k_, v in gas.items()}, "intrinsic_gas": INTRINSIC_GAS,
              "replays": {s_: {"mech": m_, "k": k_, "d": d_, "stored_tag": t_} for s_, (m_, k_, d_, t_) in REPLAYS.items()},
              "pool_variants": POOL_VARIANTS, "sample_variants": SAMPLE_VARIANTS,
              "config_sha256": {"e2.yml": reporting.config_hash(cfg2), "e6.yml": reporting.config_hash(cfg6)}}
    inputs = ([CACHE / "aligned" / f"{k_}.parquet" for k_ in POOLS] + [POOLS[k_].swaps_path for k_ in POOLS]
              + [RESULTS / "e1" / f"depth_{k_}.parquet" for k_ in POOLS] + [CACHE / "block_gas.parquet"]
              + [GDIR / "hook_gas_isolated.csv", GDIR / "hook_gas_settled.csv"]
              + [RESULTS / "e2" / f"e2_summary_{t_}.parquet" for *_, t_ in REPLAYS.values()]
              + [RESULTS / "e2" / f"e2_summary_test_lag{EPS_D}_med.parquet", reporting.frozen_path("e2"),
                 reporting.frozen_path("e6"), ROOT / "experiments" / "configs" / "e2.yml",
                 ROOT / "experiments" / "configs" / "e6.yml", Path(__file__).resolve()])
    reporting.write_manifest("eA", params, inputs, "test",
                             {"outputs": {p.name: sha(p) for p in sorted(OUT.glob("eA_*"))}, "parameters": params,
                              "note": "E2 replays re-executed in memory (frozen simulate, baseline + one configuration) to "
                                      "recover per-correction transfers; totals asserted equal to the stored summaries"})
    # write_manifest also creates tables/ and figures/ subdirectories; remove them if empty
    for sd in ("tables", "figures"):
        p_ = OUT / sd
        if p_.exists() and not any(p_.iterdir()):
            p_.rmdir()
    with pd.option_context("display.width", 250, "display.max_columns", 60):
        print(summ_df.T.to_string())
        print(s2.T.to_string())
        print(s2s.T.to_string())
        print(p3_df.T.to_string())
        print(ck.to_string())
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
