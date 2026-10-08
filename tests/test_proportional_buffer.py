"""Proportional buffer of the integer reference (ScopedHookReference buffer="abs" / "rel"): each bracket deducts
eps x its absolute net token1 change (volume="net", the contract; "gross" = the S1 hook's gross token1 volume) instead
of a fixed delta inside kappa."""
import pytest
from hypothesis import given, settings, strategies as st

from common import fixedpoint as fp

WAD = fp.WAD
REF = 2000 * WAD                      # token0 per token1 unit, WAD
kappa = st.integers(0, 10**22)
lam_bps = st.integers(0, 10_000)
gam_bps = st.integers(0, 9_999)


def F(a, k, lb, gb):
    return fp.transfer_wad(max(a, 0), k, lb * fp.BPS_TO_WAD, gb * fp.BPS_TO_WAD, 0)[0]


def h_(acc="tx_clip", buffer="abs", eps=0, k=10**18, lb=7500, gb=200, scope="block"):
    return fp.ScopedHookReference(k, 0, lb, gb, scope=scope, accumulation=acc, buffer=buffer, eps_wad=eps)


def test_bad_buffer_rejected():
    with pytest.raises(ValueError):
        fp.ScopedHookReference(0, 0, 7500, 200, buffer="fixed")
    with pytest.raises(ValueError):
        fp.ScopedHookReference(0, 0, 7500, 200, buffer="abs", eps_wad=-1)


@given(legs=st.lists(st.tuples(st.integers(-(10**22), 10**22), st.integers(-(10**19), 10**19), st.integers(0, 3)),
                     min_size=1, max_size=8), k=kappa, lb=lam_bps, gb=gam_bps,
       acc=st.sampled_from(["net", "tx_clip", "swap_clip"]), buffer=st.sampled_from(["abs", "rel"]))
