#!/usr/bin/env python3
"""E3: economic outcomes and the Pareto protection frontier. Aggregates the E2 summary (no new data).

  python experiments/e3_pareto_frontier.py --tag valid            # E2 run on the validation months, ideal oracle
  python experiments/e3_pareto_frontier.py --tag valid_lag1
  python experiments/e3_pareto_frontier.py --tag test             # only after E2 --split test --confirm-frozen
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.ticker
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import plotstyle, reporting  # noqa: E402
from common.plotstyle import DELTA, GAMMA, LAMBDA, POOL_LABEL  # noqa: E402

CFG = ROOT / "experiments" / "configs" / "e3.yml"
MARK = {"baseline": ("o", "k"), "static_0.05pct": ("s", "#8e6c00"), "static_0.30pct": ("s", "#d35400"), "unconstrained": ("^", "#c0392b"),
        "cap_gamma0": ("D", "#6c3483"), "retained": ("o", "#1f4e79"), "buffered_1eps": ("P", "#2e8b57"), "buffered_2eps": ("X", "#117a8b")}
LABEL = {"baseline": "baseline AMM", "static_0.05pct": "static 0.05%", "static_0.30pct": "static 0.30%",
         "unconstrained": r"unconstrained $\lambda S$", "cap_gamma0": r"maximal cap ($\gamma=0$)", "retained": r"retained margin $\gamma>0$",
         "buffered_1eps": r"buffered $\delta=\varepsilon_S$", "buffered_2eps": r"buffered $\delta=2\varepsilon_S$"}


def pareto_mask(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Non-dominated points: minimise x (price error), maximise y (protection)."""
    keep = np.ones(len(x), bool)
    for i in range(len(x)):
        dom = (x <= x[i]) & (y >= y[i]) & ((x < x[i]) | (y > y[i]))
        keep[i] = not dom.any()
    return keep


def frontier_figure(df: pd.DataFrame, xcol: str, out: Path, title_tag: str, gamma_show: float):
    plotstyle.apply()
    pools = list(df["pool"].unique())
    fig, axs = plt.subplots(1, len(pools), figsize=(3.0 * len(pools), 3.2))
    axs = np.atleast_1d(axs)
    for ax, pk in zip(axs, pools):
        d = df[df["pool"] == pk]
        pm = pareto_mask(d[xcol].to_numpy(), d["protection_usd"].to_numpy())
        for mech, (mk, col) in MARK.items():
            m = d[d["mech"] == mech]
            if m.empty:
                continue
            if mech in ("retained", "buffered_1eps", "buffered_2eps"):
                m = m[np.isclose(m["gamma"], gamma_show)]
            if m["lam"].nunique() > 1:
                m = m.sort_values("lam")
                ax.plot(m[xcol], m["protection_usd"], color=col, lw=0.8, alpha=0.7)
            ax.scatter(m[xcol], m["protection_usd"], marker=mk, color=col, s=22, label=LABEL[mech], zorder=3)
        ax.scatter(d.loc[pm, xcol], d.loc[pm, "protection_usd"], facecolors="none", edgecolors="k", s=70, lw=0.8, zorder=4,
                   label="Pareto-efficient")
        ax.set_xlabel(("mean" if xcol == "etw_mean" else "P95") + r" $E_{TW}$ $|\log(p_{pool}/p_{ref})|$")
        ax.set_title(POOL_LABEL[pk] + (f" ({d['variant'].iloc[0]})" if d["variant"].iloc[0] != "raw" else ""))
        ax.set_xscale("log")
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    axs[0].set_ylabel("realised LP-protection funds (USD)")
    h, l = axs[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=5, frameon=False, fontsize=7, bbox_to_anchor=(0.5, -0.12))
    fig.tight_layout()              # no suptitle: the paper caption states the regime, tag and gamma
    reporting.savefig(fig, out)
    plt.close(fig)


