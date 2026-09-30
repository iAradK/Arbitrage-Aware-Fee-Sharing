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

/// @notice Gas of a swap whose charge is settled (E7, settlement added in commit 8ca840a).
/// Three pools with identical liquidity: one with the hook and an oracle price of 1.2 (a
/// zeroForOne swap against the 1:1 pool is price-correcting and its charge is positive, so
/// the hook returns the afterSwap delta and mints ERC-6909 claims to the vault), one with
/// the hook and an oracle price of 1 (the same swap has no surplus and no charge), and one
/// without a hook. Oracle prices are written in setUp, and every case runs in its own test
/// function, so the swap starts cold as the first swap of an arbitrage transaction.
/// Output lines: SETTLEGAS,<case>,<gas>.
contract HookGasSettledTest is HookTestBase {
    uint256 internal constant AMOUNT_WAD = 10e18;   // single tick range at 1e26 liquidity; surplus above K_hat

    PoolKey internal chargedKey;
    PoolKey internal unchargedKey;
    PoolKey internal baseKey;

    function setUp() public {
        setUpHook();
        PoolId chargedId;
        PoolId unchargedId;
        (chargedKey, chargedId) = _pool(10_500, IHooks(address(hook)), 60);
        (unchargedKey, unchargedId) = _pool(10_500, IHooks(address(hook)), 120);
        (baseKey,) = _pool(10_500, IHooks(address(0)), 60);
        oracle.setPrice(bytes32(PoolId.unwrap(chargedId)), 1.2e18);
        oracle.setMode(bytes32(PoolId.unwrap(chargedId)), MockOracle.Mode.Normal);
        oracle.setPrice(bytes32(PoolId.unwrap(unchargedId)), 1e18);
        oracle.setMode(bytes32(PoolId.unwrap(unchargedId)), MockOracle.Mode.Normal);
    }

    /// @dev tickSpacing distinguishes the two hooked pools (same currencies, fee and hook).
    function _pool(uint24 fee, IHooks hooks, int24 tickSpacing) private returns (PoolKey memory key, PoolId id) {
        key = PoolKey(currency0, currency1, fee, tickSpacing, hooks);
        id = key.toId();
        manager.initialize(key, SQRT_PRICE_1_1);
        _seedDiscreteLiquidity(key);
    }

    function _swap(PoolKey memory key) private returns (uint256 used) {
        IPoolManager.SwapParams memory params =
            IPoolManager.SwapParams({zeroForOne: true, amountSpecified: -int256(AMOUNT_WAD), sqrtPriceLimitX96: MIN_PRICE_LIMIT});
        PoolSwapTest.TestSettings memory settings = PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false});
        uint256 g0 = gasleft();
        swapRouter.swap(key, params, settings, ZERO_BYTES);
        used = g0 - gasleft();
    }

    function test_charged_hook() public {
        uint256 used = _swap(chargedKey);
        assertGt(manager.balanceOf(VAULT, currency1.toId()), 0, "the charge must be settled to the vault");
        console2.log(string.concat("SETTLEGAS,charged_hook,", vm.toString(used)));
    }

    function test_uncharged_hook() public {
        uint256 used = _swap(unchargedKey);
        assertEq(manager.balanceOf(VAULT, currency1.toId()), 0, "no charge expected");
        console2.log(string.concat("SETTLEGAS,uncharged_hook,", vm.toString(used)));
    }

    function test_no_hook() public {
        uint256 used = _swap(baseKey);
        console2.log(string.concat("SETTLEGAS,no_hook,", vm.toString(used)));
    }
}
