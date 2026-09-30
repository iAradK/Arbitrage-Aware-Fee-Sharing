import numpy as np
from hypothesis import given, settings, strategies as st

from common import cpmm

fee = st.sampled_from([1e-4, 5e-4, 3e-3])


@given(x=st.floats(10, 1e4), p=st.floats(0.5, 5e3), f=fee, ratio=st.floats(1.01, 1.5), n=st.integers(2, 16), up=st.booleans())
@settings(max_examples=100)
def test_split_path_independence(x, p, f, ratio, n, up):
    y = x * p
    pi = p * ratio if up else p / ratio
    d, n_full = cpmm.full_correction_net(x, y, f, pi)
    if d == 0 or n_full <= 0:
        return
    whole = cpmm.trade(x, y, f, d, n_full)
    cx, cy, d0, d1 = x, y, 0.0, 0.0
    for _ in range(n):
        t = cpmm.trade(cx, cy, f, d, n_full / n)
        cx, cy, d0, d1 = float(t["x"]), float(t["y"]), d0 + float(t["d0"]), d1 + float(t["d1"])
    assert np.isclose(cx, whole["x"], rtol=1e-9) and np.isclose(cy, whole["y"], rtol=1e-9)
    assert np.isclose(d0, whole["d0"], rtol=1e-7, atol=1e-9) and np.isclose(d1, whole["d1"], rtol=1e-7, atol=1e-9)


@given(x=st.floats(10, 1e4), p=st.floats(0.5, 5e3), f=fee, ratio=st.floats(1.01, 1.5), up=st.booleans())
@settings(max_examples=100)
def test_full_correction_maximises_surplus(x, p, f, ratio, up):
    y = x * p
    pi = p * ratio if up else p / ratio
    d, n = cpmm.full_correction_net(x, y, f, pi)
    if d == 0 or n <= 0:
        return
    s0, _ = cpmm.surplus_of(x, y, f, d, n, pi)
    for k in (0.9, 0.99, 1.01, 1.1):
        s1, _ = cpmm.surplus_of(x, y, f, d, n * k, pi)
        assert s0 >= s1 - 1e-9 * max(1, float(s0))


def test_no_trade_inside_fee_band():
    d, n = cpmm.full_correction_net(100.0, 1e5, 3e-3, 1000.0 * 1.001)
    assert d == 0 and n == 0


def test_smallest_net_for_surplus_inverts():
    x, y, f, pi = 1000.0, 1e6, 0.0, 1100.0
    d, n = cpmm.full_correction_net(x, y, f, pi)
    s_full, _ = cpmm.surplus_of(x, y, f, d, n, pi)
    target = 0.3 * float(s_full)
    q = cpmm.smallest_net_for_surplus(x, y, f, d, n, pi, target)
    s, _ = cpmm.surplus_of(x, y, f, d, q, pi)
    assert abs(float(s) - target) < 1e-6 * target
