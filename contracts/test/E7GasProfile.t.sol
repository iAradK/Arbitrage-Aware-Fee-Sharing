// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {CumulativeSurplusAccounting} from "../src/CumulativeSurplusAccounting.sol";

/// @notice E7 gas profile of the transaction-scoped accounting core: initializePool and processCallback per fragment for
/// n in {1,2,4,8,16}, for equal monotone partitions and for reversal paths (forward, adverse, recovery).
/// All calls of one scenario run inside one transaction (transient storage), as in a multicall.
contract E7GasProfileTest is Test {
    CumulativeSurplusAccounting internal cum;

    uint256 internal constant K_HAT = 40e18;
    uint256 internal constant LAMBDA = 0.95e18;
    uint256 internal constant GAMMA = 0.05e18;
    uint256 internal constant DELTA = 5e18;
    uint256 internal constant TOTAL = 100e18;

    function setUp() public {
        cum = new CumulativeSurplusAccounting();
    }

    function _row(string memory f, string memory kind, uint256 n, string memory phase, uint256 idx, uint256 gas_, uint256 charge)
        internal
    {
        if (bytes(f).length == 0) return;
        vm.writeLine(
            f,
            string.concat(kind, ",", vm.toString(n), ",", phase, ",", vm.toString(idx), ",", vm.toString(gas_), ",", vm.toString(charge))
        );
    }

    function test_GasProfile() public {
        // Written only when E7_GAS_CSV is set (experiments/e7_solidity_conformance.py sets it).
        string memory f = vm.envOr("E7_GAS_CSV", string(""));
        if (bytes(f).length != 0) vm.writeFile(f, "kind,n,phase,index,gas,marginal_charge_wad\n");
        uint256[5] memory ns = [uint256(1), 2, 4, 8, 16];
        for (uint256 a; a < ns.length; ++a) {
            uint256 n = ns[a];
            // monotone equal partition
            bytes32 pid = keccak256(abi.encode("mono", n));
            uint256 g0 = gasleft();
            cum.initializePool(pid, 1e18, 1e18, K_HAT, LAMBDA, GAMMA, DELTA, 1);
            _row(f, "monotone", n, "initialize", 0, g0 - gasleft(), 0);
            for (uint256 j; j < n; ++j) {
                g0 = gasleft();
                (, , uint256 c) = cum.processCallback(pid, int256(TOTAL / n), 0);
                _row(f, "monotone", n, "callback", j + 1, g0 - gasleft(), c);
            }
            // reversal path: forward to 100, adverse -50, recovery +50 (n >= 2)
            if (n >= 2) {
                bytes32 rid = keccak256(abi.encode("rev", n));
                g0 = gasleft();
                cum.initializePool(rid, 1e18, 1e18, K_HAT, LAMBDA, GAMMA, DELTA, 1);
                _row(f, "reversal", n, "initialize", 0, g0 - gasleft(), 0);
                uint256 nf = n / 2 == 0 ? 1 : n / 2;
                uint256 idx;
                for (uint256 j; j < nf; ++j) {
                    g0 = gasleft();
                    (, , uint256 c) = cum.processCallback(rid, int256(TOTAL / nf), 0);
                    _row(f, "reversal", n, "callback", ++idx, g0 - gasleft(), c);
                }
                g0 = gasleft();
                (, , uint256 ca) = cum.processCallback(rid, -int256(TOTAL / 2), 0);
                _row(f, "reversal", n, "callback", ++idx, g0 - gasleft(), ca);
                uint256 rest = n - nf - 1;
                for (uint256 j; j < rest; ++j) {
                    g0 = gasleft();
                    (, , uint256 cr) = cum.processCallback(rid, int256(TOTAL / 2 / rest), 0);
                    _row(f, "reversal", n, "callback", ++idx, g0 - gasleft(), cr);
                }
            }
        }
    }
}
