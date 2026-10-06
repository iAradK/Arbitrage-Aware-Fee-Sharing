// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId} from "v4-core/src/types/PoolId.sol";
import {BalanceDelta} from "v4-core/src/types/BalanceDelta.sol";
import {PoolSwapTest} from "v4-core/src/test/PoolSwapTest.sol";

import {HookTestBase} from "./HookTestBase.sol";
import {TxScopedParticipationAwareHook} from "../../src/hooks/TxScopedParticipationAwareHook.sol";
import {MockOracle} from "../../src/mocks/MockOracle.sol";

/// @notice Full-lifecycle hook gas benchmark: measures gasleft() around the entire
/// swapRouter.swap(...) call (oracle access, transient-storage tracking, and
/// callback routing all included), not just the arithmetic-core figure in
/// SurplusSharingAccounting.t.sol/CumulativeSurplusAccounting.t.sol. Also records a
/// hook-disabled (hooks = address(0)) baseline swap of the identical size on an
/// identically-liquidity-seeded pool, so the hook's marginal overhead can be
/// isolated from ordinary swap gas. Each (fragments, tick-crossing class,
/// oracle_mode) combination gets its own fresh PoolKey (same currencies, a
/// scenario-unique fee so the derived poolId is fresh) so scenarios stay isolated
/// from each other even though the whole sweep runs inside a single transaction.
contract HookGasBenchmarkTest is HookTestBase {
    uint256[5] internal FRAGMENT_COUNTS = [uint256(1), 2, 4, 8, 16];

    // "single-tick" amounts are sized to stay within the first liquidity range;
    // "multi-tick" amounts are sized to push through several of the discrete
    // ranges seeded by HookTestBase._seedDiscreteLiquidity.
    uint256 internal constant SINGLE_TICK_AMOUNT_WAD = 1e17;
    uint256 internal constant MULTI_TICK_AMOUNT_WAD = 8e23;

    function setUp() public {
        setUpHook();
    }

    function test_GasBenchmarkFullLifecycle() public {
        string memory output = vm.envOr("HOOK_LIFECYCLE_GAS_OUTPUT", string("results/hook_lifecycle_gas.csv"));
        vm.writeFile(
            output,
            "scenario_id,fragments,fragment_index,oracle_mode,swap_amount_wad,zero_for_one,ticks_crossed,tick_crossing_class,cap_binds,gas_used_with_hook,gas_used_without_hook,marginal_charge_wad,cumulative_target_wad,reverted\n"
        );

        string[2] memory tickClasses = ["single-tick", "multi-tick"];
        uint256[2] memory amounts = [SINGLE_TICK_AMOUNT_WAD, MULTI_TICK_AMOUNT_WAD];
        string[5] memory oracleModeNames = ["valid", "stale", "zero-price", "incomplete-round", "self-invalid"];
        MockOracle.Mode[5] memory oracleModes = [
            MockOracle.Mode.Normal,
            MockOracle.Mode.ForceStale,
            MockOracle.Mode.ZeroPrice,
            MockOracle.Mode.IncompleteRound,
            MockOracle.Mode.SelfReportedInvalid
        ];

        uint256 scenarioId;
        for (uint256 f; f < FRAGMENT_COUNTS.length; ++f) {
            uint256 fragments = FRAGMENT_COUNTS[f];
            for (uint256 c; c < tickClasses.length; ++c) {
                for (uint256 m; m < oracleModes.length; ++m) {
                    _runScenario(
                        output,
                        scenarioId,
                        fragments,
                        oracleModeNames[m],
                        oracleModes[m],
                        amounts[c] / fragments,
                        tickClasses[c]
                    );
                    scenarioId++;
                }
            }
        }
    }

    function _runScenario(
        string memory output,
        uint256 scenarioId,
        uint256 fragments,
        string memory oracleModeName,
        MockOracle.Mode oracleMode,
        uint256 fragmentAmount,
        string memory tickClass
    ) private {
        // Offset away from 3000: HookTestBase.setUpHook() already initializes
        // poolA/poolB at fee 3000 with the same currencies and hook address.
        uint24 fee = uint24(10_000 + scenarioId);
        (PoolKey memory hookKey, PoolId hookId) = _scenarioPool(fee, IHooks(address(hook)));
        (PoolKey memory baseKey,) = _scenarioPool(fee, IHooks(address(0)));

        bytes32 hookPoolIdBytes = bytes32(PoolId.unwrap(hookId));
        oracle.setPrice(hookPoolIdBytes, 1e18);
        oracle.setMode(hookPoolIdBytes, oracleMode);

        int24 tickBefore = _currentTick(hookId);
        for (uint256 j; j < fragments; ++j) {
            (uint256 gasWithHook, bool revertedWithHook) = _timedSwap(hookKey, true, fragmentAmount);
            int24 tickAfter = _currentTick(hookId);
            uint256 ticksCrossed = _ticksCrossed(tickBefore, tickAfter, hookKey.tickSpacing);
            tickBefore = tickAfter;

            (uint256 gasWithoutHook, bool revertedBaseline) = _timedSwap(baseKey, true, fragmentAmount);

            TxScopedParticipationAwareHook.TransientState memory state = hook.getTransientState(hookPoolIdBytes);
            uint256 surplus = _surplus(state);
            (uint256 cumulativeTarget, bool capBinds) =
                hook.computeTransfer(surplus, state.executionMarginHat, state.lambdaWad, state.gammaWad, state.delta);

            _writeGasRow(
                output,
                scenarioId,
                fragments,
                j + 1,
                oracleModeName,
                fragmentAmount,
                true,
                ticksCrossed,
                tickClass,
                capBinds,
                gasWithHook,
                gasWithoutHook,
                state.watermark,
                cumulativeTarget,
                revertedWithHook || revertedBaseline
            );
        }
    }

    /// @dev Recomputes cumulativeSurplus the same way CumulativeSurplusAccountingLib
    /// does internally, from the transient cumulative deltas and reference prices
    /// exposed via getTransientState (the library itself only exposes this value
    /// transiently mid-accumulate(), not as a standalone view).
    function _surplus(TxScopedParticipationAwareHook.TransientState memory state) private pure returns (uint256) {
        int256 value0 = (state.cumulativeDelta0Wad * int256(state.referencePrice0Wad)) / 1e18;
        int256 value1 = (state.cumulativeDelta1Wad * int256(state.referencePrice1Wad)) / 1e18;
        int256 total = value0 + value1;
        return total > 0 ? uint256(total) : 0;
    }

    function _scenarioPool(uint24 fee, IHooks hooks) private returns (PoolKey memory key, PoolId id) {
        key = PoolKey(currency0, currency1, fee, 60, hooks);
        id = key.toId();
        manager.initialize(key, SQRT_PRICE_1_1);
        _seedDiscreteLiquidity(key);
    }

    function _timedSwap(PoolKey memory key, bool zeroForOne, uint256 amount)
        private
        returns (uint256 gasUsed, bool reverted)
    {
        IPoolManager.SwapParams memory params = IPoolManager.SwapParams({
            zeroForOne: zeroForOne,
            amountSpecified: -int256(amount),
            sqrtPriceLimitX96: zeroForOne ? MIN_PRICE_LIMIT : MAX_PRICE_LIMIT
        });
        PoolSwapTest.TestSettings memory settings =
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false});

        uint256 gasBefore = gasleft();
        try swapRouter.swap(key, params, settings, ZERO_BYTES) returns (BalanceDelta) {
            gasUsed = gasBefore - gasleft();
            reverted = false;
        } catch {
            gasUsed = gasBefore - gasleft();
            reverted = true;
        }
    }

    function _writeGasRow(
        string memory output,
        uint256 scenarioId,
        uint256 fragments,
        uint256 fragmentIndex,
        string memory oracleModeName,
        uint256 swapAmountWad,
        bool zeroForOne,
        uint256 ticksCrossed,
        string memory tickClass,
        bool capBinds,
        uint256 gasUsedWithHook,
        uint256 gasUsedWithoutHook,
        uint256 marginalChargeWad,
        uint256 cumulativeTargetWad,
        bool reverted
    ) private {
        vm.writeLine(
            output,
            string.concat(
                vm.toString(scenarioId),
                ",",
                vm.toString(fragments),
                ",",
                vm.toString(fragmentIndex),
                ",",
                oracleModeName,
                ",",
                vm.toString(swapAmountWad),
                ",",
                vm.toString(zeroForOne),
                ",",
                vm.toString(ticksCrossed),
                ",",
                tickClass,
                ",",
                vm.toString(capBinds),
                ",",
                vm.toString(gasUsedWithHook),
                ",",
                string.concat(
                    vm.toString(gasUsedWithoutHook),
                    ",",
                    vm.toString(marginalChargeWad),
                    ",",
                    vm.toString(cumulativeTargetWad),
                    ",",
                    vm.toString(reverted)
                )
            )
        );
    }
}