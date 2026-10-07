"""Figures of Section 5 (Evaluation) that replace tables. Run from the repository root:  python paper/make_eval_figures.py
Every number is read from results/ (no hard-coded values except the measured gas overhead, see GAS_*). Output: paper/figures/*.pdf
"""
import sys
from pathlib import Path

import matplotlib
import matplotlib.patches as mpatches
import matplotlib.ticker
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import plotstyle  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

R = ROOT / "results"
OUT = ROOT / "paper" / "figures"
OUT.mkdir(parents=True, exist_ok=True)
plotstyle.apply()
plt.rcParams.update({"font.size": 8, "axes.titlesize": 8, "axes.labelsize": 8, "legend.fontsize": 7})

# USDC/USDT is excluded (its fee tier is 0.001%, not the 0.01% of the model; NOTES/DECISIONS.md W8)
POOLS = ["eth_usdc_005", "eth_wbtc_030", "eth_wsteth_001"]
SHORT = {"eth_usdc_005": "ETH/USDC", "usdc_usdt_0001": "USDC/USDT", "eth_wbtc_030": "ETH/WBTC", "eth_wsteth_001": "ETH/wstETH"}
VARIANT = {"eth_usdc_005": "raw", "usdc_usdt_0001": "raw", "eth_wbtc_030": "corr24h", "eth_wsteth_001": "corr24h"}
C = plotstyle.COLORS
GAS_FIRST, GAS_EXTRA = 30.214, 13.823  # thousand gas, current contract with settlement (results/e7/gas_contract_8ca840a/gas_profile.json, DECISIONS W12)

# Okabe-Ito palette. Every series is also told apart by hatching (bars) or line style and marker (lines),
# so the figures stay readable in grayscale print.
OI = {"black": "#000000", "orange": "#E69F00", "sky": "#56B4E9", "green": "#009E73", "yellow": "#F0E442",
      "blue": "#0072B2", "vermilion": "#D55E00", "purple": "#CC79A7", "grey": "#999999"}
# mechanism -> (label, colour, hatch, marker, line style)
MECH = {"baseline": ("Baseline AMM", OI["grey"], "", "o", "-"),
        "static_0.05pct": ("Static 0.05%", OI["orange"], "////", "s", "-"),
        "static_0.30pct": ("Static 0.30%", OI["vermilion"], "\\\\\\\\", "v", "-"),
        "unconstrained": (r"Unconstrained $\lambda S$", OI["purple"], "xxxx", "^", "--"),
        "cap_gamma0": (r"Maximal cap ($\gamma=0$)", OI["sky"], "....", "D", ":"),
        "retained": ("Retained margin", OI["blue"], "", "o", "-"),
        "buffered_1eps": (r"Buffered $\delta=\varepsilon_S$", OI["green"], "----", "s", "--"),
        "buffered_2eps": (r"Buffered $\delta=2\varepsilon_S$", OI["yellow"], "||||", "^", ":")}
# pool -> (colour, marker, line style)
POOL_STYLE = {"eth_usdc_005": (OI["blue"], "o", "-"), "usdc_usdt_0001": (OI["vermilion"], "s", "--"),
              "eth_wbtc_030": (OI["green"], "^", "-."), "eth_wsteth_001": (OI["purple"], "D", ":")}
BAR_EDGE = {"edgecolor": "black", "linewidth": 0.4}
plt.rcParams["hatch.linewidth"] = 0.5


def mbar(ax, x, h, w, m, **kw):
    """Bar of mechanism m with its colour and hatch."""
    lab, col, hatch, _, _ = MECH[m]
    return ax.bar(x, h, w, color=col, hatch=hatch, **BAR_EDGE, **kw)


def save(fig, name):
    fig.savefig(OUT / f"{name}.pdf", bbox_inches="tight")
    fig.savefig(OUT / f"{name}.png", bbox_inches="tight", dpi=200)
    plt.close(fig)