@settings(max_examples=300)
def test_zero_eps_and_none_are_the_unbuffered_rule(legs, k, lb, gb, acc, buffer):
    a = fp.ScopedHookReference(k, 0, lb, gb, accumulation=acc)
    b = fp.ScopedHookReference(k, 0, lb, gb, accumulation=acc, buffer=buffer, eps_wad=0)
    for tx, (d0, d1, _) in enumerate(legs):
        assert a.swap("p", 7, tx // 2, d0, d1, REF) == b.swap("p", 7, tx // 2, d0, d1, REF)
        assert a.surplus("p") == b.surplus("p")


@given(d0=st.integers(-(10**22), 10**23), d1=st.integers(-(10**19), 10**19), eps=st.integers(0, 10**21),
       k=kappa, lb=lam_bps, gb=gam_bps)
@settings(max_examples=300)
def test_single_swap_formula(d0, d1, eps, k, lb, gb):
    """One swap pays F([d0 + d1 ref / WAD - ceil(eps |d1|)]^+; kappa) for abs, and with eps * ref for rel."""
    s_hat = d0 + fp._div_trunc(d1 * REF, WAD)
    ha = h_("net", "abs", eps, k, lb, gb)
    assert ha.swap("p", 1, 0, d0, d1, REF) == F(s_hat - (-(-eps * abs(d1) // WAD)), k, lb, gb)
    hr = h_("net", "rel", eps, k, lb, gb)
    assert hr.swap("p", 1, 0, d0, d1, REF) == F(s_hat - (-(-eps * REF * abs(d1) // WAD**2)), k, lb, gb)
    assert ha.pools["p"]["kappa"] == hr.pools["p"]["kappa"] == min(k, fp.U128_MAX)     # kappa holds no buffer


def test_eps_is_locked_at_scope_open():
    h = h_("tx_clip", "abs", eps=10 * WAD, k=0, lb=10_000, gb=0)
    h.swap("p", 1, 0, 100 * WAD, -2, REF)
    h.eps_wad = 0
    h.swap("p", 1, 1, 50 * WAD, 0, REF)
    assert h.pools["p"]["eps"] == 10 * WAD
    h.swap("p", 2, 0, 50 * WAD, -1, REF)          # new block: the new eps is locked
    assert h.pools["p"]["eps"] == 0


def test_gross_volume_counts_both_directions():
    """volume="gross" (the S1 hook): a round trip inside one transaction nets d1 to 0 but deducts eps on the gross
    volume of both legs."""
    eps = 5 * WAD                                 # 5 token0 units per token1 unit
    h = fp.ScopedHookReference(0, 0, 10_000, 0, accumulation="tx_clip", buffer="abs", eps_wad=eps, volume="gross")
    h.swap("p", 1, 0, 3000 * WAD, 10, REF)        # receives 10 token1 base units and 3000 WAD token0
    h.swap("p", 1, 0, -2000 * WAD, -10, REF)      # gives them back: net d1 = 0
    s = h.pools["p"]
    assert s["g"] == 20 and h.surplus("p") == 1000 * WAD - 5 * 20     # eps x gross volume, in token0 base units


def test_net_volume_ignores_a_round_trip():
    """volume="net" (the contract, the default): the same round trip deducts eps on the net change |d1| = 0."""
    eps = 5 * WAD
    h = h_("tx_clip", "abs", eps, k=0, lb=10_000, gb=0)
    assert h.volume == "net"
    h.swap("p", 1, 0, 3000 * WAD, 10, REF)
    assert h.surplus("p") == 3000 * WAD + 10 * REF // WAD - 5 * 10
    h.swap("p", 1, 0, -2000 * WAD, -10, REF)
    assert h.surplus("p") == 1000 * WAD


def test_bad_volume_rejected():
    with pytest.raises(ValueError):
        fp.ScopedHookReference(0, 0, 7500, 200, buffer="rel", volume="both")


@given(x1=st.integers(1, 10**19), margin=st.integers(1, 2000), legs=st.lists(st.integers(-(10**20), 10**20), min_size=1,
       max_size=4), at=st.integers(0, 8), eps=st.integers(0, 10**16), k=kappa, lb=lam_bps, gb=gam_bps,
       buffer=st.sampled_from(["abs", "rel"]))
@settings(max_examples=400)
def test_wash_round_trip_inside_a_transaction_never_lowers_the_charge(x1, margin, legs, at, eps, k, lb, gb, buffer):
    """volume="net": a correcting swap X plus round-trip legs (r, -r) inside the same transaction, in any order, pays
    at least the charge of X alone (its bracket is X's, and the watermark keeps the peak of the intermediate ones)."""
    x0 = -(x1 * REF // WAD) * (10_000 - margin) // 10_000            # receives x1 token1, pays less than its value
    alone = h_("tx_clip", buffer, eps, k, lb, gb).swap("p", 1, 0, x0, x1, REF)
    seq = []
    for r1 in legs:
        r0 = -(r1 * REF // WAD)                                      # a leg at the reference, undone exactly by -r
        seq += [(r0, r1), (-r0, -r1)]
    seq.insert(min(at, len(seq)), (x0, x1))
    w = h_("tx_clip", buffer, eps, k, lb, gb)
    washed = sum(w.swap("p", 1, 0, d0, d1, REF) for d0, d1 in seq)
    assert washed >= alone


@given(pieces=st.lists(st.tuples(st.integers(0, 10**6), st.integers(0, 10**6)), min_size=2, max_size=10),
       k=kappa, lb=lam_bps, gb=gam_bps, acc=st.sampled_from(["tx_clip", "swap_clip"]), e=st.integers(0, 50))
@settings(max_examples=400)
def test_split_pays_at_least_the_unsplit_charge(pieces, k, lb, gb, acc, e):
    """Pieces as separate transactions of one block: the clipped sum is >= the bracket of the whole, so a split never
    pays less than the unsplit swap (eps a multiple of WAD per unit: no rounding). Equal when no piece is clipped."""
    eps = e * WAD
    legs = [(s * WAD + eps * v // WAD, -v) for s, v in pieces]       # piece: buffered surplus s * WAD, volume v
    # d1 = -v (the swapper pays token1), d0 = surplus + v * ref + eps * v: S_hat = d0 - v * ref / WAD
    legs = [(d0 + v * REF // WAD, d1) for (d0, d1), (s, v) in zip(legs, pieces)]
    h = h_(acc, "abs", eps, k, lb, gb)
    paid = sum(h.swap("p", 7, j, d0, d1, REF) for j, (d0, d1) in enumerate(legs))
    whole = h_("net", "abs", eps, k, lb, gb).swap("p", 7, 0, sum(d for d, _ in legs), sum(d for _, d in legs), REF)
    assert paid == whole == F(sum(s for s, _ in pieces) * WAD, k, lb, gb)


@given(b=st.lists(st.integers(-(10**22), 10**22), min_size=2, max_size=10), k=kappa, lb=lam_bps, gb=gam_bps)
@settings(max_examples=400)
def test_negative_pieces_are_clipped_not_netted(b, k, lb, gb):
    """Pieces whose buffered surplus is negative (the buffer exceeds their surplus) do not reduce the others' sum."""
    eps, v = 7 * WAD, 3
    legs = [(x + eps * v // WAD + v * REF // WAD, -v) for x in b]
    h = h_("tx_clip", "abs", eps, k, lb, gb)
    paid = sum(h.swap("p", 7, j, d0, d1, REF) for j, (d0, d1) in enumerate(legs))
    assert paid == F(sum(max(x, 0) for x in b), k, lb, gb)
    whole = h_("net", "abs", eps, k, lb, gb).swap("p", 7, 0, sum(d for d, _ in legs), sum(d for _, d in legs), REF)
    assert paid >= whole


def test_rounding_favours_the_hook_side_of_the_swapper():
    """The deduction rounds up: a split into l transactions can deduct at most l - 1 units more than the whole."""
    eps = WAD // 3                                # 1/3 token0 unit per token1 unit: ceil rounds every piece
    legs = [(10**9, -1)] * 6
    h = h_("tx_clip", "abs", eps, k=0, lb=10_000, gb=0)
    for j, (d0, d1) in enumerate(legs):
        h.swap("p", 7, j, d0 + REF // WAD, d1, REF)
    split = h.surplus("p")
    whole = h_("net", "abs", eps, k=0, lb=10_000, gb=0)
    whole.swap("p", 7, 0, 6 * (10**9 + REF // WAD), -6, REF)
    assert 0 <= whole.surplus("p") - split <= 5