def hodl_figure(df: pd.DataFrame, out: Path, gamma_show: float):
    plotstyle.apply()
    pools = list(df["pool"].unique())
    fig, axs = plt.subplots(1, len(pools), figsize=(3.0 * len(pools), 3.0))
    axs = np.atleast_1d(axs)
    for ax, pk in zip(axs, pools):
        d = df[df["pool"] == pk]
        for mech, (mk, col) in MARK.items():
            m = d[d["mech"] == mech]
            if mech in ("retained", "buffered_1eps", "buffered_2eps"):
                m = m[np.isclose(m["gamma"], gamma_show)]
            m = m[np.isclose(m["lam"], 0.95) | (m["lam"] == 0)]
            ax.scatter(m["execution_rate"] * 100, m["mean_lp_vs_hodl_bp"], marker=mk, color=col, s=26, label=LABEL[mech], zorder=3)
        ax.axhline(0, color="grey", lw=0.6)
        ax.set_xlabel("arbitrage participation (%)")
        ax.set_title(POOL_LABEL[pk])
    axs[0].set_ylabel("mean LP outcome vs HODL (bp)")
    h, l = axs[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=4, frameon=False, fontsize=7, bbox_to_anchor=(0.5, -0.1))
    fig.tight_layout()
    reporting.savefig(fig, out)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="valid")
    ap.add_argument("--regime", default="median")
    ap.add_argument("--gamma", type=float, default=0.02)
    a = ap.parse_args()
    out = reporting.out_dir("e3")
    summ = pd.read_parquet(reporting.out_dir("e2") / f"e2_summary_{a.tag}.parquet")
    s = summ[summ["regime"] == a.regime]
    primary = s[(s["variant"] == "raw") | (s["variant"] == "corr24h")]
    primary = primary[~((primary["pool"].isin(["eth_wbtc_030", "eth_wsteth_001"])) & (primary["variant"] == "raw"))]
    raw = s[s["variant"] == "raw"]

    # totals table (all configurations)
    cols = ["pool", "variant", "regime", "mech", "lam", "gamma", "protection_usd", "searcher_net_usd", "executed_correction", "execution_rate",
            "etw_mean", "etw_p95", "mean_lp_vs_hodl_bp"]
    reporting.write_table(s[cols], out / "tables" / f"e3_totals_{a.tag}")

    # Pareto membership per pool/variant (both price-error versions)
    rows = []
    for (pk, v), d in s.groupby(["pool", "variant"]):
        for xcol in ("etw_mean", "etw_p95"):
            pm = pareto_mask(d[xcol].to_numpy(), d["protection_usd"].to_numpy())
            for _, r in d[pm].iterrows():
                rows.append({"pool": pk, "variant": v, "x": xcol, "mech": r["mech"], "lam": r["lam"], "gamma": r["gamma"],
                             xcol: r[xcol], "protection_usd": r["protection_usd"]})
    pareto = pd.DataFrame(rows)
    pareto.to_csv(out / "tables" / f"e3_pareto_set_{a.tag}.csv", index=False)

    for xcol, nm in (("etw_mean", "mean"), ("etw_p95", "p95")):
        frontier_figure(primary, xcol, out / "figures" / f"e3_frontier_{nm}_primary_{a.tag}", f"E3 frontier, {a.regime} R, tag {a.tag}", a.gamma)
        frontier_figure(raw, xcol, out / "figures" / f"e3_frontier_{nm}_raw_{a.tag}", f"E3 frontier (raw reference), {a.regime} R, tag {a.tag}", a.gamma)
    hodl_figure(primary, out / "figures" / f"e3_lp_vs_hodl_{a.tag}", a.gamma)
    head = primary[(primary["lam"].isin([0, 0.75])) & (primary["gamma"].isin([0, a.gamma]))][cols]
    print(head.round(5).to_string(index=False))
    print("Pareto members per pool:\n", pareto[pareto["x"] == "etw_mean"].groupby(["pool", "variant"])["mech"].apply(lambda x: sorted(set(x))).to_string())


if __name__ == "__main__":
    main()