# ---------------------------------------------------------------- Fig. 1: execution rate and price error (replaces Table replay)
def fig_replay():
    h = pd.read_csv(R / "e2/tables/e2_headline_test.csv")
    t3 = pd.read_csv(R / "e3/tables/e3_totals_test.csv")
    mechs = [(m, MECH[m][0]) for m in ("baseline", "static_0.05pct", "static_0.30pct", "unconstrained", "cap_gamma0", "retained")]
    fig, (a, b, c) = plt.subplots(1, 3, figsize=(7.4, 2.6), gridspec_kw={"width_ratios": [2.0, 0.95, 0.95]})
    w = 0.135
    for i, (m, lab) in enumerate(mechs):
        v = [100 * h[(h.pool == p) & (h.variant == VARIANT[p]) & (h.mech == m)].execution_rate.iloc[0] for p in POOLS]
        xs = np.arange(len(POOLS)) + (i - 2.5) * w
        mbar(a, xs, v, w, m, label=lab)
        for x, y in zip(xs, v):
            a.text(x, y + 1.5, f"{y:.0f}" if y >= 10 else f"{y:.1f}", ha="center", va="bottom", fontsize=5.5, rotation=90)
    a.set_xticks(range(len(POOLS))); a.set_xticklabels([SHORT[p].replace("/", "/"+chr(10)) for p in POOLS], fontsize=7)
    a.set_ylabel("baseline-feasible corrections\nthat are executed (%)"); a.set_ylim(0, 118); a.set_yticks([0, 25, 50, 75, 100])
    h_, l_ = a.get_legend_handles_labels()
    fig.legend(h_, l_, frameon=False, ncol=6, loc="upper center", bbox_to_anchor=(0.5, 1.08), columnspacing=0.9, handlelength=1.8,
               handleheight=1.0, fontsize=6.3)
    a.set_title("(a)", loc="left")
    e = h[(h.pool == "eth_usdc_005") & (h.variant == "raw")].set_index("mech").etw_mean
    for i, (m, lab) in enumerate(mechs):
        r = e[m] / e["baseline"]
        mbar(b, i, r, 0.7, m); b.text(i, r + 0.05, f"{r:.2f}", ha="center", fontsize=5.5)
    b.axhline(1, color="k", lw=0.8, ls="--")
    b.set_xticks([]); b.set_ylabel("mean price error\nrelative to baseline AMM"); b.set_ylim(0, 3.3)
    b.set_title("(b)", loc="left")
    L = t3[(t3.pool == "eth_usdc_005") & (t3.variant == "raw") & (t3.regime == "median")]
    for i, (m, lab) in enumerate(mechs):
        r = L[(L.mech == m) & ((L.lam == 0.75) | m.startswith("static") | (m == "baseline"))]
        if m == "retained":
            r = r[np.isclose(r.gamma, 0.02)]
        v = r.mean_lp_vs_hodl_bp.iloc[0]
        mbar(c, i, v, 0.7, m); c.text(i, v - 0.0002, f"{v:.4f}", ha="center", va="top", fontsize=5, rotation=90)
    c.axhline(0, color="k", lw=0.8)
    c.set_xticks([]); c.set_ylabel("per-step LP excess over HODL\n(bp per baseline-feasible step)"); c.set_ylim(-0.0047, 0.0003)
    c.set_title("(c)", loc="left")
    fig.tight_layout()
    save(fig, "eval_replay")


# ---------------------------------------------------------------- Fig. 3: stale reference in the replay (replaces Table replay-lag)
def fig_lag():
    tab = R / "e2/tables"
    cfgs = [("$k$=1,\n$d$=1", "e2_bootstrap_test_lag1_med.csv", "e2_bootstrap_test_lag0_med.csv"),
            ("$k$=1,\n$d$=5", "e2_bootstrap_test_lag5_med.csv", "e2_bootstrap_test_lag0_med.csv"),
            ("$k$=5,\n$d$=1", "e2_bootstrap_test_lag1_k5_med.csv", "e2_bootstrap_test_lag0_k5_med.csv"),
            ("$k$=5,\n$d$=5", "e2_bootstrap_test_lag5_k5_med.csv", "e2_bootstrap_test_lag0_k5_med.csv"),
            ("$k$=15,\n$d$=1", "e2_bootstrap_test_lag1_k15_med.csv", "e2_bootstrap_test_lag0_k15_med.csv"),
            ("$k$=15,\n$d$=5", "e2_bootstrap_test_lag5_k15_med.csv", "e2_bootstrap_test_lag0_k15_med.csv")]
    mechs = [(m, MECH[m][0]) for m in ("unconstrained", "retained", "buffered_1eps", "buffered_2eps")]

    def get(f, m, metric):
        d = pd.read_csv(tab / f)
        return d[(d.pool == "eth_usdc_005") & (d.variant == "raw") & (d.mech == m) & (d.metric == metric)].point.iloc[0]

    fig, (a, b) = plt.subplots(1, 2, figsize=(7.2, 2.4))
    w = 0.2
    for i, (m, lab) in enumerate(mechs):
        x = np.arange(len(cfgs)) + (i - 1.5) * w
        viol = [100 * get(f, m, "violation_rate") for _, f, _ in cfgs]
        prot = [100 * get(f, m, "protection_usd") / get(ideal, "retained", "protection_usd") for _, f, ideal in cfgs]
        mbar(a, x, viol, w, m, label=lab)
        mbar(b, x, prot, w, m)
        for xx, y in zip(x, viol):
            a.text(xx, y + 0.6, f"{y:.0f}", ha="center", fontsize=5.5)
        for xx, y in zip(x, prot):
            b.text(xx, y + 1, f"{y:.0f}", ha="center", fontsize=5.5)
    for ax in (a, b):
        ax.set_xticks(range(len(cfgs))); ax.set_xticklabels([c[0] for c in cfgs])
    a.set_ylabel("participation violations (%)"); b.set_ylabel("protection funds relative to\nideal reference, same $k$ (%)")
    b.axhline(100, color="k", lw=0.8, ls="--"); b.set_ylim(0, 118)
    h_, l_ = a.get_legend_handles_labels()
    fig.legend(h_, l_, frameon=False, ncol=4, loc="upper center", bbox_to_anchor=(0.5, 1.07), columnspacing=1.0, handlelength=1.8,
               handleheight=1.0, fontsize=6.5)
    a.set_title("(a)", loc="left"); b.set_title("(b)", loc="left")
    fig.tight_layout()
    save(fig, "eval_lag")


