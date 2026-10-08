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
    scope. Settlement follows the hook: a swap that settles in token0 pays the marginal charge r = target - W and
    W = target; one that settles in token1 pays floor(r * WAD / reference) token1 base units, and W advances by
    that amount valued back at the reference, rounded up (<= r, the remainder stays owed). A charge that cannot be
    converted (r > (2^256 - 1) / WAD or more than int128 token units) saturates W. `swap` returns the value W
    advanced by (the marginal charge in token0 units), and `last_token_amount` holds what the settlement token
    received.

    The transaction-scoped contract treats an invalid oracle at the first swap as disabling the pool for the rest
    of the transaction, and it has no uint128/int128 limits; neither difference matters for the checks here.

    The final hook (ParticipationAwareHook.sol) is scope="block", accumulation="tx_clip", buffer="rel",
    volume="net", with eps_wad = its locked eps_rel in parts per billion x 1e9 and tau_wei = 3 gwei; the S1 hook
    (final-integration, 6a4541b) was the same with volume="gross"; the earlier block-scoped hook was
    accumulation="net", buffer="none".

    `accumulation` selects how the scope's surplus A is accumulated (the watermark and settlement are unchanged):
      - "net" (the earlier block-scoped hook): A = [pi . (sum of all deltas in the scope)]^+;
      - "tx_clip" (V1, block scope only; the final hook): A = sum over the completed transactions of the block of
        [pi . (deltas of that transaction)]^+, plus [pi . (running deltas of the current transaction)]^+. A swap
        whose transaction differs from the previous swap's folds the previous transaction into the sum;
      - "swap_clip" (V2): A = sum over the scope's swaps of [pi . (deltas of that swap)]^+.
    Each bracket is `hook_surplus` (product rounded toward zero, then clipped). The sum C of folded brackets is
    uint128; leaving that range saturates W like the other limits.

    `buffer` selects a proportional buffer deducted inside each bracket (kappa then holds no delta):
      - "none" (the earlier block-scoped hook): nothing;
      - "abs": ceil(eps * g / WAD), eps = `eps_wad`, a price bound in token0 per token1 base unit, WAD-scaled like
        the reference;
      - "rel": ceil(eps * ref * g / WAD^2), eps = `eps_wad` a relative price bound (WAD = 100%).
    `volume` selects g, over the bracket's swaps (the scope for "net", the transaction for "tx_clip", the swap for
    "swap_clip"):
      - "net" (the contract): g = |sum of d1|, the bracket's absolute net token1 change, so volume that nets out
        inside the bracket (a round trip) does not enlarge the deduction;
      - "gross" (the S1 hook): g = sum of |d1|; g is uint128 (saturates beyond).
    With token0 as the numeraire, S_hat - S = (P_hat - P) * d1 exactly, so the deduction on the net change bounds the
    bracket's surplus error when |P_hat - P| <= eps. `eps_wad` is read when a scope opens and locked with kappa; it
    can be changed between swaps like `kappa_const`.
    """

    ACCUMULATIONS = ("net", "tx_clip", "swap_clip")
    BUFFERS = ("none", "abs", "rel")
    VOLUMES = ("net", "gross")

    def __init__(self, k_const: int, delta: int, lam_bps: int, gamma_bps: int, gas_units: int = 0,
                 tau_wei: int = 0, gas_price_token0_wad: int = 0, scope: str = "block", accumulation: str = "net",
                 buffer: str = "none", eps_wad: int = 0, volume: str = "net"):
        if not 0 <= lam_bps <= 10_000 or not 0 <= gamma_bps < 10_000 or scope not in ("block", "tx"):
            raise ValueError("bad ScopedHookReference parameters")
        if accumulation not in self.ACCUMULATIONS or (accumulation == "tx_clip" and scope != "block"):
            raise ValueError("bad ScopedHookReference accumulation")
        self.accumulation = accumulation
        if buffer not in self.BUFFERS or eps_wad < 0 or volume not in self.VOLUMES:
            raise ValueError("bad ScopedHookReference buffer")
        self.buffer, self.eps_wad, self.volume = buffer, eps_wad, volume
        self.kappa_const = min(k_const + delta, U128_MAX)
        self.gas_units, self.tau_wei, self.gas_price = gas_units, tau_wei, gas_price_token0_wad
        self.lam_bps, self.gamma_bps, self.scope = lam_bps, gamma_bps, scope
        self.pools: dict = {}
        self.last_token_amount = 0

    def kappa(self, basefee: int, ref_wad: int) -> int:
        k = self.kappa_const
        if self.gas_units:
            if basefee > 2**64 - 1:
                return U128_MAX
            p = self.gas_price or ref_wad
            k += self.gas_units * (basefee + self.tau_wei) * p // WAD
        return min(k, U128_MAX)

    def swap(self, pool, block: int, tx: int, d0: int, d1: int, oracle_price_wad: int | None,
             basefee: int = 0, settle_token0: bool = True) -> int:
        """Process one swap's core delta (positive = received by the swapper). Returns the marginal charge r_j:
        the amount W advanced by, in token0 units. `settle_token0` = the swap's unspecified currency is token0."""
        self.last_token_amount = 0
        key = block if self.scope == "block" else (block, tx)
        s = self.pools.get(pool)
        if s is None or s["key"] != key:
            if not oracle_price_wad or oracle_price_wad > U128_MAX:
                return 0
            s = {"key": key, "W": 0, "lam": self.lam_bps, "gam": self.gamma_bps, "cum0": d0, "cum1": d1,
                 "ref": oracle_price_wad, "kappa": self.kappa(basefee, oracle_price_wad), "saturated": False,
                 "tx": tx, "C": 0, "g": abs(d1), "eps": self.eps_wad}
            self.pools[pool] = s
            if not (I128_MIN <= d0 <= I128_MAX and I128_MIN <= d1 <= I128_MAX):
                s["saturated"] = True
                return 0
        else:
            if s["saturated"]:
                return 0
            if self.accumulation == "tx_clip" and s["tx"] != tx:     # fold the completed transaction
                s["C"] += self._bracket(s, s["cum0"], s["cum1"], s["g"])
                c0, c1, s["tx"], s["g"] = d0, d1, tx, abs(d1)
            else:
                c0, c1 = s["cum0"] + d0, s["cum1"] + d1
                s["g"] += abs(d1)
            if not (I128_MIN <= c0 <= I128_MAX and I128_MIN <= c1 <= I128_MAX):
                s["saturated"] = True
                return 0
            s["cum0"], s["cum1"] = c0, c1
        if self.accumulation == "swap_clip":
            s["C"] += self._bracket(s, d0, d1, abs(d1))
        if s["C"] > U128_MAX or (self.volume == "gross" and s["g"] > U128_MAX):
            s["saturated"] = True
            return 0
        a = self.surplus(pool)
        target, _ = transfer_wad(a, s["kappa"], s["lam"] * BPS_TO_WAD, s["gam"] * BPS_TO_WAD, 0)
        if target <= s["W"]:
            return 0
        if target > U128_MAX:
            s["saturated"] = True
            return 0
        r = target - s["W"]
        price = WAD if settle_token0 else s["ref"]           # settlement-token price in token0, WAD
        if r > (2**256 - 1) // WAD:
            s["saturated"] = True
            return 0
        token = r * WAD // price
        if token > I128_MAX:                                 # does not fit the int128 return delta
            s["saturated"] = True
            return 0
        collected = -(-token * price // WAD)                  # rounded up; equals r when settling in token0
        s["W"] += collected
        self.last_token_amount = token
        return collected

    def surplus(self, pool) -> int:
        """The open scope's accumulated surplus A under the chosen accumulation (0 without a scope)."""
        s = self.pools.get(pool)
        if s is None:
            return 0
        if self.accumulation == "swap_clip":
            return s["C"]
        return s["C"] + self._bracket(s, s["cum0"], s["cum1"], s["g"])

    def _bracket(self, s: dict, c0: int, c1: int, g: int) -> int:
        """[c0 + c1 * ref / WAD - buffer(v)]^+ at the scope's locked reference and eps; v = |c1| (volume "net") or
        the gross volume g (volume "gross")."""
        if self.buffer == "none" or s["eps"] == 0:
            return hook_surplus(c0, c1, s["ref"])
        if self.volume == "net":
            g = abs(c1)
        if self.buffer == "abs":
            ded = -(-s["eps"] * g // WAD)
        else:
            ded = -(-s["eps"] * s["ref"] * g // (WAD * WAD))
        return max(c0 + _div_trunc(c1 * s["ref"], WAD) - ded, 0)


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
