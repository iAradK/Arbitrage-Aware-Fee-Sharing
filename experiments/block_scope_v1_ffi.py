#!/usr/bin/env python3
"""FFI bridge for BlockScopeFuzz.t.sol and the tests using BlockScopedHookTestBase._refRun (final hook): replays a swap trace
through common.fixedpoint.ScopedHookReference(scope="block", accumulation="tx_clip", buffer="rel") and prints, per swap, the
expected (W increase, settlement token amount, block surplus A after the swap; 2^256 - 1 when no scope is open for
the swap's block) as an ABI-encoded uint256[] (0x hex, as vm.ffi decodes).

  python3 block_scope_v1_ffi.py K LAMBDA_BPS GAMMA_BPS GAS_UNITS TAU_WEI GAS_PRICE_TOKEN0_WAD EPS_WAD \
          [BLOCK TX ORACLE_PRICE_WAD BASEFEE D0 D1 SETTLE_TOKEN0] ...

ORACLE_PRICE_WAD = 0: the oracle read would be rejected. Standard library only.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.fixedpoint import ScopedHookReference  # noqa: E402

NONE = 2**256 - 1


def main(argv):
    k, lam, gam, gas, tau, gp, eps = (int(x) for x in argv[:7])
    rest = [int(x) for x in argv[7:]]
    assert len(rest) % 7 == 0, "trace must have 7 fields per swap"
    h = ScopedHookReference(k, 0, lam, gam, gas_units=gas, tau_wei=tau, gas_price_token0_wad=gp, scope="block",
                            accumulation="tx_clip", buffer="rel", eps_wad=eps)
    out = []
    for i in range(0, len(rest), 7):
        block, tx, price, basefee, d0, d1, settle0 = rest[i:i + 7]
        w = h.swap("pool", block, tx, d0, d1, price or None, basefee=basefee, settle_token0=bool(settle0))
        s = h.pools.get("pool")
        a = h.surplus("pool") if s is not None and s["key"] == block and not s["saturated"] else NONE
        out += [w, h.last_token_amount, a]
    words = [0x20, len(out)] + out
    sys.stdout.write("0x" + "".join(f"{x:064x}" for x in words))


if __name__ == "__main__":
    main(sys.argv[1:])
