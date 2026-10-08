"""Figures of Section 5 (Evaluation) that replace tables. Run from the repository root:  python paper/make_eval_figures.py
Every number is read from results/ (no hard-coded values). Output: paper/figures/*.pdf

  --run NAME      read results/NAME/ (a RESULTS_RUN root) instead of results/
  --results DIR   base results directory (default: <repo>/results)
  --out DIR       output directory (default: paper/figures)
  --figs a,b      only these figures (eval_replay, eval_lag, eval_heat, eval_costs, eval_sens)
When e3/tables/e3_totals_test.csv is absent (a run root has no E3), its lambda x gamma rows are taken from
e2/e2_summary_test.parquet: the median-regime rows and E3's columns (experiments/e3_pareto_frontier.py writes the same).
"""
import argparse
import sys
from pathlib import Path

import matplotlib
import matplotlib.patches as mpatches
import matplotlib.ticker
from matplotlib.legend_handler import HandlerTuple
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import plotstyle  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

R = ROOT / "results"
OUT = ROOT / "paper" / "figures"
plotstyle.apply()
plt.rcParams.update({"font.size": 8, "axes.titlesize": 8, "axes.labelsize": 8, "legend.fontsize": 7})

# USDC/USDT is excluded (its fee tier is 0.001%, not the 0.01% of the model; results/DECISIONS.md W8)
POOLS = ["eth_usdc_005", "eth_wbtc_030", "eth_wsteth_001"]
MAIN_POOLS = ["eth_usdc_005", "eth_wbtc_030"]   # main-text figures (Figs. 2 and 5); ETH/wstETH stays in Fig. 6 and the appendix tables
SHORT = {"eth_usdc_005": "ETH/USDC", "usdc_usdt_0001": "USDC/USDT", "eth_wbtc_030": "ETH/WBTC", "eth_wsteth_001": "ETH/wstETH"}
VARIANT = {"eth_usdc_005": "raw", "usdc_usdt_0001": "raw", "eth_wbtc_030": "corr24h", "eth_wsteth_001": "corr24h"}
C = plotstyle.COLORS

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
        "buffered_1eps": (r"Buffered $\varepsilon$", OI["green"], "----", "s", "--"),
        "buffered_2eps": (r"Buffered $2\varepsilon$", OI["yellow"], "||||", "^", ":"),
        # E8 baselines (results/e8): drawn only in the frontier panels of Fig. eval_replay
        "dynfee": (r"Dynamic fee ($\beta$ grid)", OI["black"], "", "X", "-.")}
# pool -> (colour, marker, line style)
POOL_STYLE = {"eth_usdc_005": (OI["blue"], "o", "-"), "usdc_usdt_0001": (OI["vermilion"], "s", "--"),
              "eth_wbtc_030": (OI["green"], "^", "-."), "eth_wsteth_001": (OI["purple"], "D", ":")}
BAR_EDGE = {"edgecolor": "black", "linewidth": 0.4}
plt.rcParams["hatch.linewidth"] = 0.5


def mbar(ax, x, h, w, m, **kw):
    """Bar of mechanism m with its colour and hatch."""
    lab, col, hatch, _, _ = MECH[m]
    return ax.bar(x, h, w, color=col, hatch=hatch, **BAR_EDGE, **kw)


E3_COLS = ["pool", "variant", "regime", "mech", "lam", "gamma", "protection_usd", "searcher_net_usd", "executed_correction", "execution_rate",
           "etw_mean", "etw_p95", "mean_lp_vs_hodl_bp"]


def e3_totals():
    """E3's lambda x gamma totals; without the E3 file, the same rows from the E2 summary (median regime)."""
    f = R / "e3/tables/e3_totals_test.csv"
    if f.exists():
        return pd.read_csv(f)
    s = pd.read_parquet(R / "e2/e2_summary_test.parquet")
    return s[s.regime == "median"][E3_COLS].reset_index(drop=True)


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.pdf", bbox_inches="tight")
    fig.savefig(OUT / f"{name}.png", bbox_inches="tight", dpi=200)
    plt.close(fig)


