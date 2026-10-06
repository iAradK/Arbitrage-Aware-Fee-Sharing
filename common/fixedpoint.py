"""Integer (WAD, round-down) reference of the mechanism, mirroring the Solidity accounting.

Mirrors contracts/src/SurplusSharingAccounting.sol (`computeTransfer`), the watermark of
CumulativeSurplusAccounting(.Lib) (`cumulative_charges`), and the hook's scope
(`ScopedHookReference`: block-scoped ParticipationAwareHook, or the transaction-scoped
baseline). Rounding is applied to the cumulative target, never to the marginal charge, so
fragments cannot accumulate rounding discounts.
"""
from __future__ import annotations

from typing import Iterable, Sequence

WAD = 10**18


def to_wad(x: float) -> int:
    """Nonnegative float -> WAD, rounded down."""
    if x < 0:
        raise ValueError("negative WAD")
    return int(x * WAD)


def mul_wad_down(value: int, fraction_wad: int) -> int:
    if value < 0 or not 0 <= fraction_wad <= WAD:
        raise ValueError("bad mul_wad_down input")
    return (value // WAD) * fraction_wad + ((value % WAD) * fraction_wad) // WAD


def transfer_wad(s_hat: int, k_hat: int, lam: int, gamma: int, delta: int) -> tuple[int, str]:
    """F(a) = min(lam*a, (1-gamma)*max(a-K-delta,0)). Returns (transfer, class in {prop, cap, zero})."""
    if min(s_hat, k_hat, delta) < 0 or not 0 <= lam <= WAD or not 0 <= gamma < WAD:
        raise ValueError("bad transfer_wad input")
    target = mul_wad_down(s_hat, lam)
    margin = max(0, s_hat - k_hat - delta)
    cap = mul_wad_down(margin, WAD - gamma)
    r = min(target, cap)
    if margin == 0 or r == 0:
        return r, "zero"
    return r, ("cap" if cap <= target else "prop")


def cumulative_charges(prefix_surplus: Iterable[int], k_hat: int, lam: int, gamma: int, delta: int) -> list[dict]:
    """Watermark accounting on cumulative surplus prefixes A_1..A_n."""
    wm = 0
    rows = []
    for j, a in enumerate(prefix_surplus, start=1):
        t, cls = transfer_wad(max(a, 0), k_hat, lam, gamma, delta)
        new = max(wm, t)
        rows.append({"j": j, "A": a, "target": t, "watermark": new, "charge": new - wm, "cls": cls})
        wm = new
    return rows


U128_MAX = 2**128 - 1
I128_MIN, I128_MAX = -(2**127), 2**127 - 1
BPS_TO_WAD = 10**14


def _div_trunc(a: int, b: int) -> int:
    """Solidity signed division (rounds toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def hook_surplus(cum0: int, cum1: int, ref_wad: int) -> int:
    """[cum0 + cum1 * ref / WAD]^+ with token0 as the numeraire, product rounded toward zero (as in the hook)."""
    return max(cum0 + _div_trunc(cum1 * ref_wad, WAD), 0)


class ScopedHookReference:
    """Integer mirror of contracts/src/hooks/ParticipationAwareHook.sol (scope="block") and of the
    transaction-scoped baseline TxScopedParticipationAwareHook.sol (scope="tx").

    One scope per pool. A swap opens a new scope when its key differs from the pool's open scope: the block
    (block-scoped) or the (block, transaction) pair (transaction-scoped). Opening requires a valid oracle price
    (0 < price <= 2^128 - 1, else the swap is charged 0, not accumulated, and the next swap retries) and locks the
    reference, kappa = min(K + delta + g * (basefee + tau) * p / WAD, 2^128 - 1) with p = gas_price_token0_wad or
    the reference when that is 0, and lambda, gamma in basis points. Cumulative deltas are int128 and W, kappa
    uint128; leaving those ranges, or a target above 2^128 - 1, saturates W and fails open for the rest of the
    scope. Charges are numeraire (token0) values before conversion into the settlement token, which equals the
    hook exactly when the swap settles in token0.

    The transaction-scoped contract treats an invalid oracle at the first swap as disabling the pool for the rest
    of the transaction, and it has no uint128/int128 limits; neither difference matters for the checks here.
    """

    def __init__(self, k_const: int, delta: int, lam_bps: int, gamma_bps: int, gas_units: int = 0,
                 tau_wei: int = 0, gas_price_token0_wad: int = 0, scope: str = "block"):
        if not 0 <= lam_bps <= 10_000 or not 0 <= gamma_bps < 10_000 or scope not in ("block", "tx"):
            raise ValueError("bad ScopedHookReference parameters")
        self.kappa_const = min(k_const + delta, U128_MAX)
        self.gas_units, self.tau_wei, self.gas_price = gas_units, tau_wei, gas_price_token0_wad
        self.lam_bps, self.gamma_bps, self.scope = lam_bps, gamma_bps, scope
        self.pools: dict = {}

    def kappa(self, basefee: int, ref_wad: int) -> int:
        k = self.kappa_const
        if self.gas_units:
            if basefee > 2**64 - 1:
                return U128_MAX
            p = self.gas_price or ref_wad
            k += self.gas_units * (basefee + self.tau_wei) * p // WAD
        return min(k, U128_MAX)

    def swap(self, pool, block: int, tx: int, d0: int, d1: int, oracle_price_wad: int | None,
             basefee: int = 0) -> int:
        """Process one swap's core delta (positive = received by the swapper). Returns the marginal charge r_j."""
        key = block if self.scope == "block" else (block, tx)
        s = self.pools.get(pool)
        if s is None or s["key"] != key:
            if not oracle_price_wad or oracle_price_wad > U128_MAX:
                return 0
            s = {"key": key, "W": 0, "lam": self.lam_bps, "gam": self.gamma_bps, "cum0": d0, "cum1": d1,
                 "ref": oracle_price_wad, "kappa": self.kappa(basefee, oracle_price_wad), "saturated": False}
            self.pools[pool] = s
            if not (I128_MIN <= d0 <= I128_MAX and I128_MIN <= d1 <= I128_MAX):
                s["saturated"] = True
                return 0
        else:
            if s["saturated"]:
                return 0
            c0, c1 = s["cum0"] + d0, s["cum1"] + d1
            if not (I128_MIN <= c0 <= I128_MAX and I128_MIN <= c1 <= I128_MAX):
                s["saturated"] = True
                return 0
            s["cum0"], s["cum1"] = c0, c1
        a = hook_surplus(s["cum0"], s["cum1"], s["ref"])
        target, _ = transfer_wad(a, s["kappa"], s["lam"] * BPS_TO_WAD, s["gam"] * BPS_TO_WAD, 0)
        if target <= s["W"]:
            return 0
        if target > U128_MAX:
            s["saturated"] = True
            return 0
        r, s["W"] = target - s["W"], target
        return r


def prefix_surplus_wad(deltas0: Sequence[int], deltas1: Sequence[int], pi0_wad: int, pi1_wad: int) -> list[int]:
    """A_j = max(pi . D_{1:j}, 0) with signed WAD balance deltas; product rounded toward -inf then clamped."""
    d0 = d1 = 0
    out = []
    for a, b in zip(deltas0, deltas1):
        d0 += a
        d1 += b
        v = (d0 * pi0_wad) // WAD + (d1 * pi1_wad) // WAD
        out.append(max(v, 0))
    return out
