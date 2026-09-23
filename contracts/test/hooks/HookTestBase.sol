// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Deployers} from "v4-core/test/utils/Deployers.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {Hooks} from "v4-core/src/libraries/Hooks.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId} from "v4-core/src/types/PoolId.sol";
import {Currency} from "v4-core/src/types/Currency.sol";
import {StateLibrary} from "v4-core/src/libraries/StateLibrary.sol";
import {PoolSwapTest} from "v4-core/src/test/PoolSwapTest.sol";

import {HookMiner} from "../utils/HookMiner.sol";
import {ParticipationAwareHook} from "../../src/hooks/ParticipationAwareHook.sol";
import {MockOracle} from "../../src/mocks/MockOracle.sol";

/// @notice Shared fixture deploying a real PoolManager, a real mined
/// ParticipationAwareHook, and two discrete-tick-range liquidity pools sharing the
/// same hook instance (used for the cross-pool isolation fuzz test). Liquidity is
/// seeded across several narrow, non-overlapping tick ranges rather than one
/// full-range position, so ordinary-sized swaps actually cross ticks.
abstract contract HookTestBase is Deployers {
    using StateLibrary for IPoolManager;

    uint160 internal constant HOOK_FLAGS =
        uint160(Hooks.BEFORE_SWAP_FLAG | Hooks.AFTER_SWAP_FLAG);

    MockOracle internal oracle;
    ParticipationAwareHook internal hook;

    PoolKey internal poolA;
    PoolId internal poolAId;
    PoolKey internal poolB;
    PoolId internal poolBId;

    uint256 internal constant EXECUTION_MARGIN_HAT_WAD = 1e18;
    uint256 internal constant LAMBDA_WAD = 0.75e18;
    uint256 internal constant GAMMA_WAD = 0.05e18;
    uint256 internal constant DELTA_WAD = 0;
    uint256 internal constant STALENESS_THRESHOLD_SECONDS = 3600;

    function setUpHook() internal {
        deployFreshManagerAndRouters();
        deployMintAndApprove2Currencies();

        oracle = new MockOracle();

        bytes memory constructorArgs = abi.encode(
            manager,
            oracle,
            EXECUTION_MARGIN_HAT_WAD,
            LAMBDA_WAD,
            GAMMA_WAD,
            DELTA_WAD,
            STALENESS_THRESHOLD_SECONDS
        );
        (address minedAddress, bytes32 salt) = HookMiner.find(
            address(this), HOOK_FLAGS, type(ParticipationAwareHook).creationCode, constructorArgs
        );
        hook = new ParticipationAwareHook{salt: salt}(
            manager, oracle, EXECUTION_MARGIN_HAT_WAD, LAMBDA_WAD, GAMMA_WAD, DELTA_WAD, STALENESS_THRESHOLD_SECONDS
        );
        require(address(hook) == minedAddress, "HookTestBase: hook address mismatch");

        (poolA, poolAId) = initPool(currency0, currency1, IHooks(address(hook)), 3000, SQRT_PRICE_1_1);
        _seedDiscreteLiquidity(poolA);

        (poolB, poolBId) = _deployPoolWithHook(IHooks(address(hook)));

        oracle.setPrice(bytes32(PoolId.unwrap(poolAId)), 1e18);
        oracle.setPrice(bytes32(PoolId.unwrap(poolBId)), 1e18);
    }

    /// @notice Deploy a fresh currency pair (minted and approved to the shared
    /// routers), initialize a pool keyed to the given hooks address, and seed it
    /// with the same discrete tick-range liquidity as poolA/poolB. Used by tests
    /// that need their own hook instance (e.g. with fuzzed economic parameters)
    /// deployed against a pool independent from the shared poolA/poolB fixtures.
    function _deployPoolWithHook(IHooks hooks) internal returns (PoolKey memory key, PoolId id) {
        (Currency currencyA, Currency currencyB) = deployAndMint2Currencies();
        _approveForSwapRouter(currencyA);
        _approveForSwapRouter(currencyB);
        key = PoolKey(currencyA, currencyB, 3000, 60, hooks);
        id = key.toId();
        manager.initialize(key, SQRT_PRICE_1_1);
        _seedDiscreteLiquidity(key);
    }

    /// @dev Several narrow, non-overlapping tick ranges around the current price, so
    /// swaps of ordinary size cross at least one tick boundary instead of trading
    /// entirely inside a single full-range position.
    function _seedDiscreteLiquidity(PoolKey memory key) internal {
        int24[6] memory lowerTicks = [int24(-1800), -1200, -600, 0, 600, 1200];
        for (uint256 i = 0; i < lowerTicks.length; i++) {
            int24 tickLower = lowerTicks[i];
            int24 tickUpper = tickLower + 600;
            modifyLiquidityRouter.modifyLiquidity(
                key,
                IPoolManager.ModifyLiquidityParams({
                    tickLower: tickLower,
                    tickUpper: tickUpper,
                    liquidityDelta: 1e26,
                    salt: 0
                }),
                ZERO_BYTES
            );
        }
    }

    function _approveForSwapRouter(Currency currency) private {
        MockERC20Approve(Currency.unwrap(currency)).approve(address(swapRouter), type(uint256).max);
        MockERC20Approve(Currency.unwrap(currency)).approve(address(modifyLiquidityRouter), type(uint256).max);
    }

    function _currentTick(PoolId id) internal view returns (int24 tick) {
        (, tick,,) = manager.getSlot0(id);
    }

    /// @dev Number of tick-spacing-aligned initialized-tick boundaries crossed,
    /// approximated as the absolute tick delta divided by the pool's tick spacing.
    function _ticksCrossed(int24 tickBefore, int24 tickAfter, int24 tickSpacing) internal pure returns (uint256) {
        int24 delta = tickAfter >= tickBefore ? tickAfter - tickBefore : tickBefore - tickAfter;
        return uint256(int256(delta)) / uint256(int256(int256(tickSpacing)));
    }
}

interface MockERC20Approve {
    function approve(address spender, uint256 amount) external returns (bool);
}