# ---------------------------------------------------------------- Fig. eval_replay: execution rate per pool and the recovery/price-error frontier
def frontier_panel(ax, d, p, e8=None):
    """Protection funds against mean price error as lambda varies, one pool. Static charges are single points. With `e8`
    (E8 test summary, median R) also the dynamic fee over its beta grid (calibrated beta as an open marker)."""
    style = {m: (MECH[m][0], MECH[m][1], MECH[m][3], MECH[m][4]) for m in ("static_0.05pct", "static_0.30pct", "unconstrained", "cap_gamma0", "retained")}
    x = d[(d.pool == p) & (d.variant == VARIANT[p])]
    b = x[x.mech == "baseline"].iloc[0]
    ax.scatter([b.etw_mean], [b.protection_usd], color=MECH["baseline"][1], edgecolor="k", linewidth=0.4, marker="*", s=45, zorder=3,
               label=MECH["baseline"][0])
    for m, (lab, col, mk, ls) in style.items():
        g = x[(x.mech == m) & (np.isclose(x.gamma, 0.02) if m == "retained" else True)].sort_values("lam")
        if m.startswith("static"):
            g = g.iloc[:1]
        ax.plot(g.etw_mean, g.protection_usd, marker=mk, ms=3.5, ls=ls, lw=0.9 if len(g) > 1 else 0, color=col, mec="k", mew=0.3, label=lab)
    if e8 is not None:                                   # replayed baselines (E8, same traces and regime)
        y = e8[e8.pool == p]
        g = y[y.mech.str.startswith("dynfee_b")].assign(beta=lambda z: z.mech.str[8:].astype(float)).sort_values("beta")
        g = g[g.etw_mean <= 3 * b.etw_mean]              # the largest betas leave the pool far off the benchmark (appendix table)
        lab, col, mk, ls = MECH["dynfee"][0], MECH["dynfee"][1], MECH["dynfee"][3], MECH["dynfee"][4]
        ax.plot(g.etw_mean, g.protection_usd, marker=mk, ms=3.2, ls=ls, lw=0.8, color=col, mec="k", mew=0.3, label=lab)
        cal = y[y.mech == "dynfee_cal"].iloc[0]
        ax.scatter([cal.etw_mean], [cal.protection_usd], marker="o", s=38, facecolor="none", edgecolor=col, linewidth=0.9, zorder=4,
                   label=r"dynamic fee (calibrated $\beta$)")
        # the E8 MEV-tax rows are not drawn: under competitive bidding they only restate t/(1+t) of the baseline margin (DECISIONS I9)
    ax.set_xscale("log"); ax.set_xlabel("mean price error $E_{TW}$")
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.text(0.97, 0.04, SHORT[p], transform=ax.transAxes, ha="right", va="bottom", fontsize=7.5)


def fig_replay():
    h = pd.read_csv(R / "e2/tables/e2_headline_test.csv")
    d = e3_totals()
    d = d[d.regime == "median"]
    mechs = [(m, MECH[m][0]) for m in ("baseline", "static_0.05pct", "static_0.30pct", "unconstrained", "cap_gamma0", "retained")]
    fig, (a, b, c) = plt.subplots(1, 3, figsize=(7.4, 2.3), gridspec_kw={"width_ratios": [1.5, 1, 1]})
    w = 0.135
    for i, (m, lab) in enumerate(mechs):
        v = [100 * h[(h.pool == p) & (h.variant == VARIANT[p]) & (h.mech == m)].execution_rate.iloc[0] for p in MAIN_POOLS]
        xs = np.arange(len(MAIN_POOLS)) + (i - 2.5) * w
        mbar(a, xs, v, w, m, label=lab)
        for x, y in zip(xs, v):
            a.text(x, y + 1.5, f"{y:.0f}" if y >= 10 else f"{y:.1f}", ha="center", va="bottom", fontsize=5.5, rotation=90)
    a.set_xticks(range(len(MAIN_POOLS))); a.set_xticklabels([SHORT[p].replace("/", "/" + chr(10)) for p in MAIN_POOLS], fontsize=7)
    a.set_ylabel("baseline-feasible corrections\nthat are executed (%)"); a.set_ylim(0, 118); a.set_yticks([0, 25, 50, 75, 100])
    a.set_title("(a)", loc="left")
    e8 = pd.read_parquet(R / "e8/e8_summary_test.parquet")
    e8 = e8[e8.regime == "median"]
    frontier_panel(b, d, "eth_usdc_005", e8); b.set_title("(b)", loc="left"); b.set_ylabel("funds available for\nLP protection (USD)")
    frontier_panel(c, d, "eth_wbtc_030", e8); c.set_title("(c)", loc="left"); c.set_ylabel("funds available for\nLP protection (USD)")
    # One shared legend for (a)-(c). Each rule shows its bar in (a) next to its marker and line in (b, c): a line for the
    # curves over lambda (and over beta for the dynamic fee), a marker alone for the single points (static charges, baseline).
    # (b) and (c) carry the same series, so their handles are taken from (b) only. Column-major order groups the entries:
    # baseline and unconstrained | static charges | participation-aware rules | dynamic fee.
    ha = dict(zip(*a.get_legend_handles_labels()[::-1]))
    hb = dict(zip(*b.get_legend_handles_labels()[::-1]))
    order = ["baseline", "unconstrained", "static_0.05pct", "static_0.30pct", "cap_gamma0", "retained"]
    h_ = [(ha[MECH[m][0]], hb[MECH[m][0]]) for m in order] + [hb[MECH["dynfee"][0]], hb[r"dynamic fee (calibrated $\beta$)"]]
    l_ = [MECH[m][0] for m in order] + [MECH["dynfee"][0], r"dynamic fee (calibrated $\beta$)"]
    fig.legend(h_, l_, frameon=False, ncol=4, loc="upper center", bbox_to_anchor=(0.5, 1.13), columnspacing=0.9, handlelength=2.6,
               handleheight=1.0, fontsize=6.3, handler_map={tuple: HandlerTuple(ndivide=None, pad=0.3)})
    fig.tight_layout()
    fig.canvas.draw()
    plotstyle.ensure_all_xticks(fig)  # at least two labelled, non-overlapping x ticks per panel (log axes in b and c)
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

    fig, (a, b) = plt.subplots(1, 2, figsize=(7.2, 2.15))
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

    panels = [("(a)", grid("0", "violation_rate"), "Reds", 0, 40, "participation violations (%)"),
              ("(b)", grid("eps_S", "violation_rate"), "Reds", 0, 40, "participation violations (%)"),
              ("(c)", grid("eps_S", "recapture_effective"), "Blues", 0, 70, "effective recapture (%)")]
    fig, axs = plt.subplots(1, 3, figsize=(7.4, 2.05))
    for ax, (t, g, cm, lo, hi, clab) in zip(axs, panels):
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
        cb = plt.colorbar(im, ax=ax, fraction=0.05, pad=0.02)
        cb.ax.tick_params(labelsize=6)
        cb.set_label(clab, fontsize=6.5)
        ax.set_ylabel("cadence $k$ (min)")       # on every panel, so no panel relies on its neighbour's axis title
    fig.tight_layout()
    save(fig, "eval_heat")


