// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {SurplusSharingAccounting} from "./SurplusSharingAccounting.sol";

/// @notice Transaction-scoped accounting prototype for split-resistant surplus sharing.
/// @dev Inputs are two-token trader balance deltas and reference prices, all in WAD.
/// The contract stores a common-numeraire watermark. A production Uniswap v4 hook
/// should call initializePool from its first beforeSwap and processCallback from
/// afterSwap, before applying the hook's own accounting delta.
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

    uint256 private constant _STATUS_DISABLED = 1;
    uint256 private constant _STATUS_ENABLED = 2;

    bytes32 private constant _NAMESPACE =
        keccak256("participation-aware-surplus-sharing.transient.v1");
    bytes32 private constant _STATUS = keccak256("status");
    bytes32 private constant _DIRECTION = keccak256("direction");
    bytes32 private constant _REFERENCE_PRICE_0 = keccak256("referencePrice0");
    bytes32 private constant _REFERENCE_PRICE_1 = keccak256("referencePrice1");
    bytes32 private constant _EXECUTION_MARGIN = keccak256("executionMargin");
    bytes32 private constant _LAMBDA = keccak256("lambda");
    bytes32 private constant _GAMMA = keccak256("gamma");
    bytes32 private constant _DELTA = keccak256("delta");
    bytes32 private constant _CUMULATIVE_DELTA_0 = keccak256("cumulativeDelta0");
    bytes32 private constant _CUMULATIVE_DELTA_1 = keccak256("cumulativeDelta1");
    bytes32 private constant _WATERMARK = keccak256("watermark");

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
        bytes32 statusSlot = _slot(poolId, _STATUS);
        if (_tload(statusSlot) != 0) return false;

        if (lambdaWad > WAD) revert InvalidLambda();
        if (gammaWad >= WAD) revert InvalidGamma();
        if (initialDirection < -1 || initialDirection > 1) {
            revert InvalidDirection();
        }

        _tstore(
            _slot(poolId, _DIRECTION),
            uint256(int256(initialDirection) + 2)
        );
        _tstore(_slot(poolId, _EXECUTION_MARGIN), executionMarginHat);
        _tstore(_slot(poolId, _LAMBDA), lambdaWad);
        _tstore(_slot(poolId, _GAMMA), gammaWad);
        _tstore(_slot(poolId, _DELTA), delta);

        if (initialDirection == 0) {
            _tstore(statusSlot, _STATUS_DISABLED);
            emit PoolInitialized(poolId, initialDirection, false);
            return true;
        }
        if (referencePrice0Wad == 0 || referencePrice1Wad == 0) {
            revert InvalidReferencePrice();
        }

        _tstore(_slot(poolId, _REFERENCE_PRICE_0), referencePrice0Wad);
        _tstore(_slot(poolId, _REFERENCE_PRICE_1), referencePrice1Wad);
        _tstore(statusSlot, _STATUS_ENABLED);
        emit PoolInitialized(poolId, initialDirection, true);
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
        bytes32 statusSlot = _slot(poolId, _STATUS);
        uint256 status = _tload(statusSlot);
        if (status == 0) revert PoolNotInitialized();
        if (status == _STATUS_DISABLED) {
            return (0, _tload(_slot(poolId, _WATERMARK)), 0);
        }

        (bool arithmeticOk, int256 signedSurplus) = _accumulateAndValue(
            poolId,
            delta0Wad,
            delta1Wad
        );
        if (!arithmeticOk) {
            return _disable(poolId, statusSlot);
        }

        cumulativeSurplus = signedSurplus > 0
            ? uint256(signedSurplus)
            : 0;
        cumulativeTarget = _computeLockedTarget(poolId, cumulativeSurplus);

        bytes32 watermarkSlot = _slot(poolId, _WATERMARK);
        uint256 previousWatermark = _tload(watermarkSlot);
        if (cumulativeTarget > previousWatermark) {
            marginalCharge = cumulativeTarget - previousWatermark;
            _tstore(watermarkSlot, cumulativeTarget);
        }

        emit CumulativeCharge(
            poolId,
            cumulativeSurplus,
            cumulativeTarget,
            marginalCharge
        );
    }

    /// @dev Isolating the parameter loads keeps processCallback below the
    /// legacy code generator's stack limit without requiring via-IR.
    function _computeLockedTarget(
        bytes32 poolId,
        uint256 cumulativeSurplus
    ) private view returns (uint256 cumulativeTarget) {
        (cumulativeTarget,) = computeTransfer(
            cumulativeSurplus,
            _tload(_slot(poolId, _EXECUTION_MARGIN)),
            _tload(_slot(poolId, _LAMBDA)),
            _tload(_slot(poolId, _GAMMA)),
            _tload(_slot(poolId, _DELTA))
        );
    }

    function _accumulateAndValue(
        bytes32 poolId,
        int256 delta0Wad,
        int256 delta1Wad
    ) private returns (bool ok, int256 signedSurplus) {
        bytes32 cumulative0Slot = _slot(poolId, _CUMULATIVE_DELTA_0);
        bytes32 cumulative1Slot = _slot(poolId, _CUMULATIVE_DELTA_1);
        (bool delta0Ok, int256 cumulativeDelta0) = _tryAdd(
            _tloadInt(cumulative0Slot),
            delta0Wad
        );
        (bool delta1Ok, int256 cumulativeDelta1) = _tryAdd(
            _tloadInt(cumulative1Slot),
            delta1Wad
        );
        if (!delta0Ok || !delta1Ok) return (false, 0);

        (bool value0Ok, int256 value0) = _tryMulSignedWad(
            cumulativeDelta0,
            _tload(_slot(poolId, _REFERENCE_PRICE_0))
        );
        (bool value1Ok, int256 value1) = _tryMulSignedWad(
            cumulativeDelta1,
            _tload(_slot(poolId, _REFERENCE_PRICE_1))
        );
        (bool sumOk, int256 totalValue) = _tryAdd(value0, value1);
        if (!value0Ok || !value1Ok || !sumOk) return (false, 0);

        _tstoreInt(cumulative0Slot, cumulativeDelta0);
        _tstoreInt(cumulative1Slot, cumulativeDelta1);
        return (true, totalValue);
    }

    /// @notice Inspect the transient state during the current transaction.
    function getTransientState(
        bytes32 poolId
    ) external view returns (TransientState memory state) {
        state.status = _tload(_slot(poolId, _STATUS));
        uint256 directionCode = _tload(_slot(poolId, _DIRECTION));
        state.initialDirection = directionCode == 0
            ? int8(0)
            : int8(int256(directionCode) - 2);
        state.referencePrice0Wad = _tload(
            _slot(poolId, _REFERENCE_PRICE_0)
        );
        state.referencePrice1Wad = _tload(
            _slot(poolId, _REFERENCE_PRICE_1)
        );
        state.executionMarginHat = _tload(
            _slot(poolId, _EXECUTION_MARGIN)
        );
        state.lambdaWad = _tload(_slot(poolId, _LAMBDA));
        state.gammaWad = _tload(_slot(poolId, _GAMMA));
        state.delta = _tload(_slot(poolId, _DELTA));
        state.cumulativeDelta0Wad = _tloadInt(
            _slot(poolId, _CUMULATIVE_DELTA_0)
        );
        state.cumulativeDelta1Wad = _tloadInt(
            _slot(poolId, _CUMULATIVE_DELTA_1)
        );
        state.watermark = _tload(_slot(poolId, _WATERMARK));
    }

    function _disable(
        bytes32 poolId,
        bytes32 statusSlot
    ) private returns (uint256, uint256, uint256) {
        _tstore(statusSlot, _STATUS_DISABLED);
        emit PoolDisabled(poolId);
        return (0, _tload(_slot(poolId, _WATERMARK)), 0);
    }

    function _tryMulSignedWad(
        int256 quantity,
        uint256 priceWad
    ) private pure returns (bool ok, int256 value) {
        bool negative = quantity < 0;
        uint256 magnitude;
        if (negative) {
            unchecked {
                magnitude = uint256(-(quantity + 1)) + 1;
            }
        } else {
            magnitude = uint256(quantity);
        }

        if (priceWad != 0 && magnitude > type(uint256).max / priceWad) {
            return (false, 0);
        }
        uint256 scaled = (magnitude * priceWad) / WAD;

        if (!negative) {
            if (scaled > uint256(type(int256).max)) return (false, 0);
            return (true, int256(scaled));
        }

        uint256 minMagnitude = uint256(1) << 255;
        if (scaled > minMagnitude) return (false, 0);
        if (scaled == minMagnitude) return (true, type(int256).min);
        return (true, -int256(scaled));
    }

    function _tryAdd(
        int256 left,
        int256 right
    ) private pure returns (bool ok, int256 result) {
        unchecked {
            if (right > 0 && left > type(int256).max - right) {
                return (false, 0);
            }
            if (right < 0 && left < type(int256).min - right) {
                return (false, 0);
            }
            return (true, left + right);
        }
    }

    function _slot(
        bytes32 poolId,
        bytes32 field
    ) private pure returns (bytes32) {
        return keccak256(abi.encodePacked(_NAMESPACE, poolId, field));
    }

    function _tload(bytes32 slot) private view returns (uint256 value) {
        assembly ("memory-safe") {
            value := tload(slot)
        }
    }

    function _tloadInt(bytes32 slot) private view returns (int256 value) {
        assembly ("memory-safe") {
            value := tload(slot)
        }
    }

    function _tstore(bytes32 slot, uint256 value) private {
        assembly ("memory-safe") {
            tstore(slot, value)
        }
    }

    function _tstoreInt(bytes32 slot, int256 value) private {
        assembly ("memory-safe") {
            tstore(slot, value)
        }
    }
}
