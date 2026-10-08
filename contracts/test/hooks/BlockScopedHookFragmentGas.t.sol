// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {console2} from "forge-std/console2.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "v4-core/src/types/PoolId.sol";

import {BlockScopedHookTestBase, SwapProbe} from "./BlockScopedHookTestBase.sol";

/// @notice Per-fragment gas of the final block-scoped hook (E7, replaces the transaction-scoped HookGasIsolatedTest
/// figures). Run with `forge test --isolate`: every probe call is its own transaction, storage written by earlier
/// transactions is nonzero and cold, as on chain. A correction of 1e17 token0 (single tick range, price 1.0, no charge)
/// is split into n equal fragments, n in {1, 2, 4, 8, 16}, in three layouts:
///   tx      all fragments in one transaction (probe.swapMany), as HookGasIsolatedTest
///   txs     one transaction per fragment, all in one block
///   blocks  one transaction per fragment, each in its own block (vm.roll; the oracle price moves between blocks)
/// Each layout starts from steady state: the pool has had a scope in an earlier block and the oracle price has moved.
/// The same sequence runs on a pool without a hook. Overhead(n) = sum over fragments of gas with the hook minus without.
/// Output lines: BSFRAG,<layout>,<n>,<hook 0|1>,<fragment>,<gas>. The hook is _finalParams (P-net, eps 28.3 bp,
/// g_hat 180,214, tau 3 gwei, gasPriceToken0Wad 1e18).
contract BlockScopedHookFragmentGasTest is BlockScopedHookTestBase {
    using PoolIdLibrary for PoolKey;

    uint256 internal constant AMOUNT = 1e17;
    SwapProbe internal probe;
    PoolKey internal hooked;
    PoolKey internal plain;

    function setUp() public {
        setUpBase();
        probe = _probe();
        (hooked,) = _pool(IHooks(address(_deployHook(_finalParams(K_HAT)))), 1e18, 3000);
        (plain,) = _pool(IHooks(address(0)), 0, 3000);
    }

    /// @dev Steady state: a scope in an earlier block, then a new block with a moved oracle price.
    function _warm(PoolKey memory key, bool hook) internal {
        probe.swap(key, _exactIn(true, AMOUNT));
        vm.roll(vm.getBlockNumber() + 1);
        if (hook) oracle.setPrice(PoolId.unwrap(key.toId()), 1.0001e18);
    }

    function _log(string memory layout, uint256 n, bool hook, uint256 j, uint256 used) internal pure {
        console2.log(string.concat("BSFRAG,", layout, ",", vm.toString(n), ",", hook ? "1" : "0", ",", vm.toString(j + 1), ",",
            vm.toString(used)));
    }

    function _run(string memory layout, uint256 n, bool hook) internal {
        _assertIsolated();
        PoolKey memory key = hook ? hooked : plain;
        _warm(key, hook);
        IPoolManager.SwapParams memory p = _exactIn(true, AMOUNT / n);
        bytes32 L = keccak256(bytes(layout));
        if (L == keccak256("tx")) {
            IPoolManager.SwapParams[] memory ps = new IPoolManager.SwapParams[](n);
            for (uint256 j; j < n; ++j) ps[j] = p;
            uint256[] memory used = probe.swapMany(key, ps);
            for (uint256 j; j < n; ++j) _log(layout, n, hook, j, used[j]);
        } else {
            for (uint256 j; j < n; ++j) {
                if (L == keccak256("blocks") && j > 0) {
                    vm.roll(vm.getBlockNumber() + 1);
                    if (hook) oracle.setPrice(PoolId.unwrap(key.toId()), j % 2 == 0 ? 1.0001e18 : 1.0002e18);
                }
                _log(layout, n, hook, j, probe.swap(key, p));
            }
        }
    }

    function test_tx_n01() public { _run("tx", 1, true); _run("tx", 1, false); }
    function test_tx_n02() public { _run("tx", 2, true); _run("tx", 2, false); }
    function test_tx_n04() public { _run("tx", 4, true); _run("tx", 4, false); }
    function test_tx_n08() public { _run("tx", 8, true); _run("tx", 8, false); }
    function test_tx_n16() public { _run("tx", 16, true); _run("tx", 16, false); }
    function test_txs_n02() public { _run("txs", 2, true); _run("txs", 2, false); }
    function test_txs_n04() public { _run("txs", 4, true); _run("txs", 4, false); }
    function test_txs_n08() public { _run("txs", 8, true); _run("txs", 8, false); }
    function test_txs_n16() public { _run("txs", 16, true); _run("txs", 16, false); }
    function test_blocks_n02() public { _run("blocks", 2, true); _run("blocks", 2, false); }
    function test_blocks_n04() public { _run("blocks", 4, true); _run("blocks", 4, false); }
    function test_blocks_n08() public { _run("blocks", 8, true); _run("blocks", 8, false); }
    function test_blocks_n16() public { _run("blocks", 16, true); _run("blocks", 16, false); }
}
