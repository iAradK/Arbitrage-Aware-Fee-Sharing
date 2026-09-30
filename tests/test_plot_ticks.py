"""Every saved figure must show at least two labelled x ticks per panel (common.plotstyle.ensure_xticks)."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pytest  # noqa: E402

from common import plotstyle, reporting  # noqa: E402


def _labelled(ax):
    ax.figure.canvas.draw()
    lo, hi = sorted(ax.get_xlim())
    return [t.get_text() for t in ax.get_xticklabels() if t.get_text() and lo <= t.get_position()[0] <= hi]


@pytest.mark.parametrize("lo,hi", [(1.40e-3, 1.47e-3), (1.4e-3, 2.9e-3), (6e-4, 2.2e-3), (5.5e-5, 5e-4), (1e-5, 1e-1)])
def test_log_axis_gets_two_labelled_ticks(tmp_path, lo, hi):
    plotstyle.apply()
    fig, ax = plt.subplots()
    ax.plot([lo, hi], [0, 1])
    ax.set_xscale("log")
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    reporting.savefig(fig, tmp_path / "f")
    labs = _labelled(ax)
    assert len(labs) >= 2, labs
    assert len(labs) <= 6, labs
    # labelled ticks are spread out (no overlapping labels): at least 20% of the axis width apart in log space
    import math
    xs = sorted(t.get_position()[0] for t in ax.get_xticklabels() if t.get_text() and lo <= t.get_position()[0] <= hi)
    w = math.log10(hi) - math.log10(lo)
    assert all((math.log10(b) - math.log10(a)) / w >= 0.2 for a, b in zip(xs, xs[1:])), xs
    plt.close(fig)


def test_narrow_panel_labels_do_not_overlap(tmp_path):
    plotstyle.apply()
    fig, axs = plt.subplots(1, 4, figsize=(7.4, 2.4))
    for ax in axs:
        ax.plot([5.5e-5, 5e-4], [0, 1])
        ax.set_xscale("log")
    reporting.savefig(fig, tmp_path / "n")
    for ax in axs:
        boxes = sorted((t.get_window_extent().x0, t.get_window_extent().x1) for t in ax.get_xticklabels() if t.get_text())
        assert len(boxes) >= 2
        assert all(b[0] - a[1] >= 0 for a, b in zip(boxes, boxes[1:])), boxes
    plt.close(fig)


def test_linear_and_categorical_axes_unchanged(tmp_path):
    plotstyle.apply()
    fig, (a, b) = plt.subplots(1, 2)
    a.plot([0, 60], [0, 1])
    b.plot(["Q1", "Q2", "Q3", "Q4"], [1, 2, 3, 4])
    before = list(b.get_xticks())
    reporting.savefig(fig, tmp_path / "g")
    assert len(_labelled(a)) >= 2
    assert list(b.get_xticks()) == before and len(_labelled(b)) == 4
    plt.close(fig)
