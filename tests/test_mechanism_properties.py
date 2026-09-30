"""Property tests: Property 1, Theorems 2-5, Corollary 1, fixed-point vs float."""
import importlib.util
from pathlib import Path

import numpy as np
from hypothesis import given, settings, strategies as st

from common import fixedpoint as fp
from common import mechanism as mech

REPO = Path(__file__).resolve().parent.parent
pos = st.floats(min_value=1e-3, max_value=1e6, allow_nan=False, allow_infinity=False)
unit = st.floats(min_value=0.0, max_value=1.0)
gam_open = st.floats(min_value=1e-3, max_value=0.999)


@given(S=pos, C=pos, R=st.floats(0, 1e5), lam=unit, gamma=st.floats(0, 0.999))
def test_property1_participation_preserved(S, C, R, lam, gamma):
    K = C + R
    if not S - C >= R:
        return
    r = float(mech.transfer_ideal(S, K, lam, gamma))
    M = S - K
    assert 0.0 <= r <= M + 1e-9 * max(1, S)
    assert S - C - r >= R - 1e-9 * max(1, S)


@given(data=st.data(), lam=unit, gamma=gam_open, C=pos, R=st.floats(0, 1e4))
@settings(max_examples=200)
def test_theorem2_baseline_optimum_preserved(data, lam, gamma, C, R):
    n = data.draw(st.integers(2, 12))
    base = data.draw(st.lists(st.floats(0, 1e5), min_size=n, max_size=n, unique=True))
    S = np.sort(np.array(base)) + np.arange(n) * 1.0   # distinct, gaps >= 1
    K = C + R
    feas = S - C >= R
    if feas.sum() < 2:
        return
    Sf = S[feas]
    pay = Sf - C - mech.transfer_ideal(Sf, K, lam, gamma)
    assert np.argmax(pay) == np.argmax(Sf)
    assert (np.diff(pay) > -1e-9).all()


@given(S=pos, C=pos, R=st.floats(0, 1e4), eS=st.floats(0, 1e3), eK=st.floats(0, 1e3), lam=unit,
       gamma=st.floats(0, 0.999), oS=unit, oK=unit, slack=st.floats(0, 10))
def test_theorem3_buffer_preserves_participation(S, C, R, eS, eK, lam, gamma, oS, oK, slack):
    if not S - C >= R:
        return
    K = C + R
    S_hat = S + (2 * oS - 1) * eS          # |S_hat - S| <= eS
    K_hat = K - oK * eK                    # K - K_hat <= eK
    delta = eS + eK + slack
    r = float(mech.transfer_est(S_hat, K_hat, lam, gamma, delta))
    assert S - C - r >= R - 1e-9 * max(1, S, eS, eK)


seq = st.lists(st.floats(0, 1e5), min_size=1, max_size=16)


@given(inc=seq, K=pos, lam=unit, gamma=st.floats(0, 0.999), delta=st.floats(0, 1e3))
def test_theorem4_monotone_partition_float(inc, K, lam, gamma, delta):
    A = np.cumsum(inc)
    r = mech.watermark_charges(A, K, lam, gamma, delta)
    F = float(mech.F_scalar(A[-1], K, lam, gamma, delta))
    assert abs(r.sum() - F) <= 1e-9 * max(1.0, F)
    assert (r >= -1e-12).all()


@given(A=st.lists(st.integers(0, 10**24), min_size=1, max_size=16), K=st.integers(0, 10**23), lam=st.integers(0, fp.WAD),
       gamma=st.integers(0, fp.WAD - 1), delta=st.integers(0, 10**22))
def test_theorem5_any_sequence_integer_exact(A, K, lam, gamma, delta):
    rows = fp.cumulative_charges(A, K, lam, gamma, delta)
    assert sum(r["charge"] for r in rows) == max(r["target"] for r in rows)
    assert all(r["charge"] >= 0 for r in rows)


@given(inc=st.lists(st.integers(0, 10**23), min_size=1, max_size=16), K=st.integers(0, 10**23), lam=st.integers(0, fp.WAD),
       gamma=st.integers(0, fp.WAD - 1), delta=st.integers(0, 10**22))
def test_theorem4_monotone_partition_integer_exact(inc, K, lam, gamma, delta):
    A = [int(v) for v in np.cumsum(np.array(inc, dtype=object))]
    rows = fp.cumulative_charges(A, K, lam, gamma, delta)
    assert sum(r["charge"] for r in rows) == fp.transfer_wad(A[-1], K, lam, gamma, delta)[0]


@given(S=pos, C=pos, R=st.floats(0, 1e4), extra=st.floats(0, 1e3), n=st.integers(2, 16), lam=unit, gamma=st.floats(0, 0.999))
def test_corollary1_fragmentation_never_improves(S, C, R, extra, n, lam, gamma):
    K = C + R
    pay_unsplit = S - C - float(mech.F_scalar(S, K, lam, gamma, 0.0))
    r = mech.watermark_charges(np.cumsum(np.full(n, S / n)), K, lam, gamma, 0.0).sum()
    assert S - (C + extra) - r <= pay_unsplit + 1e-9 * max(1, S)


@given(S=st.floats(0, 1e6), K=st.floats(0, 1e6), lam=unit, gamma=st.floats(0, 0.999), delta=st.floats(0, 1e5))
def test_fixedpoint_agrees_with_float(S, K, lam, gamma, delta):
    r_int, _ = fp.transfer_wad(fp.to_wad(S), fp.to_wad(K), fp.to_wad(lam), fp.to_wad(gamma), fp.to_wad(delta))
    r_flt = float(mech.transfer_est(S, K, lam, gamma, delta))
    assert abs(r_int / fp.WAD - r_flt) <= 1e-9 * max(1.0, S)


def _hook_replay():
    p = REPO / "contracts" / "python" / "hook_replay.py"
    spec = importlib.util.spec_from_file_location("hook_replay_ref", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@given(S=st.integers(0, 10**25), K=st.integers(0, 10**24), lam=st.integers(0, fp.WAD), gamma=st.integers(0, fp.WAD - 1),
       delta=st.integers(0, 10**23))
def test_fixedpoint_matches_existing_solidity_reference(S, K, lam, gamma, delta):
    ref = _hook_replay()
    assert fp.transfer_wad(S, K, lam, gamma, delta)[0] == ref.transfer_wad(S, K, lam, gamma, delta)[0]


def test_conservative_tie_break_smallest():
    assert mech.argmax_smallest(np.array([0.0, 5.0, 5.0, 5.0, 3.0])) == 1
