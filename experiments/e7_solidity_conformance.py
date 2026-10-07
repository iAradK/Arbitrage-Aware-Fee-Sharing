#!/usr/bin/env python3
"""E7: Solidity conformance, gas overhead, minimum viable surplus.

  python experiments/e7_solidity_conformance.py --vectors      # write results/e7/vectors.json from the integer reference
  python experiments/e7_solidity_conformance.py --forge        # run the Foundry tests in WSL, parse gas, write gas_profile.json
  python experiments/e7_solidity_conformance.py --smin         # S_min quantiles per month and the share of opportunities above it
  (flags can be combined; --smin --split test needs --confirm-frozen)

Foundry runs in WSL2 (see the project memory): wsl -e bash -ic 'cd contracts && forge test --offline ...'.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "contracts" / "python"))
sys.path.insert(0, str(ROOT / "experiments"))

from common import fixedpoint as fp, minutegrid as mg, refprice, reporting  # noqa: E402
from common.pools import CACHE, POOLS, RESULTS  # noqa: E402

CFG = ROOT / "experiments" / "configs" / "e7.yml"


def _wsl_path(p: Path) -> str:
    s = str(p.resolve()).replace("\\", "/")
    return "/mnt/" + s[0].lower() + s[2:]


def build_vectors(cfg: dict, out: Path) -> dict:
    import e6_fragmentation as e6
    e6cfg = reporting.load_config(ROOT / "experiments" / "configs" / "e6.yml")
    e6cfg["n_opportunities"] = 100          # E6 v2 replays every opportunity; the vector sample keeps the v1 size
    rng = np.random.default_rng(cfg["seed"])
    lam_w = [fp.to_wad(l) for l in cfg["lambdas"]]
    gam_w = fp.to_wad(cfg["gamma"])
    eps_path = RESULTS / "e4" / "tables" / "e4_eps_valid.csv"
    eps_tab = pd.read_csv(eps_path) if eps_path.exists() else None
    atomic, seqs = [], []
    for key in cfg["pools"]:
        pool = POOLS[key]
        R_usd = mg.r_regimes_usd(key, "raw", cfg["arb_tx_gas"], cfg["r_quantiles"])[cfg["r_regime"]]
        eps = 0.0
        if eps_tab is not None:
            m = eps_tab[(eps_tab["pool"] == key) & (eps_tab["variant"] == "raw") & (eps_tab["scenario"] == f"lag{cfg['delta_from_e4_lag_min']}")]
            eps = float(m["eps_S_valid_usd"].iloc[0]) if len(m) else 0.0
        # validation-month replay states (real pool state each minute, full price-correcting candidate)
        g = mg.build_grid(key, "valid", "raw", lags=(0,))
        c = mg.real_state_candidates(g, key, cfg["arb_tx_gas"], R_usd, 0)
        c = c[c["valid"] & (c["S"] > 0)].copy()
        c["S_usd"], c["K_usd"] = c["S"] * c["usd"], (c["C"] + c["R"]) * c["usd"]
        c["mult"] = c["S_usd"] / (c["K_usd"] + eps)
        used = set()
        for m in cfg["state_multiples_of_K"]:
            i = (np.log(c["mult"]) - np.log(m)).abs().sort_values().index
            i = next(j for j in i if j not in used)
            used.add(i)
            r = c.loc[i]
            for lam_f, lw in zip(cfg["lambdas"], lam_w):
                S_w, K_w, d_w = fp.to_wad(r["S_usd"]), fp.to_wad(r["K_usd"]), fp.to_wad(eps)
                t, cls = fp.transfer_wad(S_w, K_w, lw, gam_w, d_w)
                atomic.append({"pool": key, "state_time": str(r["t"]), "target_multiple": m, "lambda": lam_f, "surplus_hat_wad": str(S_w),
                               "k_hat_wad": str(K_w), "lambda_wad": str(lw), "gamma_wad": str(gam_w), "delta_wad": str(d_w),
                               "expected_transfer_wad": str(t), "class": cls})
        # fragment and reversal sequences from E6-type opportunities (validation split)
        opps, _ = e6.select_opportunities(key, "raw", "valid", e6cfg, R_usd, rng)
        for o in opps[opps["quartile"].isin([2, 4])].groupby("quartile").head(cfg["sequence_opportunities_per_pool"] // 2).itertuples():
            usd = o.usd_per_num
            K_w = fp.to_wad((o.C + o.R) * usd)
            lw, d_w = fp.to_wad(0.75), fp.to_wad(eps)
            for n in cfg["sequence_partitions"]:
                paths = {"equal": e6.path_prefix_surplus(o, np.arange(1, n + 1) / n, pool) * usd, "reversal": e6.reversal_prefix_surplus(o, n, pool) * usd}
                for kind, A in paths.items():
                    A_w = [fp.to_wad(v) for v in A]
                    rows = fp.cumulative_charges(A_w, K_w, lw, gam_w, d_w)
                    deltas = [A_w[0]] + [A_w[j] - A_w[j - 1] for j in range(1, len(A_w))]
                    seqs.append({"pool": key, "kind": kind, "n": n, "k_hat_wad": str(K_w), "lambda_wad": str(lw), "gamma_wad": str(gam_w),
                                 "delta_wad": str(d_w), "deltas": [str(d) for d in deltas], "expected_charges": [str(r["charge"]) for r in rows],
                                 "expected_targets": [str(r["target"]) for r in rows]})
    vec = {"n_atomic": len(atomic), "n_sequences": len(seqs), "atomic": atomic, "sequences": seqs}
    (out / "vectors.json").write_text(json.dumps(vec))
    cls = pd.Series([a["class"] for a in atomic]).value_counts().to_dict()
    print("vectors:", len(atomic), "atomic, class counts (python):", cls, "|", len(seqs), "sequences,", sum(len(s["deltas"]) for s in seqs), "fragments")
    return {"class_counts_python": cls}


def wsl(cmd: str, cwd: Path, timeout=1800) -> str:
    full = f"cd '{_wsl_path(cwd)}' && {cmd}"
    r = subprocess.run(["wsl", "-e", "bash", "-ic", full], capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def run_forge(cfg: dict, out: Path) -> None:
    proj = ROOT / cfg["forge_project"]
    # The tests write their outputs only when these variables are set (paths relative to the forge project).
    rel = Path(os.path.relpath(out, proj)).as_posix()
    log = wsl(f"E7_COUNTS='{rel}/conformance_counts.json' E7_GAS_CSV='{rel}/gas_cumulative.csv' "
              "forge test --offline --match-contract 'E7ConformanceTest|E7GasProfileTest' -vv 2>&1 | grep -E 'PASS|FAIL|Suite|Error|mismatch' ", proj)
    print(log)
    log2 = wsl(f"HOOK_LIFECYCLE_GAS_OUTPUT='{rel}/hook_lifecycle_gas.csv' forge test --offline --match-contract HookGasBenchmarkTest 2>&1 | grep -E 'PASS|FAIL|Suite'", proj)
    print(log2)
    # v2 (fix 5): every fragment count in its own transaction, so each run's first swap is cold
    log3 = wsl("forge test --offline --match-contract HookGasIsolatedTest -vv 2>&1 | grep -E 'ISOGAS|PASS|FAIL|Suite'", proj)
    iso = pd.DataFrame([ln.strip().split(",")[1:] for ln in log3.splitlines() if ln.strip().startswith("ISOGAS,")],
                       columns=["n", "hook", "fragment", "gas"]).astype(int)
    iso.to_csv(out / "hook_gas_isolated.csv", index=False)
    piv = iso.pivot_table(index=["n", "fragment"], columns="hook", values="gas").reset_index()
    piv["overhead"] = piv[1] - piv[0]
    it = piv.groupby("n")["overhead"].sum().reset_index().rename(columns={"n": "n_fragments", "overhead": "hook_overhead_gas_total"})
    it["cumulative_k"] = it["hook_overhead_gas_total"] / 1e3
    it["penalty_vs_n1_pct"] = 100 * (it["hook_overhead_gas_total"] / it.loc[it["n_fragments"] == 1, "hook_overhead_gas_total"].iloc[0] - 1)
    reporting.write_table(it, out / "tables" / "e7_gas_isolated", {"hook_overhead_gas_total": "{:.0f}", "cumulative_k": "{:.1f}", "penalty_vs_n1_pct": "{:.1f}"})
    iso_first = float(piv[piv["fragment"] == 1]["overhead"].median())
    iso_extra = float(piv[piv["fragment"] >= 2]["overhead"].median())
    print(log3[-400:])
    (out / "forge_log.txt").write_text(log + "\n" + log2 + "\n" + "\n".join(l for l in log3.splitlines() if "ISOGAS" not in l))
    counts = json.load(open(out / "conformance_counts.json"))
    reporting.write_table(pd.DataFrame([counts]), out / "tables" / "e7_conformance")
    g = pd.read_csv(out / "gas_cumulative.csv")
    cb = g[g["phase"] == "callback"]
    tab = (g.groupby(["kind", "n", "phase"])["gas"].agg(["count", "sum", "min", "max"]).reset_index())
    reporting.write_table(tab, out / "tables" / "e7_gas_cumulative_core")
    # hook lifecycle overhead = gas with hook - gas without hook for the identical swap (valid oracle, single tick)
    h = pd.read_csv(out / "hook_lifecycle_gas.csv")
    v = h[(h["oracle_mode"] == "valid") & (h["tick_crossing_class"] == "single-tick")].copy()
    v["overhead"] = v["gas_used_with_hook"] - v["gas_used_without_hook"]
    first = float(v[v["fragment_index"] == 1]["overhead"].median())
    extra = float(v[v["fragment_index"] >= 2]["overhead"].median()) if (v["fragment_index"] >= 2).any() else float("nan")
    rows = []
    for n, gg in v.groupby("fragments"):
        tot = gg["overhead"].sum()
        rows.append({"n_fragments": int(n), "hook_overhead_gas_total": float(tot)})
    lt = pd.DataFrame(rows)
    lt["cumulative_k"] = lt["hook_overhead_gas_total"] / 1e3
    lt["penalty_vs_n1_pct"] = 100 * (lt["hook_overhead_gas_total"] / lt.loc[lt["n_fragments"] == 1, "hook_overhead_gas_total"].iloc[0] - 1)
    reporting.write_table(lt, out / "tables" / "e7_hook_lifecycle_overhead", {"hook_overhead_gas_total": "{:.0f}", "cumulative_k": "{:.1f}", "penalty_vs_n1_pct": "{:.1f}"})
    other = h[h["oracle_mode"] != "valid"].copy()
    other["overhead"] = other["gas_used_with_hook"] - other["gas_used_without_hook"]
    cold = float(v[(v["scenario_id"] == v["scenario_id"].min()) & (v["fragment_index"] == 1)]["overhead"].iloc[0])
    prof = {"first_call_gas": iso_first, "extra_fragment_gas": iso_extra,
            "source": "HookGasIsolatedTest (v2): one transaction per fragment count and per pool with/without the hook; the first fragment is cold, "
                      "later fragments warm, as inside one arbitrage transaction. Table: tables/e7_gas_isolated.",
            "v1_single_transaction_benchmark": {"first_call_gas_warm_median": first, "first_call_gas_cold_scenario0": cold, "extra_fragment_gas": extra,
                                                "caveat": "HookGasBenchmark runs every scenario inside ONE transaction, so only its very first swap is cold."},
            "definition": "gas with hook - gas without hook for the identical swap, valid oracle, single-tick", "fail_open_overhead_median": float(other["overhead"].median()),
            "computeTransfer_gas": {k: counts[k] for k in ("computeTransfer_gas_min", "computeTransfer_gas_max", "computeTransfer_gas_mean")},
            "core_processCallback_gas_median": float(cb["gas"].median()), "core_initializePool_gas_median": float(g[g["phase"] == "initialize"]["gas"].median()),
            "old_paper": {"computeTransfer": "981-1149", "initializePool": 28237, "processCallback": 22578}}
    (out / "gas_profile.json").write_text(json.dumps(prof, indent=1))
    for name in ("e2", "e4", "e5", "e6"):
        c = reporting.load_config(ROOT / "experiments" / "configs" / f"{name}.yml")
        if abs(c.get("hook_overhead_gas", iso_first) - iso_first) > 1:
            print(f"WARNING: configs/{name}.yml hook_overhead_gas={c['hook_overhead_gas']} differs from measured first_call_gas={iso_first:.0f}; update it and rerun")
    print(json.dumps(prof, indent=1))


def smin(cfg: dict, out: Path, split: str) -> None:
    prof = json.load(open(out / "gas_profile.json"))
    g_hat = cfg["arb_tx_gas"] + prof["first_call_gas"]              # arbitrage tx + hook overhead of a single-swap arbitrage
    bars, _ = mg._bars()
    blocks = pd.read_parquet(CACHE / "block_gas.parquet")
    ts = pd.DatetimeIndex(blocks["timestamp"])
    eth = refprice.lookup_bars(bars["ETHUSDC"], ts)
    # hook's gas price in Eq. (9): "block_median" = base fee + that block's own median tip (v2);
    # "train_median_constant" = base fee + a constant tip tau_hat, the median tip_p50 over real fee-history blocks of the train months
    tip_mode = cfg.get("tip_mode", "block_median")
    tau_hat = None
    if tip_mode == "block_median":
        gas_price = lambda df: df["gas_price_wei"].to_numpy()  # noqa: E731
    elif tip_mode == "train_median_constant":
        train = (ts < mg.VALID_START) & (blocks["gas_source"] == "fee_history").to_numpy()
        tau_hat = float(blocks.loc[train, "tip_p50_wei"].median())
        gas_price = lambda df: df["base_fee_wei"].to_numpy() + tau_hat  # noqa: E731
    else:
        raise ValueError(f"unknown tip_mode {tip_mode}")
    a_usd = g_hat * gas_price(blocks) * 1e-18 * eth                                       # C_gas_hat per block
    a_actual_usd = g_hat * blocks["gas_price_wei"].to_numpy() * 1e-18 * eth                # at the block's actual gas price (base + its median tip)
    df = pd.DataFrame({"month": ts.strftime("%Y-%m"), "a": a_usd, "a_actual": a_actual_usd}).dropna()
    eps_path = RESULTS / "e4" / "tables" / "e4_eps_valid.csv"
    eps_tab = pd.read_csv(eps_path) if eps_path.exists() else None
    rows, shares, gasq = [], [], []
    import e6_fragmentation as e6
    e6cfg = reporting.load_config(ROOT / "experiments" / "configs" / "e6.yml")
    gq = df["a"].quantile([0.10, 0.50, 0.90, 0.99])                  # gas conditions over all blocks of the year (v2, fix 6)
    gq_actual = df["a_actual"].quantile(gq.index)
    for key in cfg["pools"]:
        R_usd = mg.r_regimes_usd(key, "raw", cfg["arb_tx_gas"], cfg["r_quantiles"])[cfg["r_regime"]]
        eps = 0.0
        if eps_tab is not None:
            m = eps_tab[(eps_tab["pool"] == key) & (eps_tab["variant"] == "raw") & (eps_tab["scenario"] == f"lag{cfg['delta_from_e4_lag_min']}")]
            eps = float(m["eps_S_valid_usd"].iloc[0]) if len(m) else 0.0
        const = cfg["B_hat_usd"] + cfg["C_hedge_hat_usd"] + cfg["C_slip_hat_usd"] + R_usd + eps
        for qq, a_q in gq.items():
            gasq.append({"pool": key, "gas_quantile": f"P{int(round(qq * 100))}", "C_gas_usd": a_q, "R_protocol_usd": R_usd, "delta_usd": eps,
                         "S_min_delta0_usd": a_q + const - eps, "S_min_usd": a_q + const,
                         "C_gas_actual_usd": gq_actual[qq], "hook_gas_actual_usd": gq_actual[qq] * prof["first_call_gas"] / g_hat})
        for mo, gg in df.groupby("month"):
            q = gg["a"].quantile(cfg["smin_quantiles"]) + const
            rows.append({"pool": key, "month": mo, "R_protocol_usd": R_usd, "delta_usd": eps, **{f"S_min_p{int(k * 100)}_usd": v for k, v in q.items()}})
        # share of E6-type opportunities with S >= S_min at their own block
        c, n_raw = e6.all_candidates(key, "raw", split, e6cfg, R_usd)
        smin_c = g_hat * gas_price(c) * 1e-18 * c["eth_usd"].to_numpy() + const
        ok = c["S_usd"].to_numpy() >= smin_c
        ok0 = c["S_usd"].to_numpy() >= smin_c - eps                                   # same threshold with delta = 0
        shares.append({"pool": key, "split": split, "n_opportunities": len(c), "share_S_ge_Smin": float(ok.mean()) if len(c) else np.nan,
                       "share_S_ge_Smin_delta0": float(ok0.mean()) if len(c) else np.nan,
                       "median_S_usd": float(c["S_usd"].median()) if len(c) else np.nan, "median_S_min_usd": float(np.median(smin_c)) if len(c) else np.nan})
    q = pd.DataFrame(rows)
    reporting.write_table(q, out / "tables" / "e7_smin_by_month", {c: "{:.3f}" for c in q.columns if c.startswith("S_min") or c.endswith("usd")})
    gt = pd.DataFrame(gasq)
    reporting.write_table(gt, out / "tables" / "e7_smin_gas_conditions", {c: "{:.3f}" for c in gt.columns if c.endswith("usd")})
    sh = pd.DataFrame(shares)
    reporting.write_table(sh, out / "tables" / f"e7_smin_share_{split}", {"share_S_ge_Smin": "{:.3f}", "share_S_ge_Smin_delta0": "{:.3f}", "median_S_usd": "{:.2f}", "median_S_min_usd": "{:.2f}"})
    gp = "(base_fee + tip_p50 of the block)" if tau_hat is None else f"(base_fee + tau_hat), tau_hat = train-median tip_p50 = {tau_hat:.0f} wei"
    (out / "smin_formula.txt").write_text("S_min = C_gas_hat + B_hat + C_hedge_hat + C_slip_hat + R_protocol + delta,  C_gas_hat = g_hat * gasprice * p_native->USD,  "
                                          f"g_hat = arb_tx_gas ({cfg['arb_tx_gas']}) + hook first-call overhead ({prof['first_call_gas']:.0f}) = {g_hat:.0f} gas, "
                                          f"gasprice = {gp}.\n")
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(f"g_hat = {g_hat:.0f} gas, tip_mode = {tip_mode}, tau_hat = {tau_hat}")
        print(q[q["pool"] == "eth_usdc_005"].round(3).to_string(index=False))
        print(sh.round(3).to_string(index=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CFG))
    ap.add_argument("--vectors", action="store_true")
    ap.add_argument("--forge", action="store_true")
    ap.add_argument("--smin", action="store_true")
    ap.add_argument("--split", choices=["valid", "test"], default=None)
    ap.add_argument("--confirm-frozen", action="store_true")
    ap.add_argument("--freeze", action="store_true")
    a = ap.parse_args()
    cfg = reporting.load_config(a.config)
    out = reporting.out_dir("e7")
    if a.freeze:
        reporting.freeze("e7", cfg)
        return
    split = a.split or cfg["smin_share_split"]
    if a.smin:
        reporting.guard_split("e7", split, a.confirm_frozen, cfg)
    if a.vectors:
        build_vectors(cfg, out)
    if a.forge:
        run_forge(cfg, out)
    if a.smin:
        smin(cfg, out, split)
    reporting.write_manifest("e7", cfg, [CACHE / "aligned" / f"{k}.parquet" for k in cfg["pools"]], split)


if __name__ == "__main__":
    main()
