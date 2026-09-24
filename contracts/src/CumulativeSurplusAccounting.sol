// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {SurplusSharingAccounting} from "./SurplusSharingAccounting.sol";
import {CumulativeSurplusAccountingLib} from "./CumulativeSurplusAccountingLib.sol";

/// @notice Transaction-scoped accounting prototype for split-resistant surplus sharing.
/// @dev Inputs are two-token trader balance deltas and reference prices, all in WAD.
/// The contract stores a common-numeraire watermark. A production Uniswap v4 hook
/// should call initializePool from its first beforeSwap and processCallback from
/// afterSwap, before applying the hooks own accounting delta.
/// Transient-storage bookkeeping and checked arithmetic live in
/// CumulativeSurplusAccountingLib; this contract only owns validation-to-error
/// translation, event emission, and the locked-target computation (reused from
/// the inherited SurplusSharingAccounting.computeTransfer, single-sourced there).
contract CumulativeSurplusAccounting is SurplusSharingAccounting {
    struct TransientState {
        uint256 status;
        int8 initialDirection;
        uint256 referencePrice0Wad;
        uint256 referencePrice1Wad;
        uint256 executionMarginHat;
        uint256 lambdaWad;
        uint256 gammaWad;
        uint256 delta;
        int256 cumulativeDelta0Wad;
        int256 cumulativeDelta1Wad;
        uint256 watermark;
    }

    error InvalidReferencePrice();
    error InvalidDirection();
    error PoolNotInitialized();

    event PoolInitialized(
        bytes32 indexed poolId,
        int8 initialDirection,
        bool enabled
    );
    event PoolDisabled(bytes32 indexed poolId);
    event CumulativeCharge(
        bytes32 indexed poolId,
        uint256 cumulativeSurplus,
        uint256 cumulativeTarget,
        uint256 marginalCharge
    );

    /// @notice Lock transaction-scoped configuration for one pool.
    /// @param initialDirection -1 or +1 identifies the initial correction direction;
    /// zero records that no valid initial discrepancy exists and disables charging.
    /// @return initializedNow False when the pool was already initialized in this tx.
    function initializePool(
        bytes32 poolId,
        uint256 referencePrice0Wad,
        uint256 referencePrice1Wad,
        uint256 executionMarginHat,
        uint256 lambdaWad,
        uint256 gammaWad,
        uint256 delta,
        int8 initialDirection
    ) external returns (bool initializedNow) {
        CumulativeSurplusAccountingLib.InitResult result = CumulativeSurplusAccountingLib
            .initializePool(
                poolId,
                referencePrice0Wad,
                referencePrice1Wad,
                executionMarginHat,
                lambdaWad,
                gammaWad,
                delta,
                initialDirection
            );

        if (result == CumulativeSurplusAccountingLib.InitResult.AlreadyInitialized) {
            return false;
        }
        if (result == CumulativeSurplusAccountingLib.InitResult.InvalidLambda) {
            revert InvalidLambda();
        }
        if (result == CumulativeSurplusAccountingLib.InitResult.InvalidGamma) {
            revert InvalidGamma();
        }
        if (result == CumulativeSurplusAccountingLib.InitResult.InvalidDirection) {
            revert InvalidDirection();
        }
        if (result == CumulativeSurplusAccountingLib.InitResult.InvalidReferencePrice) {
            revert InvalidReferencePrice();
        }

        bool enabled = result == CumulativeSurplusAccountingLib.InitResult.Enabled;
        emit PoolInitialized(poolId, initialDirection, enabled);
        return true;
    }

    /// @notice Accumulate a baseline AMM delta and return only the new charge.
    /// @dev Positive delta entries are tokens received by the trader. Arithmetic
    /// failure disables additional charges for this pool without refunding the
    /// existing watermark; a production hook can use the same fail-open policy.
    function processCallback(
        bytes32 poolId,
        int256 delta0Wad,
        int256 delta1Wad
    ) external returns (
        uint256 cumulativeSurplus,
        uint256 cumulativeTarget,
        uint256 marginalCharge
    ) {
        uint256 poolStatus = CumulativeSurplusAccountingLib.status(poolId);
        if (poolStatus == 0) revert PoolNotInitialized();
        if (poolStatus == CumulativeSurplusAccountingLib.STATUS_DISABLED) {
            return (0, CumulativeSurplusAccountingLib.watermark(poolId), 0);
        }

        (bool arithmeticOk, uint256 surplus) = CumulativeSurplusAccountingLib.accumulate(
            poolId,
            delta0Wad,
            delta1Wad
        );
        if (!arithmeticOk) {
            CumulativeSurplusAccountingLib.disable(poolId);
            emit PoolDisabled(poolId);
            return (0, CumulativeSurplusAccountingLib.watermark(poolId), 0);
        }

        cumulativeSurplus = surplus;
        cumulativeTarget = _computeLockedTarget(poolId, cumulativeSurplus);
        marginalCharge = CumulativeSurplusAccountingLib.updateWatermark(
            poolId,
            cumulativeTarget
        );

        emit CumulativeCharge(
            poolId,
            cumulativeSurplus,
            cumulativeTarget,
            marginalCharge
        );
    }

    /// @notice Inspect the transient state during the current transaction.
    function getTransientState(
        bytes32 poolId
    ) external view returns (TransientState memory state) {
        (
            state.status,
            state.initialDirection,
            state.referencePrice0Wad,
            state.referencePrice1Wad,
            state.executionMarginHat,
            state.lambdaWad,
            state.gammaWad,
            state.delta,
            state.cumulativeDelta0Wad,
            state.cumulativeDelta1Wad,
            state.watermark
        ) = CumulativeSurplusAccountingLib.loadState(poolId);
    }

    /// @dev Isolating the parameter loads keeps processCallback below the
    /// legacy code generators stack limit without requiring via-IR.
    function _computeLockedTarget(
        bytes32 poolId,
        uint256 cumulativeSurplus
    ) private view returns (uint256 cumulativeTarget) {
        (
            uint256 executionMarginHat,
            uint256 lambdaWad,
            uint256 gammaWad,
            uint256 delta
        ) = CumulativeSurplusAccountingLib.loadLockedParams(poolId);
        (cumulativeTarget, ) = computeTransfer(
            cumulativeSurplus,
            executionMarginHat,
            lambdaWad,
            gammaWad,
            delta
        );
    }
}