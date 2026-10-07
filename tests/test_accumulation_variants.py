"""Accumulation variants of the block-scoped integer reference: V1 per-transaction clipping ("tx_clip") and V2
per-swap clipping ("swap_clip"), against the deployed net accumulation ("net")."""
import pytest
from hypothesis import given, settings, strategies as st

from common import fixedpoint as fp

WAD = fp.WAD
kappa = st.integers(0, 10**22)
lam_bps = st.integers(0, 10_000)
gam_bps = st.integers(0, 9_999)
# (numeraire delta, starts a new transaction)
legs = st.lists(st.tuples(st.integers(-(10**21), 10**22), st.booleans()), min_size=1, max_size=12)


def F(a, k, lb, gb):
    return fp.transfer_wad(max(a, 0), k, lb * fp.BPS_TO_WAD, gb * fp.BPS_TO_WAD, 0)[0]


def run(acc, swaps, k, lb, gb, scope="block", ref=WAD):
    """swaps: (tx, d0, d1) in one block; returns the charges."""
    h = fp.ScopedHookReference(k, 0, lb, gb, scope=scope, accumulation=acc)
    return [h.swap("p", 7, tx, d0, d1, ref) for tx, d0, d1 in swaps]


def with_tx(ls):
    out, tx = [], 0
    for j, (d, new) in enumerate(ls):
        tx += j > 0 and new
        out.append((tx, d, 0))
    return out


def test_bad_accumulation_rejected():
    with pytest.raises(ValueError):
        fp.ScopedHookReference(0, 0, 7500, 200, accumulation="other")
    with pytest.raises(ValueError):
        fp.ScopedHookReference(0, 0, 7500, 200, scope="tx", accumulation="tx_clip")


@given(ls=st.lists(st.tuples(st.integers(-(10**21), 10**22), st.integers(-(10**21), 10**21), st.booleans()),
                   min_size=1, max_size=10), k=kappa, lb=lam_bps, gb=gam_bps,
       ref=st.integers(1, 10**24), settle0=st.booleans())
@settings(max_examples=200)
def test_net_is_the_default(ls, k, lb, gb, ref, settle0):
    a = fp.ScopedHookReference(k, 0, lb, gb)
    b = fp.ScopedHookReference(k, 0, lb, gb, accumulation="net")
    for j, (d0, d1, _) in enumerate(ls):
        assert a.swap("p", 7, j, d0, d1, ref, settle_token0=settle0) == b.swap("p", 7, j, d0, d1, ref, settle_token0=settle0)
        assert a.pools["p"] == b.pools["p"]


@given(inc=st.lists(st.integers(0, 10**22), min_size=1, max_size=12), k=kappa, lb=lam_bps, gb=gam_bps,
       same_tx=st.booleans())
@settings(max_examples=300)
def test_same_direction_split_pays_the_unsplit_charge(inc, k, lb, gb, same_tx):
    """Pieces with nonnegative surplus, as separate transactions of one block or as one transaction: every variant
    collects exactly F(sum)."""
    swaps = [(0 if same_tx else j, d, 0) for j, d in enumerate(inc)]
    for acc in ("net", "tx_clip", "swap_clip"):
        assert sum(run(acc, swaps, k, lb, gb)) == F(sum(inc), k, lb, gb), acc


@given(x=st.integers(1, 10**22), y=st.integers(0, 10**22), k=kappa, lb=lam_bps, gb=gam_bps)
@settings(max_examples=300)
def test_counter_trade_of_another_transaction_is_not_netted(x, y, k, lb, gb):
    """A negative-surplus transaction followed by a back-run: net charges F([y - x]^+), V1 and V2 charge F(y)."""
    swaps = [(0, -x, 0), (1, y, 0)]
    assert sum(run("net", swaps, k, lb, gb)) == F(y - x, k, lb, gb)
    assert run("tx_clip", swaps, k, lb, gb) == [0, F(y, k, lb, gb)]
    assert run("swap_clip", swaps, k, lb, gb) == [0, F(y, k, lb, gb)]


@given(inc=st.lists(st.integers(-(10**21), 10**22), min_size=1, max_size=12), k=kappa, lb=lam_bps, gb=gam_bps)
@settings(max_examples=300)
def test_v1_inside_one_transaction_is_the_watermark(inc, k, lb, gb):
    """Within one transaction V1 is the transaction-scoped watermark rule, swap by swap."""
    swaps = [(0, d, 0) for d in inc]
    assert run("tx_clip", swaps, k, lb, gb) == run("net", swaps, k, lb, gb, scope="tx")


