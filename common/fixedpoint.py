"""Integer (WAD, round-down) reference of the mechanism, mirroring the Solidity accounting.

Mirrors contracts/src/SurplusSharingAccounting.sol (`computeTransfer`) and the
transaction-scoped watermark of CumulativeSurplusAccounting(.Lib). Rounding is applied to
the cumulative target, never to the marginal charge, so fragments cannot accumulate
rounding discounts.
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