# ---------------------------------------------------------------- Fig. 4: delay x cadence heatmaps (replaces Table oracle)
def fig_heat():
    s = pd.read_csv(R / "e4/e4_summary_test.csv")
    s = s[(s.pool == "eth_usdc_005") & (s.variant == "raw") & (s.eta == 0) & s.delay_min.notna() & (s.calibration == "rolling")]
    ds = sorted(s.delay_min.unique()); ks = sorted(s.cadence_min.unique())

    def grid(delta, col):
        g = np.full((len(ks), len(ds)), np.nan)
        for i, k in enumerate(ks):
            for j, d in enumerate(ds):
                r = s[(s.cadence_min == k) & (s.delay_min == d) & (s.delta.astype(str) == delta)]
                if len(r):
                    g[i, j] = 100 * r[col].iloc[0]
        return g

    panels = [("(a)", grid("0", "violation_rate"), "Reds", 0, 40),
              ("(b)", grid("eps_S", "violation_rate"), "Reds", 0, 40),
              ("(c)", grid("eps_S", "recapture_effective"), "Blues", 0, 70)]
    fig, axs = plt.subplots(1, 3, figsize=(7.4, 2.2))
    for ax, (t, g, cm, lo, hi) in zip(axs, panels):
        im = ax.imshow(g, cmap=cm, vmin=lo, vmax=hi, aspect="auto")
        for i in range(len(ks)):
            for j in range(len(ds)):
                v = g[i, j]
                ax.text(j, i, f"{v:.1f}" if v < 10 else f"{v:.0f}", ha="center", va="center", fontsize=6.5,
                        color="white" if v > 0.6 * hi else "black")
                if ds[j] < ks[i]:  # the reference is younger than the interval between corrections
                    ax.add_patch(mpatches.Rectangle((j - .5, i - .5), 1, 1, fill=False, ec="k", lw=1.3))
        ax.set_xticks(range(len(ds))); ax.set_xticklabels([f"{int(d)}" for d in ds])
        ax.set_yticks(range(len(ks))); ax.set_yticklabels([f"{int(k)}" for k in ks])
        ax.set_xlabel("reference delay $d$ (min)"); ax.grid(False)
        ax.set_title(t, loc="left", fontsize=7.5)
        plt.colorbar(im, ax=ax, fraction=0.05, pad=0.02).ax.tick_params(labelsize=6)
    axs[0].set_ylabel("cadence $k$ (min)")
    fig.tight_layout()
    save(fig, "eval_heat")