@given(ls=st.lists(st.integers(-(10**21), 10**22), min_size=1, max_size=12), k=kappa, lb=lam_bps, gb=gam_bps)
@settings(max_examples=300)
def test_v1_equals_v2_with_one_swap_per_transaction(ls, k, lb, gb):
    swaps = [(j, d, 0) for j, d in enumerate(ls)]
    assert run("tx_clip", swaps, k, lb, gb) == run("swap_clip", swaps, k, lb, gb)


@given(ls=legs, k=kappa, lb=lam_bps, gb=gam_bps)
@settings(max_examples=500)
def test_net_le_v1_le_v2(ls, k, lb, gb):
    """[sum]^+ <= sum of per-transaction brackets <= sum of per-swap brackets at every swap, so the watermarks and
    the totals are ordered (token0 settlement, where W equals the target)."""
    swaps = with_tx(ls)
    hs = {acc: fp.ScopedHookReference(k, 0, lb, gb, accumulation=acc) for acc in ("net", "tx_clip", "swap_clip")}
    for tx, d0, d1 in swaps:
        for h in hs.values():
            h.swap("p", 7, tx, d0, d1, WAD)
        a = [hs[acc].surplus("p") for acc in ("net", "tx_clip", "swap_clip")]
        w = [hs[acc].pools["p"]["W"] for acc in ("net", "tx_clip", "swap_clip")]
        assert a[0] <= a[1] <= a[2] and w[0] <= w[1] <= w[2]


def test_v2_overcharges_a_round_trip_inside_one_transaction():
    """+a, -a, +a in one transaction: net and V1 charge F(a); V2 adds the brackets of both positive legs."""
    k, lb, gb, a = 10**18, 7500, 200, 5 * WAD
    swaps = [(0, a, 0), (0, -a, 0), (0, a, 0)]
    assert sum(run("net", swaps, k, lb, gb)) == F(a, k, lb, gb)
    assert sum(run("tx_clip", swaps, k, lb, gb)) == F(a, k, lb, gb)
    assert sum(run("swap_clip", swaps, k, lb, gb)) == F(2 * a, k, lb, gb)


def test_surplus_accessor_and_token1_rounding():
    """Brackets use hook_surplus on the locked reference: V2 truncates each swap's product, V1 each transaction's."""
    ref = 2 * 10**9 + 1                                   # USDC units per wei, WAD (not a multiple of WAD)
    swaps = [(0, -3 * 10**6, 1500 * 10**9 + 7), (0, 10, 333), (1, -10**6, 5 * 10**8 + 3)]
    hs = {acc: fp.ScopedHookReference(0, 0, 7500, 200, accumulation=acc) for acc in ("net", "tx_clip", "swap_clip")}
    for tx, d0, d1 in swaps:
        for h in hs.values():
            h.swap("p", 7, tx, d0, d1, ref)
    hsur = fp.hook_surplus
    tx0 = (swaps[0][1] + swaps[1][1], swaps[0][2] + swaps[1][2])
    assert hs["net"].surplus("p") == hsur(sum(s[1] for s in swaps), sum(s[2] for s in swaps), ref)
    assert hs["tx_clip"].surplus("p") == hsur(*tx0, ref) + hsur(swaps[2][1], swaps[2][2], ref)
    assert hs["swap_clip"].surplus("p") == sum(hsur(d0, d1, ref) for _, d0, d1 in swaps)


def test_new_block_resets_the_clipped_sum():
    h = fp.ScopedHookReference(0, 0, 10_000, 0, accumulation="tx_clip")
    h.swap("p", 1, 0, 4 * WAD, 0, WAD)
    h.swap("p", 1, 1, 3 * WAD, 0, WAD)
    assert h.surplus("p") == 7 * WAD
    h.swap("p", 2, 0, WAD, 0, WAD)
    assert h.pools["p"]["C"] == 0 and h.surplus("p") == WAD


def test_clipped_sum_overflow_saturates():
    """Alternating legs keep the cumulative delta inside int128 while the clipped sum C passes 2^128 - 1."""
    h = fp.ScopedHookReference(0, 0, 10_000, 0, accumulation="swap_clip")
    for j, d in enumerate([fp.I128_MAX, -fp.I128_MAX, fp.I128_MAX, -fp.I128_MAX]):
        h.swap("p", 1, j, d, 0, WAD)
    assert not h.pools["p"]["saturated"] and h.pools["p"]["C"] == 2 * fp.I128_MAX
    h.swap("p", 1, 4, fp.I128_MAX, 0, WAD)
    assert h.pools["p"]["saturated"] and h.pools["p"]["cum0"] == fp.I128_MAX
    assert h.swap("p", 1, 9, WAD, 0, WAD) == 0
