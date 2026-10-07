#!/usr/bin/env python3
"""E2 validation months with the block-scoped hook's gas (config/gas_block_scope.json).

Every correction of the replay is the first swap of its block (one action per minute and configuration), so it pays
the block-scoped overhead of a block's first swap: 34,559 gas when uncharged, 48,177 when charged (r > 0, settlement
included). The hook's g_hat is the charged amount, as in DECISIONS I3 (K_hat matters only when the hook charges).
Sensitivity: 65,277 for charged corrections (the vault holds none of the charged token). Everything else is E2 as
frozen: experiments/e2_sequential_replay.py simulate(), config hash = results/e2/frozen_config.sha256, median
reservation regime, rolling eps_S. Two runs, as stored:
  ideal     retained margin, hook lag 0, cadence 1    tag valid_lag0_med
  buffered  delta = eps_S (buffered_1eps), lag 1, cadence 15    tag valid_lag1_k15_med

Modes
  asfrozen             reruns the stored configuration (30,214 gas for every correction) and asserts that every row
                       equals the stored e2_summary_<tag>.parquet; writes only e2_asfrozen_check_<tag>_blockgas.json
  blockgas             charged corrections pay 48,177 (writes *_<tag>_blockgas.*)
  blockgas_emptyvault  charged corrections pay 65,277 (writes *_<tag>_blockgas_emptyvault.*)
  compare              relative changes against the stored results -> tables/e2_blockgas_comparison_valid.*

Validation months only; stored files are never overwritten.

  python experiments/e2_blockgas.py --mode asfrozen --lag 0 --cadence 1
  python experiments/e2_blockgas.py --mode blockgas --lag 1 --cadence 15
  python experiments/e2_blockgas.py --mode compare
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

import e2_sequential_replay as e2  # noqa: E402
from common import minutegrid as mg, reporting  # noqa: E402
from common.pools import CACHE, POOLS, RESULTS  # noqa: E402

SPLIT = "valid"
OUT = RESULTS / "e2"
GAS_CFG = ROOT / "config" / "gas_block_scope.json"
SUFFIX = {"blockgas": "blockgas", "blockgas_emptyvault": "blockgas_emptyvault"}
RUNS = {"ideal": ("valid_lag0_med", "retained", 0.0), "buffered": ("valid_lag1_k15_med", "buffered_1eps", 1.0)}
KEYS = ["pool", "variant", "regime", "mech", "lam", "gamma", "dmult", "phi"]


def tag_of(d: int, k: int) -> str:
    return f"{SPLIT}_lag{d}" + (f"_k{k}" if k != 1 else "") + "_med"


def load_cfg() -> dict:
    cfg = reporting.load_config(e2.CFG)
    frozen = (OUT / "frozen_config.sha256").read_text().strip()
    assert reporting.config_hash(cfg) == frozen, "experiments/configs/e2.yml is not the frozen E2 config"
    return cfg


def configs(cfg: dict, mode: str) -> tuple[dict, pd.DataFrame, dict]:
    if mode == "asfrozen":
        return cfg, e2.make_configs(cfg), {}
    gas = json.loads(GAS_CFG.read_text())
    ov = gas["hook_overhead_gas"]
    charged = ov["charged_swap"] if mode == "blockgas" else ov["charged_swap_empty_vault_sensitivity"]
    cfg2 = {**cfg, "hook_overhead_gas": ov["first_swap_of_block"]}     # every uncharged correction, the baseline, eps_S
    C = e2.make_configs(cfg2)
    nohook = (C["mech"] == "baseline_nohook").to_numpy()
    g_ch = float(cfg["gas_units"] + charged)
    C["gas_ch"] = np.where(nohook, C["gas"], g_ch)
    C["ghat"] = np.where(nohook, C["gas"], g_ch)
    info = {"hook_gas_uncharged": ov["first_swap_of_block"], "hook_gas_charged": charged, "ghat_gas": g_ch,
            "gas_config": str(GAS_CFG.relative_to(ROOT)), "gas_config_source": gas["source"]}
    return cfg2, C, info


def run_one(args) -> dict:
    key, variant, mode, d, k = args
    cfg = load_cfg()
    cfg2, C, _ = configs(cfg, mode)
    R_usd = mg.r_regimes_usd(key, variant, cfg["gas_units"], cfg["r_quantiles"])["median"]   # median regime, as stored
    assert cfg["eps_mode"] == "rolling"
    seed = e2.eps_seed(key, variant, R_usd, d, k, e2.PRIOR[SPLIT], cfg2)
    g = mg.build_grid(key, SPLIT, variant, lags=(0, d))
    r = e2.simulate(g, POOLS[key], C, cfg2, R_usd, np.nan, d, k, seed)
    t = r["table"]
    t.insert(0, "regime", "median")
    t.insert(0, "variant", variant)
    t.insert(0, "pool", key)
    t["R_usd"], t["eps_S_usd"], t["hook_lag_min"], t["cadence_min"], t["eps_mode"] = (
        R_usd, t["eps_S_used_median_usd"].iloc[0], d, k, cfg["eps_mode"])
    dl = r["daily"].merge(C[["mech", "lam", "gamma", "dmult", "phi"]].reset_index().rename(columns={"index": "cfg"}), on="cfg")
    for c_, v_ in (("regime", "median"), ("variant", variant), ("pool", key)):
        dl.insert(0, c_, v_)
    nhk = None
    if r["nohook"] is not None:
        nhk = r["nohook"][r["nohook"]["act"]].drop(columns="act")
        for c_, v_ in (("R_usd", R_usd), ("regime", "median"), ("variant", variant), ("pool", key)):
            nhk.insert(0, c_, v_)
    return {"table": t, "daily": dl, "nohook": nhk}


def simulate_all(mode: str, d: int, k: int) -> dict:
    cfg = load_cfg()
    jobs = [(key, v, mode, d, k) for key in cfg["pools"] for v in cfg["variants"][key]]
    with ProcessPoolExecutor(max_workers=len(jobs)) as ex:
        res = list(ex.map(run_one, jobs))
    out = {"table": pd.concat([r["table"] for r in res], ignore_index=True),
           "daily": pd.concat([r["daily"] for r in res], ignore_index=True)}
    nh = [r["nohook"] for r in res if r["nohook"] is not None]
    out["nohook"] = pd.concat(nh, ignore_index=True) if nh else None
    return out


def check_asfrozen(new: pd.DataFrame, tag: str) -> dict:
    old = pd.read_parquet(OUT / f"e2_summary_{tag}.parquet")
    assert len(new) == len(old) and list(new.columns) == list(old.columns), "row or column mismatch"
    m = old.merge(new, on=KEYS, suffixes=("_old", "_new"), validate="one_to_one")
    assert len(m) == len(old)
    bad = []
    for c in old.columns:
        if c in KEYS:
            continue
        a, b = m[f"{c}_old"], m[f"{c}_new"]
        same = (a == b) | (a.isna() & b.isna()) if a.dtype != object else (a == b)
        if not same.all():
            bad.append(c)
    return {"tag": tag, "rows": len(old), "columns": len(old.columns), "columns_differing": bad, "identical": not bad}


def run(mode: str, d: int, k: int) -> None:
    cfg = load_cfg()
    tag = tag_of(d, k)
    assert tag in {t for t, _, _ in RUNS.values()}, f"{tag} is not one of the stored runs this check compares with"
    res = simulate_all(mode, d, k)
    if mode == "asfrozen":
        chk = check_asfrozen(res["table"], tag)
        chk["git_commit"] = reporting.git_commit()
        (OUT / f"e2_asfrozen_check_{tag}_blockgas.json").write_text(json.dumps(chk, indent=1))
        print(json.dumps(chk, indent=1))
        assert chk["identical"], "the snapshot does not reproduce the stored E2 results"
        return
    _, _, info = configs(cfg, mode)
    sfx = SUFFIX[mode]
    paths = {"summary": OUT / f"e2_summary_{tag}_{sfx}.parquet", "daily": OUT / f"e2_daily_{tag}_{sfx}.parquet"}
    for p in paths.values():
        assert not p.exists(), f"{p} exists; refusing to overwrite"
    res["table"].to_parquet(paths["summary"], index=False)
    res["daily"].to_parquet(paths["daily"], index=False)
    if res["nohook"] is not None:
        paths["nohook"] = OUT / f"e2_nohook_steps_{tag}_{sfx}.parquet"
        res["nohook"].to_parquet(paths["nohook"], index=False)
    reporting.write_manifest("e2", cfg, [CACHE / "aligned" / f"{kk}.parquet" for kk in cfg["pools"]], SPLIT,
                             {"hook_lag_min": d, "cadence_min": k, "eps_mode": cfg["eps_mode"], "regimes": ["median"],
                              "mode": mode, **info,
                              "outputs": {p.name: reporting.sha256(p) for p in paths.values()}},
                             tag=f"{tag}_{sfx}", latest=False)
    print(f"{mode} {tag}: wrote", ", ".join(p.name for p in paths.values()))


# ---------------------------------------------------------------------------------------------------------------- compare
METRICS = {"protection_usd": "funds (USD)", "execution_rate": "execution rate", "etw_mean": "mean price error",
           "participation_violation_rate": "participation violations", "cap_violation_rate": "cap violations",
           "n_executed": "executed corrections"}


def removal(steps: pd.DataFrame, hook_gas: float, stored_gas: float) -> tuple[int, int]:
    """State-matched corrections removed by the hook gas on the no-hook path (e2_hook_gas.py): F150 and not F(150+g).
    C_hook_usd was stored at `stored_gas`; gas cost is linear in the gas amount, so it is rescaled."""
    f150 = steps["f150"].to_numpy()
    ch = steps["C_hook_usd"].to_numpy() * (hook_gas / stored_gas)
    fg = (steps["S_usd"].to_numpy() - steps["C_usd"].to_numpy() - ch) >= steps["R_usd"].to_numpy()
    return int(f150.sum()), int((f150 & ~fg).sum())


def compare() -> None:
    cfg = load_cfg()
    gas = json.loads(GAS_CFG.read_text())["hook_overhead_gas"]
    h = cfg["headline"]
    rows, rem = [], []
    for rule, (tag, mech, dm) in RUNS.items():
        old = pd.read_parquet(OUT / f"e2_summary_{tag}.parquet")
        old_steps = pd.read_parquet(OUT / f"e2_nohook_steps_{tag}.parquet")
        for mode, sfx in SUFFIX.items():
            new = pd.read_parquet(OUT / f"e2_summary_{tag}_{sfx}.parquet")
            new_steps = pd.read_parquet(OUT / f"e2_nohook_steps_{tag}_{sfx}.parquet")
            for name, sel in ((rule, (lambda s: (s.mech == mech) & (s.lam == h["lambda"]) & (s.gamma == h["gamma"]) & (s.dmult == dm))),
                              ("baseline (hook gas, no transfer)", lambda s: s.mech == "baseline"),
                              ("baseline_nohook", lambda s: s.mech == "baseline_nohook")):
                o, n = old[sel(old)].set_index(["pool", "variant"]), new[sel(new)].set_index(["pool", "variant"])
                assert len(o) == len(n) == 9
                for (pool, var), orow in o.iterrows():
                    nrow = n.loc[(pool, var)]
                    for c, label in METRICS.items():
                        ov, nv = float(orow[c]), float(nrow[c])
                        rows.append({"run": rule, "tag": tag, "gas_case": mode, "mechanism": name, "pool": pool, "variant": var,
                                     "metric": label, "stored_30214": ov, "blockgas": nv,
                                     "rel_change": (nv / ov - 1) if ov != 0 else (0.0 if nv == 0 else np.nan)})
            # the no-hook path does not depend on the hook gas: its states must be identical
            assert (old_steps[["pool", "variant", "t", "S_usd", "C_usd", "f150"]].reset_index(drop=True)
                    .equals(new_steps[["pool", "variant", "t", "S_usd", "C_usd", "f150"]].reset_index(drop=True))), "no-hook path changed"
            for (pool, var), s_old in old_steps.groupby(["pool", "variant"], sort=False):
                s_new = new_steps[(new_steps.pool == pool) & (new_steps.variant == var)]
                n0, m0 = int(s_old.f150.sum()), int((s_old.f150 & ~s_old.f180).sum())
                assert removal(s_old, cfg["hook_overhead_gas"], cfg["hook_overhead_gas"]) == (n0, m0)
                n1, m1 = int(s_new.f150.sum()), int((s_new.f150 & ~s_new.f180).sum())          # at 34,559, from the replay
                assert removal(s_old, gas["first_swap_of_block"], cfg["hook_overhead_gas"]) == (n1, m1), "rescaling check"
                _, mc = removal(s_old, gas["charged_swap"], cfg["hook_overhead_gas"])
                _, me = removal(s_old, gas["charged_swap_empty_vault_sensitivity"], cfg["hook_overhead_gas"])
                if mode == "blockgas":
                    rem.append({"run": rule, "tag": tag, "pool": pool, "variant": var, "n_f150": n0,
                                "removed_30214": m0, "removed_34559": m1, "removed_48177": mc, "removed_65277": me,
                                "X_30214": m0 / n0 if n0 else np.nan, "X_34559": m1 / n0 if n0 else np.nan,
                                "X_48177": mc / n0 if n0 else np.nan, "X_65277": me / n0 if n0 else np.nan})
    df, rm = pd.DataFrame(rows), pd.DataFrame(rem)
    for c in ("X_34559", "X_48177", "X_65277"):
        rm[f"rel_change_{c}"] = rm[c] / rm["X_30214"] - 1
    reporting.write_table(df, OUT / "tables" / "e2_blockgas_comparison_valid", {"stored_30214": "{:.6g}", "blockgas": "{:.6g}", "rel_change": "{:+.4%}"})
    reporting.write_table(rm, OUT / "tables" / "e2_blockgas_removed_valid", {c: "{:.4f}" for c in rm.columns if c.startswith(("X_", "rel_"))})
    key = df[(df.pool == "eth_usdc_005") | (df.variant == "corr24h")]
    with pd.option_context("display.width", 250, "display.max_columns", 30, "display.max_rows", 400):
        print(key[key.mechanism.isin(["ideal", "buffered"])].pivot_table(index=["run", "gas_case", "pool", "variant"], columns="metric",
                                                                          values="rel_change").round(4).to_string())
        print(rm.round(4).to_string(index=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["asfrozen", "blockgas", "blockgas_emptyvault", "compare"], required=True)
    ap.add_argument("--lag", type=int, default=0)
    ap.add_argument("--cadence", type=int, default=1)
    a = ap.parse_args()
    if a.mode == "compare":
        compare()
    else:
        run(a.mode, a.lag, a.cadence)


if __name__ == "__main__":
    main()