# ---------------------------------------------------------------- Fig. 5: size-dependent costs (replaces Table costs)
def fig_costs():
    d = pd.read_parquet(R / "e5/e5_summary.parquet")
    d = d[(d.gas_units == 150000) & (d.bps_rank == 1) & np.isclose(d.curvature, 0.5) & d.pool.isin(POOLS)]
    est = [("gas_only", "gas-only", OI["vermilion"], "--", "s", "////"),
           ("gas_only_buf", r"gas-only, $\delta=\varepsilon_K$", OI["green"], ":", "^", "\\\\"),
           ("exact", "exact", OI["blue"], "-", "o", "")]

    def band(ax, xvar, fixed, metric):
        dd = d[np.isclose(d[fixed[0]], fixed[1])]
        for k, lab, col, ls, mk, hatch in est:
            g = dd[dd.k_hat == k]
            piv = g.pivot_table(index=xvar, columns="pool", values=metric)
            ax.fill_between(piv.index, piv.min(axis=1), piv.max(axis=1), facecolor=col, alpha=0.18, lw=0)
            ax.fill_between(piv.index, piv.min(axis=1), piv.max(axis=1), facecolor="none", edgecolor=col, hatch=hatch, lw=0, alpha=0.5)
            ax.plot(piv.index, piv["eth_usdc_005"], color=col, ls=ls, marker=mk, ms=3, label=lab)

    fig, axs = plt.subplots(1, 3, figsize=(7.4, 2.3))
    band(axs[0], "lam", ("gamma", 0.02), "q_ratio_mean"); axs[0].set_xlabel(r"sharing rate $\lambda$"); axs[0].set_ylabel(r"trade selection $q^{BR}/q^0$")
    band(axs[1], "gamma", ("lam", 0.75), "q_ratio_mean"); axs[1].set_xlabel(r"retained fraction $\gamma$")
    band(axs[2], "lam", ("gamma", 0.02), "execution_rate"); axs[2].set_xlabel(r"sharing rate $\lambda$"); axs[2].set_ylabel("execution rate")
    for ax, t in zip(axs, ["(a)", "(b)", "(c)"]):
        ax.set_ylim(0, 1.05); ax.set_title(t, loc="left", fontsize=8)
    axs[0].legend(frameon=False, loc="lower left", handlelength=2.2)
    fig.tight_layout()
    save(fig, "eval_costs")


# ---------------------------------------------------------------- Fig. 6: fragmentation leakage vs gas (replaces Table frag)
def fig_frag():
    c = pd.read_csv(R / "e6/tables/e6_checks_test.csv")
    fig, (a, b) = plt.subplots(1, 2, figsize=(7.2, 2.3), gridspec_kw={"width_ratios": [1.6, 1]})
    for i, p in enumerate(POOLS):
        g = c[(c.pool == p) & (c.variant == VARIANT[p])].sort_values("n_fragments")
        col, mk, ls = POOL_STYLE[p]
        a.plot(g.n_fragments, 100 * g.indep_over_cumulative_equal, marker=mk, ms=3, ls=ls, color=col, label=SHORT[p])
    a.axhline(0.0, color="k", lw=0)
    a.plot([1, 16], [100, 100], color="k", ls=(0, (6, 1.5, 1, 1.5, 1, 1.5)), lw=0.9, label="cumulative rule (all $n$)")
    a.set_xscale("log", base=2); a.set_xticks([1, 2, 4, 8, 16]); a.set_xticklabels([1, 2, 4, 8, 16])
    a.set_yscale("log"); a.set_ylim(0.4, 150); a.set_yticks([1, 10, 100]); a.set_yticklabels(["1", "10", "100"])
    a.set_xlabel("number of fragments $n$"); a.set_ylabel("independent-rule transfer\nrelative to cumulative (%)")
    a.legend(frameon=False, ncol=2, fontsize=6.5, loc="lower left", handlelength=2.2)
    a.set_title("(a)", loc="left", fontsize=8)
    ns = [1, 2, 4, 8, 16]
    gas = [GAS_FIRST + GAS_EXTRA * (n - 1) for n in ns]
    b.bar(range(5), gas, 0.65, color=OI["grey"], **BAR_EDGE)
    for i, gv in enumerate(gas):
        b.text(i, gv + 4, f"{gv:.0f}k", ha="center", fontsize=6)
    b.set_xticks(range(5)); b.set_xticklabels(ns); b.set_xlabel("number of fragments $n$"); b.set_ylabel("hook overhead (k gas)")
    b.set_title("(b)", loc="left", fontsize=8)
    fig.tight_layout()
    save(fig, "eval_frag")


