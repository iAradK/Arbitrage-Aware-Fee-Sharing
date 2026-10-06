#!/usr/bin/env python3
"""FFI bridge for contracts/test/hooks/BlockScopeFuzz.t.sol: replays a swap trace through the block-scoped integer
reference (common.fixedpoint.ScopedHookReference) and prints the expected (W increase, settlement token amount) of
every swap as an ABI-encoded uint256[] (0x-prefixed hex, the format vm.ffi decodes).

  python3 block_scope_ffi.py K DELTA LAMBDA_BPS GAMMA_BPS GAS_UNITS TAU_WEI GAS_PRICE_TOKEN0_WAD  \
          [BLOCK ORACLE_PRICE_WAD BASEFEE D0 D1 SETTLE_TOKEN0] ...

ORACLE_PRICE_WAD = 0 means the oracle read would be rejected. Standard library only (runs in WSL's python3).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.fixedpoint import ScopedHookReference  # noqa: E402


def main(argv):
    k, delta, lam, gam, gas, tau, gp = (int(x) for x in argv[:7])
    rest = [int(x) for x in argv[7:]]
    assert len(rest) % 6 == 0, "trace must have 6 fields per swap"
    h = ScopedHookReference(k, delta, lam, gam, gas_units=gas, tau_wei=tau, gas_price_token0_wad=gp)
    out = []
    for i in range(0, len(rest), 6):
        block, price, basefee, d0, d1, settle0 = rest[i:i + 6]
        w_inc = h.swap("pool", block, i // 6, d0, d1, price or None, basefee=basefee, settle_token0=bool(settle0))
        out += [w_inc, h.last_token_amount]
    words = [0x20, len(out)] + out
    sys.stdout.write("0x" + "".join(f"{w:064x}" for w in words))


if __name__ == "__main__":
    main(sys.argv[1:])
