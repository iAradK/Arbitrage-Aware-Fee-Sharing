"""One matplotlib style for every figure. Greek axis labels via mathtext."""
import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402
import numpy as np  # noqa: E402

POOL_LABEL = {"eth_usdc_005": "ETH/USDC 0.05%", "usdc_usdt_0001": "USDC/USDT 0.01%",
              "eth_wbtc_030": "ETH/WBTC 0.30%", "eth_wsteth_001": "ETH/wstETH 0.01%"}
COLORS = ["#1f4e79", "#c0392b", "#2e8b57", "#8e6c00", "#6c3483", "#5d6d7e", "#d35400", "#117a8b"]
GAMMA, LAMBDA, DELTA, ETA = r"$\gamma$", r"$\lambda$", r"$\delta$", r"$\eta$"


def _log_label(v: float, _pos=None) -> str:
    """2e-3 -> $2\\times10^{-3}$, 1e-3 -> $10^{-3}$ (compact mathtext for log-axis ticks)."""
    if v <= 0:
        return ""
    e = int(math.floor(math.log10(v) + 1e-9))
    m = v / 10 ** e
    ms = f"{m:.3g}"
    return f"$10^{{{e}}}$" if ms == "1" else f"${ms}\\times10^{{{e}}}$"


def ensure_xticks(ax, min_ticks: int = 2, max_ticks: int = 5) -> None:
    """Guarantee at least `min_ticks` labelled x ticks inside the visible range.

    Linear axes almost always have enough; log axes spanning less than a decade show no labelled major tick, so
    ticks are placed at 1, 2, 3, 5 x 10^k (thinned to at most `max_ticks`), or, for very narrow ranges, at
    `min_ticks + 1` evenly spaced points in log space rounded to two significant figures."""
    if not ax.get_visible() or not ax.xaxis.get_visible():
        return
    lo, hi = sorted(ax.get_xlim())
    visible = [t for t in ax.get_xticks() if lo <= t <= hi]
    labelled = [t for t in visible if ax.xaxis.get_major_formatter()(t) not in ("", None)]
    if len(labelled) >= min_ticks:
        return
    if ax.get_xscale() == "log" and lo > 0:
        L0, W = math.log10(lo), math.log10(hi) - math.log10(lo)
        pos = lambda v: (math.log10(v) - L0) / W                      # noqa: E731  fraction of the axis width
        # candidates 1, 5, 2, 3 x 10^k in that priority; keep one only if it is at least `min_gap` of the axis width
        # away from every kept tick, so mathtext labels never overlap on narrow panels
        min_gap = 0.25
        cands = []
        for m in (1, 5, 2, 3):
            for e in range(int(math.floor(L0)), int(math.ceil(math.log10(hi))) + 1):
                v = m * 10.0 ** e
                if lo <= v <= hi and all(abs(pos(v) - pos(c)) >= min_gap for c in cands):
                    cands.append(v)
        cands = sorted(cands)[:max_ticks]
        if len(cands) < min_ticks:
            # very narrow range: points at 1/4 and 3/4 of the axis (log space), rounded to the fewest significant
            # figures (2 or 3, the label precision) that keep them distinct and inside the axis
            raw = [10 ** (L0 + f * W) for f in np.linspace(0.25, 0.75, min_ticks)]
            for sf in (2, 3):
                cands = [float(f"{v:.{sf}g}") for v in raw]
                if len(set(cands)) == len(cands) and all(lo <= c <= hi for c in cands):
                    break
        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_log_label))
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        # drop ticks whose rendered labels overlap (narrow panels), least important mantissa first (3, 2, 5, then 1)
        rank = lambda v: {1: 0, 5: 1, 2: 2, 3: 3}.get(round(v / 10 ** math.floor(math.log10(v) + 1e-9)), 4)  # noqa: E731
        while True:
            ax.xaxis.set_major_locator(matplotlib.ticker.FixedLocator(cands))
            if len(cands) <= min_ticks:
                break
            ax.figure.canvas.draw()
            boxes = sorted((t.get_window_extent().x0, t.get_window_extent().x1)
                           for t in ax.get_xticklabels() if t.get_text() and t.get_visible())
            if all(b[0] - a[1] >= 3 for a, b in zip(boxes, boxes[1:])):
                break
            cands.remove(max(cands, key=rank))
    else:
        ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=max_ticks, min_n_ticks=min_ticks))


def ensure_all_xticks(fig, min_ticks: int = 2) -> None:
    for ax in fig.axes:
        if ax.has_data() or ax.lines or ax.collections:
            ensure_xticks(ax, min_ticks)


def apply():
    plt.rcParams.update({
        "figure.dpi": 120, "savefig.dpi": 200, "font.size": 9, "axes.titlesize": 9, "axes.labelsize": 9,
        "legend.fontsize": 8, "axes.grid": True, "grid.alpha": 0.25, "axes.spines.top": False,
        "axes.spines.right": False, "axes.prop_cycle": matplotlib.cycler(color=COLORS),
        "pdf.fonttype": 42, "font.family": "serif", "mathtext.fontset": "cm",
    })