# ---------------------------------------------------------------- Fig. 5: size-dependent costs (replaces Table costs)
def fig_costs():
    d = pd.read_parquet(R / "e5/e5_summary.parquet")
    d = d[(d.gas_units == 150000) & (d.bps_rank == 1) & np.isclose(d.curvature, 0.5) & d.pool.isin(MAIN_POOLS)]
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

    fig, axs = plt.subplots(1, 4, figsize=(7.4, 2.0))
    band(axs[0], "lam", ("gamma", 0.02), "q_ratio_mean"); axs[0].set_xlabel(r"sharing rate $\lambda$"); axs[0].set_ylabel(r"trade selection $q^{BR}/q^0$")
    band(axs[1], "gamma", ("lam", 0.75), "q_ratio_mean"); axs[1].set_xlabel(r"retained fraction $\gamma$"); axs[1].set_ylabel(r"trade selection $q^{BR}/q^0$")
    band(axs[2], "lam", ("gamma", 0.02), "execution_rate"); axs[2].set_xlabel(r"sharing rate $\lambda$"); axs[2].set_ylabel("execution rate")
    for ax, t in zip(axs, ["(a)", "(b)", "(c)", "(d)"]):
        ax.set_title(t, loc="left", fontsize=8)
    for ax in axs[:3]:
        ax.set_ylim(0, 1.05)
    axs[0].legend(frameon=False, loc="lower left", handlelength=2.2, fontsize=6)
    frag_panel(axs[3])
    fig.tight_layout()
    save(fig, "eval_costs")


def frag_panel(a):
    """Independent-rule transfer relative to the block-scoped rule against the number of fragments (panel (d) of eval_costs)."""
    c = pd.read_csv(R / "e6/tables/e6_checks_test.csv")
    for i, p in enumerate(MAIN_POOLS):
        g = c[(c.pool == p) & (c.variant == VARIANT[p])].sort_values("n_fragments")
        col, mk, ls = POOL_STYLE[p]
        a.plot(g.n_fragments, 100 * g.indep_over_cumulative_equal, marker=mk, ms=3, ls=ls, color=col, label=SHORT[p])
    a.plot([1, 16], [100, 100], color="k", ls=(0, (6, 1.5, 1, 1.5, 1, 1.5)), lw=0.9, label="block-scoped rule")
    a.set_xscale("log", base=2); a.set_xticks([1, 2, 4, 8, 16]); a.set_xticklabels([1, 2, 4, 8, 16])
    a.set_yscale("log"); a.set_ylim(0.1, 150); a.set_yticks([0.1, 1, 10, 100]); a.set_yticklabels(["0.1", "1", "10", "100"])
    a.set_xlabel("number of fragments $m$"); a.set_ylabel("independent-rule transfer\nrelative to block-scoped (%)")
    a.legend(frameon=False, ncol=1, fontsize=5.5, loc="lower left", handlelength=2.0, labelspacing=0.2)


# ---------------------------------------------------------------- Appendix: sensitivity to lambda and gamma
def fig_sens():
    d = e3_totals()
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


FIGS = {"eval_replay": fig_replay, "eval_lag": fig_lag, "eval_heat": fig_heat, "eval_costs": fig_costs, "eval_sens": fig_sens}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=None, help="read results/<run>/ instead of results/")
    ap.add_argument("--results", default=str(ROOT / "results"), help="base results directory")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--figs", default=",".join(FIGS))
    a = ap.parse_args()
    R = Path(a.results) / a.run if a.run else Path(a.results)
    OUT = Path(a.out)
    for name in a.figs.split(","):
        FIGS[name]()
        print("ok", name)
