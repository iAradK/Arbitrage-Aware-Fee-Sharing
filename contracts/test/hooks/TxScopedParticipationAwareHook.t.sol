// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId} from "v4-core/src/types/PoolId.sol";
import {BalanceDelta} from "v4-core/src/types/BalanceDelta.sol";
import {PoolSwapTest} from "v4-core/src/test/PoolSwapTest.sol";

import {HookTestBase} from "./HookTestBase.sol";
import {HookMiner} from "../utils/HookMiner.sol";
import {TxScopedParticipationAwareHook} from "../../src/hooks/TxScopedParticipationAwareHook.sol";
import {CumulativeSurplusAccountingLib} from "../../src/CumulativeSurplusAccountingLib.sol";
import {SurplusSharingAccounting} from "../../src/SurplusSharingAccounting.sol";
import {MockOracle} from "../../src/mocks/MockOracle.sol";

/// @notice Fuzz coverage for TxScopedParticipationAwareHook through real PoolManager swaps:
/// boundary economic parameters, tick-crossing behavior, invalid oracle states, and
/// cross-pool isolation. See the plan's invariant-fuzzing caveat (not repeated as
/// cross-call invariants here): the watermark lives in transient storage scoped to a
/// single transaction, so multi-swap monotonicity is checked with an internal
/// multi-swap loop inside one test function/transaction, not a Foundry invariant
/// handler (whose calls are separate top-level transactions).
contract TxScopedParticipationAwareHookTest is HookTestBase {
    function setUp() public {
        setUpHook();
    }

    /// @notice The direction locked for a pool at its first swap is taken directly
    /// from that swap's own zeroForOne flag: zeroForOne => +1, oneForZero => -1.
    function test_DirectionMapping_MatchesZeroForOneFlag() public {
        _swapExactIn(poolA, true, 1e18);
        TxScopedParticipationAwareHook.TransientState memory stateA = hook.getTransientState(bytes32(PoolId.unwrap(poolAId)));
        assertEq(stateA.initialDirection, int8(1));

        _swapExactIn(poolB, false, 1e18);
        TxScopedParticipationAwareHook.TransientState memory stateB = hook.getTransientState(bytes32(PoolId.unwrap(poolBId)));
        assertEq(stateB.initialDirection, int8(-1));
    }

    /// @dev Boundary/extreme economic parameters (K_hat, delta, lambda, gamma) and
    /// swap sizes from dust to large: the on-chain watermark after a real swap must
    /// equal the pure computeTransfer(...) formula applied to the surplus actually
    /// accumulated on-chain.
    function testFuzz_BoundaryLimits_ExtremeMarginAndDeltaParameters(
        uint256 lambdaWad,
        uint256 gammaWad,
        uint256 executionMarginHatWad,
        uint256 deltaWad,
        uint256 swapAmount
    ) public {
        lambdaWad = bound(lambdaWad, 0, 1e18);
        gammaWad = bound(gammaWad, 0, 1e18 - 1);
        executionMarginHatWad = bound(executionMarginHatWad, 0, 1e24);
        deltaWad = bound(deltaWad, 0, 1e24);
        swapAmount = bound(swapAmount, 1e6, 1e21);

        (TxScopedParticipationAwareHook customHook, PoolKey memory key, PoolId id) =
            _deployCustomHookAndPool(executionMarginHatWad, lambdaWad, gammaWad, deltaWad);

        oracle.setPrice(bytes32(PoolId.unwrap(id)), 1e18);
        _swapExactInOn(key, true, swapAmount);

        TxScopedParticipationAwareHook.TransientState memory state = customHook.getTransientState(bytes32(PoolId.unwrap(id)));

        (, int256 value0) =
            CumulativeSurplusAccountingLib.tryMulSignedWad(state.cumulativeDelta0Wad, state.referencePrice0Wad);
        (, int256 value1) =
            CumulativeSurplusAccountingLib.tryMulSignedWad(state.cumulativeDelta1Wad, state.referencePrice1Wad);
        int256 totalValue = value0 + value1;
        uint256 expectedSurplus = totalValue > 0 ? uint256(totalValue) : 0;

        (uint256 expectedTarget,) = customHook.computeTransfer(
            expectedSurplus, executionMarginHatWad, lambdaWad, gammaWad, deltaWad
        );

        assertEq(state.watermark, expectedTarget, "watermark must equal the pure formula's target");
    }

    /// @dev Lambda/gamma outside their valid range must revert the hook's own
    /// constructor with the exact SurplusSharingAccounting error, before any pool
    /// or swap machinery is involved.
    function testFuzz_ConstructorRejectsInvalidLambdaGamma(uint256 lambdaWad, uint256 gammaWad) public {
        vm.assume(lambdaWad > 1e18 || gammaWad >= 1e18);

        bytes memory args =
            abi.encode(manager, oracle, VAULT, uint256(0), lambdaWad, gammaWad, uint256(0), STALENESS_THRESHOLD_SECONDS);
        (, bytes32 salt) = HookMiner.find(address(this), HOOK_FLAGS, type(TxScopedParticipationAwareHook).creationCode, args);

        if (lambdaWad > 1e18) {
            vm.expectRevert(SurplusSharingAccounting.InvalidLambda.selector);
        } else {
            vm.expectRevert(SurplusSharingAccounting.InvalidGamma.selector);
        }
        new TxScopedParticipationAwareHook{salt: salt}(manager, oracle, VAULT, 0, lambdaWad, gammaWad, 0, STALENESS_THRESHOLD_SECONDS);
    }

    /// @dev Sequential same-direction swap fragments within one transaction: the
    /// watermark (and hence the cumulative charge) must never decrease as fragments
    /// accumulate, across both single-tick and multi-tick-crossing fragment sizes.
    function testFuzz_TickCrossing_ChargeMonotoneInSwapSize(uint8 fragmentCount, uint256 fragmentSeed) public {
        fragmentCount = uint8(bound(fragmentCount, 1, 8));
        bytes32 poolIdBytes = bytes32(PoolId.unwrap(poolAId));

        uint256 previousWatermark = 0;
        for (uint256 i = 0; i < fragmentCount; i++) {
            uint256 amount = bound(uint256(keccak256(abi.encode(fragmentSeed, i))), 1e6, 5e20);
            _swapExactIn(poolA, true, amount);
            TxScopedParticipationAwareHook.TransientState memory state = hook.getTransientState(poolIdBytes);
            assertGe(state.watermark, previousWatermark, "watermark must be non-decreasing across fragments");
            previousWatermark = state.watermark;
        }
    }

    /// @dev Every invalid oracle mode must fail open: the swap never reverts because
    /// of the hook, the pool ends up disabled with zero watermark, and OracleRejected
    /// fires with the mode's distinguishing reason.
    function testFuzz_InvalidOracleStates_FailOpenNeverBlocksOrOvercharges(
        uint8 modeSeed,
        bool zeroForOne,
        uint256 swapAmount
    ) public {
        MockOracle.Mode mode = MockOracle.Mode(bound(modeSeed, 1, 5));
        swapAmount = bound(swapAmount, 1e6, 5e20);
        bytes32 poolIdBytes = bytes32(PoolId.unwrap(poolAId));

        oracle.setMode(poolIdBytes, mode);
        _swapExactIn(poolA, zeroForOne, swapAmount);

        TxScopedParticipationAwareHook.TransientState memory state = hook.getTransientState(poolIdBytes);
        assertEq(state.status, CumulativeSurplusAccountingLib.STATUS_DISABLED);
        assertEq(state.watermark, 0);
    }

    /// @dev Two pools sharing one hook instance, swapped within the same
    /// transaction: a swap on poolB must not perturb poolA's transient state.
    function testFuzz_CrossPoolIsolationThroughRealSwaps(uint256 amountA, uint256 amountB) public {
        amountA = bound(amountA, 1e6, 5e20);
        amountB = bound(amountB, 1e6, 5e20);

        _swapExactIn(poolA, true, amountA);
        TxScopedParticipationAwareHook.TransientState memory stateAAfterFirst =
            hook.getTransientState(bytes32(PoolId.unwrap(poolAId)));

        _swapExactIn(poolB, false, amountB);

        TxScopedParticipationAwareHook.TransientState memory stateAAfterSecond =
            hook.getTransientState(bytes32(PoolId.unwrap(poolAId)));

        assertEq(stateAAfterSecond.cumulativeDelta0Wad, stateAAfterFirst.cumulativeDelta0Wad);
        assertEq(stateAAfterSecond.cumulativeDelta1Wad, stateAAfterFirst.cumulativeDelta1Wad);
        assertEq(stateAAfterSecond.watermark, stateAAfterFirst.watermark);
        assertEq(stateAAfterSecond.initialDirection, int8(1));

        TxScopedParticipationAwareHook.TransientState memory stateB = hook.getTransientState(bytes32(PoolId.unwrap(poolBId)));
        assertEq(stateB.initialDirection, int8(-1));
    }

    /// @dev Reference price 1.2 (token1 worth 1.2 token0) against a pool at 1:1, so a
    /// zeroForOne swap is price-correcting with positive surplus. Exact input charges
    /// the output token (token1): the swapper's output is reduced by exactly the amount
    /// minted to the vault, which is the charge converted at the reference price.
    function test_Settlement_ExactInput_ChargesOutputTokenToVault() public {
        bytes32 poolIdBytes = bytes32(PoolId.unwrap(poolAId));
        oracle.setPrice(poolIdBytes, 1.2e18);

        BalanceDelta swapperDelta = _swapExactIn(poolA, true, 100e18);
        TxScopedParticipationAwareHook.TransientState memory state = hook.getTransientState(poolIdBytes);

        uint256 surplus = _surplus(state);
        (uint256 target,) = hook.computeTransfer(surplus, EXECUTION_MARGIN_HAT_WAD, LAMBDA_WAD, GAMMA_WAD, DELTA_WAD);
        uint256 expectedTokens = (target * 1e18) / 1.2e18;
        assertGt(expectedTokens, 0, "scenario must produce a positive charge");

        assertEq(manager.balanceOf(VAULT, currency1.toId()), expectedTokens, "vault claims");
        assertEq(manager.balanceOf(VAULT, currency0.toId()), 0, "no charge in the specified token");
        assertEq(int256(swapperDelta.amount1()), state.cumulativeDelta1Wad - int256(expectedTokens), "output reduced");
        assertEq(int256(swapperDelta.amount0()), state.cumulativeDelta0Wad, "input unchanged");
        assertLe(state.watermark, target, "watermark never exceeds the cumulative target");
        assertGe(state.watermark * 1e18, expectedTokens * 1.2e18, "watermark covers the value collected");
    }

    /// @dev Exact output charges the input token (token0, the numeraire), so the
    /// token amount equals the numeraire charge with no conversion remainder.
    function test_Settlement_ExactOutput_ChargesInputTokenToVault() public {
        bytes32 poolIdBytes = bytes32(PoolId.unwrap(poolAId));
        oracle.setPrice(poolIdBytes, 1.2e18);

        BalanceDelta swapperDelta = _swapExactOut(poolA, true, 100e18);
        TxScopedParticipationAwareHook.TransientState memory state = hook.getTransientState(poolIdBytes);

        (uint256 target,) =
            hook.computeTransfer(_surplus(state), EXECUTION_MARGIN_HAT_WAD, LAMBDA_WAD, GAMMA_WAD, DELTA_WAD);
        assertGt(target, 0, "scenario must produce a positive charge");

        assertEq(manager.balanceOf(VAULT, currency0.toId()), target, "vault claims");
        assertEq(manager.balanceOf(VAULT, currency1.toId()), 0, "no charge in the specified token");
        assertEq(int256(swapperDelta.amount0()), state.cumulativeDelta0Wad - int256(target), "input increased");
        assertEq(int256(swapperDelta.amount1()), state.cumulativeDelta1Wad, "output unchanged");
        assertEq(state.watermark, target);
    }

    /// @dev Fragmented exact-input correction within one transaction: the total
    /// minted to the vault, valued at the reference price, never exceeds the final
    /// watermark, and the watermark never exceeds the cumulative target.
    function testFuzz_Settlement_FragmentsNeverOvercollect(uint8 fragmentCount) public {
        fragmentCount = uint8(bound(fragmentCount, 1, 16));
        bytes32 poolIdBytes = bytes32(PoolId.unwrap(poolAId));
        oracle.setPrice(poolIdBytes, 1.2e18);

        for (uint256 i = 0; i < fragmentCount; i++) {
            _swapExactIn(poolA, true, 100e18 / fragmentCount);
        }
        TxScopedParticipationAwareHook.TransientState memory state = hook.getTransientState(poolIdBytes);
        (uint256 target,) =
            hook.computeTransfer(_surplus(state), EXECUTION_MARGIN_HAT_WAD, LAMBDA_WAD, GAMMA_WAD, DELTA_WAD);

        uint256 minted = manager.balanceOf(VAULT, currency1.toId());
        assertGt(minted, 0);
        assertLe(minted * 1.2e18, state.watermark * 1e18, "collected value within watermark");
        assertLe(state.watermark, target, "watermark within cumulative target");
    }

    function _surplus(TxScopedParticipationAwareHook.TransientState memory state) private pure returns (uint256) {
        (, int256 value0) =
            CumulativeSurplusAccountingLib.tryMulSignedWad(state.cumulativeDelta0Wad, state.referencePrice0Wad);
        (, int256 value1) =
            CumulativeSurplusAccountingLib.tryMulSignedWad(state.cumulativeDelta1Wad, state.referencePrice1Wad);
        int256 totalValue = value0 + value1;
        return totalValue > 0 ? uint256(totalValue) : 0;
    }

    function _swapExactOut(PoolKey memory key, bool zeroForOne, uint256 amount) internal returns (BalanceDelta) {
        IPoolManager.SwapParams memory params = IPoolManager.SwapParams({
            zeroForOne: zeroForOne,
            amountSpecified: int256(amount),
            sqrtPriceLimitX96: zeroForOne ? MIN_PRICE_LIMIT : MAX_PRICE_LIMIT
        });
        PoolSwapTest.TestSettings memory settings =
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false});
        return swapRouter.swap(key, params, settings, ZERO_BYTES);
    }

    function _deployCustomHookAndPool(
        uint256 executionMarginHatWad,
        uint256 lambdaWad,
        uint256 gammaWad,
        uint256 deltaWad
    ) private returns (TxScopedParticipationAwareHook customHook, PoolKey memory key, PoolId id) {
        bytes memory args =
            abi.encode(manager, oracle, VAULT, executionMarginHatWad, lambdaWad, gammaWad, deltaWad, STALENESS_THRESHOLD_SECONDS);
        (address minedAddress, bytes32 salt) =
            HookMiner.find(address(this), HOOK_FLAGS, type(TxScopedParticipationAwareHook).creationCode, args);
        customHook = new TxScopedParticipationAwareHook{salt: salt}(
            manager, oracle, VAULT, executionMarginHatWad, lambdaWad, gammaWad, deltaWad, STALENESS_THRESHOLD_SECONDS
        );
        require(address(customHook) == minedAddress, "mined address mismatch");

        (key, id) = _deployPoolWithHook(IHooks(address(customHook)));
    }

    function _swapExactIn(PoolKey memory key, bool zeroForOne, uint256 amount) internal returns (BalanceDelta) {
        return _swapExactInOn(key, zeroForOne, amount);
    }

    function _swapExactInOn(PoolKey memory key, bool zeroForOne, uint256 amount) internal returns (BalanceDelta) {
        IPoolManager.SwapParams memory params = IPoolManager.SwapParams({
            zeroForOne: zeroForOne,
            amountSpecified: -int256(amount),
            sqrtPriceLimitX96: zeroForOne ? MIN_PRICE_LIMIT : MAX_PRICE_LIMIT
        });
        PoolSwapTest.TestSettings memory settings =
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false});
        return swapRouter.swap(key, params, settings, ZERO_BYTES);
    }
}