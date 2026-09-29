// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {console2} from "forge-std/console2.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId} from "v4-core/src/types/PoolId.sol";
import {PoolSwapTest} from "v4-core/src/test/PoolSwapTest.sol";

import {HookTestBase} from "./HookTestBase.sol";
import {MockOracle} from "../../src/mocks/MockOracle.sol";

/// @notice Hook gas with every fragment count in its own transaction (E7 v2, fix 5).
/// HookGasBenchmark runs all scenarios inside one test function, so only its very first
/// swap pays cold-access costs. Here setUp() deploys the pools, and each test function
/// (one per fragment count and per pool with/without the hook) is a separate transaction:
/// the first fragment of every run starts cold and later fragments are warm, as they would
/// be inside one arbitrage transaction. Overhead(n) = sum over fragments of gas with the
/// hook minus the same sum without it. Output lines: ISOGAS,<n>,<hook 0|1>,<fragment>,<gas>.
contract HookGasIsolatedTest is HookTestBase {
    uint256 internal constant SINGLE_TICK_AMOUNT_WAD = 1e17;

    PoolKey internal hookKey;
    PoolKey internal baseKey;

    function setUp() public {
        setUpHook();
        PoolId hookId;
        (hookKey, hookId) = _pool(10_500, IHooks(address(hook)));
        (baseKey,) = _pool(10_500, IHooks(address(0)));
        oracle.setPrice(bytes32(PoolId.unwrap(hookId)), 1e18);
        oracle.setMode(bytes32(PoolId.unwrap(hookId)), MockOracle.Mode.Normal);
    }

    function _pool(uint24 fee, IHooks hooks) private returns (PoolKey memory key, PoolId id) {
        key = PoolKey(currency0, currency1, fee, 60, hooks);
        id = key.toId();
        manager.initialize(key, SQRT_PRICE_1_1);
        _seedDiscreteLiquidity(key);
    }

    function _run(uint256 n, bool withHook) private {
        PoolKey memory key = withHook ? hookKey : baseKey;
        uint256 amount = SINGLE_TICK_AMOUNT_WAD / n;
        IPoolManager.SwapParams memory params =
            IPoolManager.SwapParams({zeroForOne: true, amountSpecified: -int256(amount), sqrtPriceLimitX96: MIN_PRICE_LIMIT});
        PoolSwapTest.TestSettings memory settings = PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false});
        for (uint256 j; j < n; ++j) {
            uint256 g0 = gasleft();
            swapRouter.swap(key, params, settings, ZERO_BYTES);
            uint256 used = g0 - gasleft();
            console2.log(
                string.concat("ISOGAS,", vm.toString(n), ",", withHook ? "1" : "0", ",", vm.toString(j + 1), ",", vm.toString(used))
            );
        }
    }

    function test_n01_hook() public { _run(1, true); }
    function test_n01_base() public { _run(1, false); }
    function test_n02_hook() public { _run(2, true); }
    function test_n02_base() public { _run(2, false); }
    function test_n04_hook() public { _run(4, true); }
    function test_n04_base() public { _run(4, false); }
    function test_n08_hook() public { _run(8, true); }
    function test_n08_base() public { _run(8, false); }
    function test_n16_hook() public { _run(16, true); }
    function test_n16_base() public { _run(16, false); }
}
