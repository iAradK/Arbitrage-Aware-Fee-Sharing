#!/usr/bin/env python3
"""Attribution check (analysis only, nothing written to results/): does the retained margin's extra price error over the
baseline come from the extra settlement gas of a charged swap?

E2's own code (experiments/e2_sequential_replay.py, frozen at f0e46a9) replays the test months, ideal reference (d = 0,
k = 1), median R regime, primary variant of each pool, one configuration per run:
  baseline        the frozen E2 baseline row
  retained        lambda 0.75, gamma 0.02, frozen final gas (uncharged 150,000 + 38,520; charged 150,000 + 52,161)
  retained_cf     the same, with the gas paid by a charged swap set to the uncharged figure (gas_ch = gas); the hook's
                  K_hat (ghat) keeps the frozen charged figure, so only the gas the searcher pays changes
1. baseline and retained must reproduce results/final_test/e2/e2_summary_test.parquet exactly (every column of the
   summary row); otherwise the script stops.
2. Per-correction records are captured by wrapping cpmm.full_correction_net, cpmm.trade and argmax_smallest inside the
   e2 module (observation only; simulate's logic is unchanged): for every step where retained acts, the size grid, the
   chosen size and the state. On that same state the script evaluates the baseline's choice (payoff S - C) and the
   counterfactual retained choice without the extra settlement gas (S - C - r), with mechanism.argmax_smallest.
   A correction "differs in size" when retained's chosen size differs from the baseline's on the same state; it is
   "explained by the extra settlement gas" when the counterfactual retained choice equals the baseline's.
3. Price-error gap: etw_mean(retained) / etw_mean(baseline) - 1 and the same for retained_cf, from the three replays.

  RESULTS_RUN=final_test python scratchpad/e2_settlement_gas_attribution.py [--out scratchpad/e2_settlement_gas_attribution.md]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))
os.environ.setdefault("RESULTS_RUN", "final_test")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import e2_sequential_replay as e2  # noqa: E402
from common import cpmm, minutegrid as mg, reporting  # noqa: E402
from common.mechanism import argmax_smallest  # noqa: E402
from common.pools import POOLS  # noqa: E402

PRIMARY = {"eth_usdc_005": "raw", "eth_wbtc_030": "corr24h", "eth_wsteth_001": "corr24h"}
LAM, GAM = 0.75, 0.02


class Recorder:
    """Wraps the three helpers simulate calls, in the e2 module only. Active only while `on`."""

    def __init__(self):
        self.on = False
        self.reset()

    def reset(self):
        self.n_fcn = -1          # index of the acting step (one full_correction_net call per acting step)
        self.last_tr = None
        self.rows = []           # (acting step index, tr dict, masked payoff, chosen j)

    def full_correction_net(self, *a, **k):
        if self.on:
            self.n_fcn += 1
            self.last_tr = None
        return cpmm.full_correction_net(*a, **k)

    def trade(self, *a, **k):
        out = cpmm.trade(*a, **k)
        if self.on and np.ndim(out["d0"]) == 2:          # the size grid (the first call per step is the full correction)
            self.last_tr = {c: np.array(out[c], dtype=float) for c in ("d0", "d1", "x", "y")}
        return out

    def argmax_smallest(self, payoff, *a, **k):
        j = argmax_smallest(payoff, *a, **k)
        if self.on:
            self.rows.append((self.n_fcn, self.last_tr, np.array(payoff, dtype=float), np.array(j)))
        return j


class CpmmProxy:
    def __init__(self, rec):
        self.rec = rec

    def __getattr__(self, name):
        if name == "full_correction_net":
            return self.rec.full_correction_net
        if name == "trade":
            return self.rec.trade
        return getattr(cpmm, name)


def git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "scratchpad" / "e2_settlement_gas_attribution.md"))
    a = ap.parse_args()
    head = git("rev-parse", "--short", "HEAD")
    code_same = git("diff", "--name-only", "f0e46a9", "HEAD", "--", "experiments", "common", "config") == ""
    dirty = git("status", "--porcelain", "--", "experiments", "common", "config")
    assert code_same and not dirty, "E2 code or config differs from f0e46a9"
    cfg = reporting.load_config(ROOT / "experiments" / "configs" / "e2_final.yml")
    stored = pd.read_parquet(reporting.out_dir("e2") / "e2_summary_test.parquet")
    Cfull = e2.final_configs(cfg)
    rec = Recorder()
    e2.cpmm = CpmmProxy(rec)
    e2.argmax_smallest = rec.argmax_smallest
    d, k = cfg["hook_lag_min"], 1
    G = cfg["grid_points"]
    lines, recs = [], []
    t0 = time.time()
    for key, variant in PRIMARY.items():
        pool = POOLS[key]
        R_usd = mg.r_regimes_usd(key, variant, cfg["gas_units"], cfg["r_quantiles"])["median"]
        g = mg.build_grid(key, "test", variant, lags=(0, d))
        er = e2.final_eps_rel(cfg, key, variant, g, reference=f"lag{d}")
        seed = e2.eps_seed(key, variant, R_usd, d, k, e2.PRIOR["test"], cfg)        # as main(); recorder off
        sel_b = (Cfull.mech == "baseline")
        sel_r = (Cfull.mech == "retained") & np.isclose(Cfull.lam, LAM) & np.isclose(Cfull.gamma, GAM)
        Cb = Cfull[sel_b].reset_index(drop=True)
        Cr = Cfull[sel_r].reset_index(drop=True)
        Ccf = Cr.copy()
        Ccf["gas_ch"] = Ccf["gas"]
        out = {}
        for name, C in (("baseline", Cb), ("retained", Cr), ("retained_cf", Ccf)):
            rec.reset()
            rec.on = name == "retained"
            res = e2.simulate(g, pool, C, cfg, R_usd, np.nan, d, k, seed, eps_rel=er)
            rec.on = False
            out[name] = res["table"].iloc[0]
            if name == "retained":
                rows = list(rec.rows)
        # 1. exact reproduction of the stored summary rows
        for name, mech in (("baseline", "baseline"), ("retained", "retained")):
            s = stored[(stored.pool == key) & (stored.variant == variant) & (stored.regime == "median") & (stored.mech == mech)
                       & np.isclose(stored.lam, 0.0 if mech == "baseline" else LAM) & np.isclose(stored.gamma, 0.0 if mech == "baseline" else GAM)]
            assert len(s) == 1, (key, mech, len(s))
            s = s.iloc[0]
            for c in out[name].index:
                if c in ("eps_S_used_median_usd",):
                    continue
                a_, b_ = out[name][c], s[c]
                same = (a_ == b_) or (isinstance(a_, float) and np.isnan(a_) and np.isnan(b_))
                if not same:
                    raise SystemExit(f"STOP: {key} {name} column {c}: replay {a_!r} != stored {b_!r}")
        # 2. per-correction records on the retained path
        fin = np.isfinite(g["p_ref_0"].to_numpy()) & np.isfinite(g[f"p_ref_{d}"].to_numpy()) & np.isfinite(g["L"].to_numpy()) \
            & np.isfinite(g["gas_wei"].to_numpy())
        steps = np.flatnonzero(fin)
        pib_a, gas_a, en_a, usd_a = g["p_ref_0"].to_numpy(), g["gas_wei"].to_numpy(), g["eth_in_num"].to_numpy(), g["usd_per_num"].to_numpy()
        gas_u, gas_ch, ghat = float(Cr["gas"].iloc[0]), float(Cr["gas_ch"].iloc[0]), float(Cr["ghat"].iloc[0])
        n_rec = len(rows)
        mism = 0
        for n, tr, payoff, j in rows:
            i = steps[n]
            usd = usd_a[i]
            R = R_usd / usd
            Cst = float(mg.gas_cost_num(gas_a[i], en_a[i], gas_u))
            Cch = float(mg.gas_cost_num(gas_a[i], en_a[i], gas_ch))
            Kh = float(mg.gas_cost_num(gas_a[i], en_a[i], ghat)) + R
            S = np.maximum(pib_a[i] * tr["d0"][0] + tr["d1"][0], 0.0)
            r = np.minimum(LAM * S, (1 - GAM) * np.maximum(S - Kh, 0.0))   # d = 0: S_hat = S; retained: no buffer
            Pi = S - np.where(r > 0, Cch, Cst) - r
            tol = 1e-9 * max(1.0, R)
            mine = np.where(Pi >= R - tol, Pi, -np.inf)
            mism += int(not np.array_equal(mine, payoff[0]))
            jr = int(j[0])
            pb = S - Cst
            jb = int(argmax_smallest(np.where(pb >= R - tol, pb, -np.inf)))
            pc = S - Cst - r
            jc = int(argmax_smallest(np.where(pc >= R - tol, pc, -np.inf)))
            recs.append({"pool": key, "t": g["t"].iloc[i], "j_retained": jr, "j_baseline": jb, "j_retained_cf": jc,
                         "r_retained_usd": r[jr] * usd, "r_at_baseline_size_usd": r[jb] * usd,
                         "extra_gas_usd": (Cch - Cst) * usd, "S_retained_usd": S[jr] * usd, "S_baseline_usd": S[jb] * usd,
                         "frac_retained": (jr + 1) / G, "frac_baseline": (jb + 1) / G})
        assert mism == 0, f"{key}: recomputed retained payoff differs from simulate's at {mism} steps"
        df = pd.DataFrame([r_ for r_ in recs if r_["pool"] == key])
        diff = df[df.j_retained != df.j_baseline]
        expl = diff[diff.j_retained_cf == diff.j_baseline]
        b, r_, c_ = (out[n_]["etw_mean"] for n_ in ("baseline", "retained", "retained_cf"))
        gap, gap_cf = r_ / b - 1, c_ / b - 1
        share = (gap - gap_cf) / gap if gap != 0 else np.nan
        lines.append({"pool": key, "variant": variant, "n_corrections": int(out["retained"]["n_executed"]), "n_records": n_rec,
                      "etw_baseline": b, "etw_retained": r_, "etw_retained_cf": c_, "gap_retained": gap, "gap_retained_cf": gap_cf,
                      "share_gap_explained": share, "n_size_differs": len(diff), "n_explained_by_settlement_gas": len(expl),
                      "n_explained_charge_avoided": int((expl.r_retained_usd <= 0).sum()),
                      "n_explained_charge_reduced": int(((expl.r_retained_usd > 0) & (expl.r_retained_usd < expl.r_at_baseline_size_usd)).sum()),
                      "n_smaller_than_baseline": int((diff.j_retained < diff.j_baseline).sum()),
                      "protection_retained": out["retained"]["protection_usd"], "protection_retained_cf": out["retained_cf"]["protection_usd"]})
        print(f"{key} done in {time.time() - t0:.0f}s: {lines[-1]}", flush=True)
    res = pd.DataFrame(lines)
    md = [f"# E2 retained vs baseline: extra settlement gas (test months, d = 0, k = 1, median R)\n",
          f"Script `scratchpad/e2_settlement_gas_attribution.py`, run on HEAD {head} (experiments/, common/, config/ identical to f0e46a9). "
          f"Baseline and retained reproduce `results/final_test/e2/e2_summary_test.parquet` exactly (every column but eps_S_used_median_usd). "
          f"Counterfactual: charged-swap gas = uncharged ({int(gas_u):,} instead of {int(gas_ch):,}); K_hat unchanged.\n",
          "| Pool | Corrections | E_TW gap retained vs baseline | gap without extra settlement gas | share of gap explained | "
          "sizes differ (same state) | explained by settlement gas | of which charge avoided / reduced | retained smaller |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r_ in res.itertuples():
        md.append(f"| {r_.pool} {r_.variant} | {r_.n_corrections:,} | {r_.gap_retained:+.3%} | {r_.gap_retained_cf:+.3%} | {r_.share_gap_explained:.0%} | "
                  f"{r_.n_size_differs:,} | {r_.n_explained_by_settlement_gas:,} | {r_.n_explained_charge_avoided:,} / {r_.n_explained_charge_reduced:,} | "
                  f"{r_.n_smaller_than_baseline:,} |")
    md.append("\nProtection (USD), retained vs counterfactual: " + "; ".join(
        f"{r_.pool} {r_.protection_retained:,.0f} vs {r_.protection_retained_cf:,.0f}" for r_ in res.itertuples()))
    Path(a.out).write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
