"""Block-scoped integer reference of the hook (common.fixedpoint.ScopedHookReference)."""
from hypothesis import given, settings, strategies as st

from common import fixedpoint as fp

WAD = fp.WAD
incs = st.lists(st.integers(-(10**21), 10**22), min_size=1, max_size=12)
kappa = st.integers(0, 10**22)
lam_bps = st.integers(0, 10_000)
gam_bps = st.integers(0, 9_999)


def _prefix(inc):
    out, acc = [], 0
    for d in inc:
        acc += d
        out.append(max(acc, 0))
    return out


@given(inc=incs, k=kappa, lb=lam_bps, gb=gam_bps)
@settings(max_examples=300)
def test_separate_transactions_in_one_block_equal_one_transaction(inc, k, lb, gb):
    """Fragments as separate transactions of one block pay exactly what the in-transaction watermark charges."""
    h = fp.ScopedHookReference(k, 0, lb, gb)
    charges = [h.swap("p", block=7, tx=j, d0=d, d1=0, oracle_price_wad=WAD) for j, d in enumerate(inc)]
    rows = fp.cumulative_charges(_prefix(inc), k, lb * fp.BPS_TO_WAD, gb * fp.BPS_TO_WAD, 0)
    assert charges == [r["charge"] for r in rows]
    assert sum(charges) == max(r["target"] for r in rows)
    assert all(c >= 0 for c in charges)


@given(inc=st.lists(st.integers(0, 10**22), min_size=1, max_size=12), k=kappa, lb=lam_bps, gb=gam_bps)
@settings(max_examples=300)
def test_tx_scope_across_transactions_is_the_independent_rule(inc, k, lb, gb):
    """The transaction-scoped baseline charges each transaction F(a_i); the block scope charges F(sum a_i) >= that."""
    tx = fp.ScopedHookReference(k, 0, lb, gb, scope="tx")
    bl = fp.ScopedHookReference(k, 0, lb, gb)
    paid_tx = sum(tx.swap("p", 7, j, d, 0, WAD) for j, d in enumerate(inc))
    paid_bl = sum(bl.swap("p", 7, j, d, 0, WAD) for j, d in enumerate(inc))
    F = lambda a: fp.transfer_wad(a, k, lb * fp.BPS_TO_WAD, gb * fp.BPS_TO_WAD, 0)[0]
    assert paid_tx == sum(F(d) for d in inc)
    assert paid_bl == F(sum(inc))
    assert paid_tx <= paid_bl


def test_invalid_oracle_opens_no_scope_and_retries():
    h = fp.ScopedHookReference(10**18, 0, 7500, 500)
    assert h.swap("p", 1, 0, 20 * WAD, 0, oracle_price_wad=None) == 0
    assert "p" not in h.pools
    r = h.swap("p", 1, 1, 20 * WAD, 0, oracle_price_wad=WAD)
    assert r == fp.transfer_wad(20 * WAD, 10**18, 7500 * fp.BPS_TO_WAD, 500 * fp.BPS_TO_WAD, 0)[0]
    assert h.pools["p"]["cum0"] == 20 * WAD


def test_new_block_resets_scope_and_relocks_kappa_and_reference():
    h = fp.ScopedHookReference(10**18, 0, 7500, 500, gas_units=200_000, tau_wei=5 * 10**7, gas_price_token0_wad=2000 * WAD)
    h.swap("p", 100, 0, 30 * WAD, 0, oracle_price_wad=WAD, basefee=10 * 10**9)
    k1 = h.pools["p"]["kappa"]
    assert k1 == 10**18 + 200_000 * (10 * 10**9 + 5 * 10**7) * 2000
    h.swap("p", 100, 1, 1 * WAD, 0, oracle_price_wad=2 * WAD, basefee=50 * 10**9)   # same block: locked
    assert h.pools["p"]["kappa"] == k1 and h.pools["p"]["ref"] == WAD and h.pools["p"]["cum0"] == 31 * WAD
    h.swap("p", 101, 0, 5 * WAD, 0, oracle_price_wad=2 * WAD, basefee=50 * 10**9)    # next block: reset
    s = h.pools["p"]
    assert s["kappa"] == 10**18 + 200_000 * (50 * 10**9 + 5 * 10**7) * 2000
    assert s["ref"] == 2 * WAD and s["cum0"] == 5 * WAD and s["key"] == 101


def test_out_of_range_saturates_and_fails_open():
    h = fp.ScopedHookReference(0, 0, 10_000, 0)
    assert h.swap("p", 1, 0, fp.I128_MAX, 0, WAD) > 0
    assert h.swap("p", 1, 1, 1, 0, WAD) == 0          # cum0 leaves int128: saturate
    assert h.pools["p"]["saturated"]
    assert h.swap("p", 1, 2, -5, 0, WAD) == 0
    assert h.swap("p", 2, 0, 10, 0, WAD) == 10        # next block reopens


def test_surplus_rounds_toward_zero_like_solidity():
    assert fp.hook_surplus(0, -3, WAD // 2) == 0
    assert fp.hook_surplus(2, -3, WAD // 2) == 1          # -1.5 truncates to -1
    assert fp.hook_surplus(0, 3, WAD // 2) == 1


def test_token1_settlement_rounds_down_and_keeps_the_remainder_owed():
    """Settling in token1 at reference 3 pays floor(r / 3) token1 units; W advances by that value rounded up, so
    the remainder of r is charged by the next swap of the scope (as the hook's _toSettlementAmount)."""
    ref = 3 * WAD
    h = fp.ScopedHookReference(0, 0, 10_000, 0)
    r1 = h.swap("p", 1, 0, 10, 0, oracle_price_wad=ref, settle_token0=False)   # target 10
    assert (r1, h.last_token_amount, h.pools["p"]["W"]) == (9, 3, 9)
    r2 = h.swap("p", 1, 1, 0, 0, oracle_price_wad=ref, settle_token0=True)     # same surplus: the remainder
    assert (r2, h.last_token_amount, h.pools["p"]["W"]) == (1, 1, 10)


def test_settlement_amount_outside_int128_saturates():
    h = fp.ScopedHookReference(0, 0, 10_000, 0)
    assert h.swap("p", 1, 0, 0, fp.I128_MAX, oracle_price_wad=2 * WAD, settle_token0=True) == 0   # target 2^128 - 2 > int128
    assert h.pools["p"]["saturated"]
    assert h.swap("p", 2, 0, 5, 0, oracle_price_wad=WAD) == 5