# ---------------------------------------------------------------- Appendix: sensitivity to lambda and gamma
def fig_sens():
    d = pd.read_csv(R / "e3/tables/e3_totals_test.csv")
    lams = [0.25, 0.5, 0.75, 0.95]; gams = [0.0, 0.005, 0.02, 0.05, 0.10]
    fig, axs = plt.subplots(2, len(POOLS), figsize=(7.4, 3.6))
    for j, p in enumerate(POOLS):
        x = d[(d.pool == p) & (d.variant == VARIANT[p])]
        base = x[x.mech == "baseline"].etw_mean.iloc[0]
        P = np.full((4, 5), np.nan); E = np.full((4, 5), np.nan)
        for i, l in enumerate(lams):
            for k, g in enumerate(gams):
                r = x[(x.mech == "cap_gamma0") & (x.lam == l)] if g == 0 else x[(x.mech == "retained") & (x.lam == l) & np.isclose(x.gamma, g)]
                P[i, k] = r.protection_usd.iloc[0]; E[i, k] = r.etw_mean.iloc[0] / base
        for row, (M, cm, lo, hi, fmt) in enumerate([(P / np.nanmax(P), "Blues", 0, 1, None), (E, "Reds", 1, 1.6, "{:.2f}")]):
            ax = axs[row, j]
            ax.imshow(M, cmap=cm, vmin=lo, vmax=hi, aspect="auto")
            for i in range(4):
                for k in range(5):
                    if row == 0:
                        v = P[i, k]; t = f"{v/1000:.1f}k" if v >= 1000 else (f"{v:.0f}" if v >= 10 else f"{v:.1f}")
                        dark = M[i, k] > 0.6
                    else:
                        t = fmt.format(E[i, k]); dark = E[i, k] > 1.35
                    ax.text(k, i, t, ha="center", va="center", fontsize=5.5, color="white" if dark else "black")
            ax.add_patch(mpatches.Rectangle((2 - .5, 2 - .5), 1, 1, fill=False, ec="k", lw=1.4))
            ax.set_xticks(range(5)); ax.set_xticklabels(["0\n(cap)", "0.005", "0.02", "0.05", "0.10"], fontsize=5.5)
            ax.set_yticks(range(4)); ax.set_yticklabels(lams if j == 0 else [], fontsize=6)
            ax.grid(False)
            if row == 0: ax.set_title(SHORT[p], fontsize=7.5)
            if row == 1: ax.set_xlabel(r"$\gamma$")
            if j == 0: ax.set_ylabel(r"$\lambda$")
    # no in-figure row headers: the caption says what the top and bottom rows show
    fig.subplots_adjust(left=0.06, right=0.995, top=0.93, bottom=0.10, wspace=0.12, hspace=0.35)
    save(fig, "eval_sens")


# ---------------------------------------------------------------- Frontier: protection funds against price error as lambda varies
def fig_frontier():
    d = pd.read_csv(R / "e3/tables/e3_totals_test.csv")
    d = d[d.regime == "median"]
    style = {m: (MECH[m][0], MECH[m][1], MECH[m][3], MECH[m][4]) for m in ("static_0.05pct", "static_0.30pct", "unconstrained", "cap_gamma0")}
    style["retained"] = (r"Retained margin ($\gamma=0.02$)", MECH["retained"][1], MECH["retained"][3], MECH["retained"][4])
    fig, axs = plt.subplots(1, len(POOLS), figsize=(7.4, 2.4))
    for ax, p in zip(axs, POOLS):
        x = d[(d.pool == p) & (d.variant == VARIANT[p])]
        X, Y = x.etw_mean.to_numpy(), x.protection_usd.to_numpy()
        b = x[x.mech == "baseline"].iloc[0]
        ax.scatter([b.etw_mean], [b.protection_usd], color="k", marker="*", s=30, zorder=3, label="Baseline AMM")
        for m, (lab, col, mk, ls) in style.items():
            g = x[(x.mech == m) & (np.isclose(x.gamma, 0.02) if m == "retained" else True)].sort_values("lam")
            if m.startswith("static"):
                g = g.iloc[:1]
            ax.plot(g.etw_mean, g.protection_usd, marker=mk, ms=3.5, ls=ls, lw=0.9 if len(g) > 1 else 0, color=col, mec="k", mew=0.3,
                    label=lab)
        ax.set_xscale("log"); ax.set_title(SHORT[p], fontsize=8); ax.set_xlabel("mean price error $E_{TW}$")
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    axs[0].set_ylabel("funds available for\nLP protection (USD)")
    h_, l_ = axs[0].get_legend_handles_labels()
    fig.legend(h_, l_, frameon=False, ncol=6, loc="upper center", bbox_to_anchor=(0.5, 1.09), columnspacing=0.8, handlelength=2.0, fontsize=6)
    fig.tight_layout()
    fig.canvas.draw()
    plotstyle.ensure_all_xticks(fig)  # at least two labelled, non-overlapping x ticks per panel (log axes)
    save(fig, "eval_frontier")


if __name__ == "__main__":
    for f in (fig_replay, fig_lag, fig_heat, fig_costs, fig_frag, fig_sens, fig_frontier):
        f()
        print("ok", f.__name__)
