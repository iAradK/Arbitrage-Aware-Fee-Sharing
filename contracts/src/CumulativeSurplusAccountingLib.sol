// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Transient-storage plumbing for the transaction-scoped watermark rule.
/// @dev Pure bookkeeping only: slot derivation, TSTORE/TLOAD, checked arithmetic,
/// and status/watermark transitions. Validation errors are returned as status codes
/// rather than reverted here, so callers (a deployed accounting contract, or a v4
/// hook with its own fail-open policy) decide how to translate them. Callers remain
/// responsible for computing the locked transfer target via
/// SurplusSharingAccounting.computeTransfer, so that formula stays single-sourced.
/// Transient storage is scoped per contract address by the EVM itself (like regular
/// storage), so two different contracts using this library never collide on slots
/// even though they derive the same hashes for the same poolId.
library CumulativeSurplusAccountingLib {
    uint256 internal constant WAD = 1e18;

    uint256 internal constant STATUS_DISABLED = 1;
    uint256 internal constant STATUS_ENABLED = 2;

    bytes32 internal constant NAMESPACE =
        keccak256("participation-aware-surplus-sharing.transient.v1");
    bytes32 internal constant STATUS = keccak256("status");
    bytes32 internal constant DIRECTION = keccak256("direction");
    bytes32 internal constant REFERENCE_PRICE_0 = keccak256("referencePrice0");
    bytes32 internal constant REFERENCE_PRICE_1 = keccak256("referencePrice1");
    bytes32 internal constant EXECUTION_MARGIN = keccak256("executionMargin");
    bytes32 internal constant LAMBDA = keccak256("lambda");
    bytes32 internal constant GAMMA = keccak256("gamma");
    bytes32 internal constant DELTA = keccak256("delta");
    bytes32 internal constant CUMULATIVE_DELTA_0 = keccak256("cumulativeDelta0");
    bytes32 internal constant CUMULATIVE_DELTA_1 = keccak256("cumulativeDelta1");
    bytes32 internal constant WATERMARK = keccak256("watermark");

    enum InitResult {
        AlreadyInitialized,
        InvalidLambda,
        InvalidGamma,
        InvalidDirection,
        InvalidReferencePrice,
        DisabledNoDiscrepancy,
        Enabled
    }

    /// @dev Mirrors the validation and storage order of the original inline
    /// implementation exactly, so a caller that reverts on any non-Enabled,
    /// non-DisabledNoDiscrepancy result observes byte-for-byte identical behavior
    /// (transient writes made before an invalid result are rolled back when the
    /// caller reverts, matching the original single-function implementation).
    function initializePool(
        bytes32 poolId,
        uint256 referencePrice0Wad,
        uint256 referencePrice1Wad,
        uint256 executionMarginHat,
        uint256 lambdaWad,
        uint256 gammaWad,
        uint256 delta,
        int8 initialDirection
    ) internal returns (InitResult result) {
        bytes32 statusSlot = slot(poolId, STATUS);
        if (tload(statusSlot) != 0) return InitResult.AlreadyInitialized;

        if (lambdaWad > WAD) return InitResult.InvalidLambda;
        if (gammaWad >= WAD) return InitResult.InvalidGamma;
        if (initialDirection < -1 || initialDirection > 1) {
            return InitResult.InvalidDirection;
        }

        tstore(
            slot(poolId, DIRECTION),
            uint256(int256(initialDirection) + 2)
        );
        tstore(slot(poolId, EXECUTION_MARGIN), executionMarginHat);
        tstore(slot(poolId, LAMBDA), lambdaWad);
        tstore(slot(poolId, GAMMA), gammaWad);
        tstore(slot(poolId, DELTA), delta);

        if (initialDirection == 0) {
            tstore(statusSlot, STATUS_DISABLED);
            return InitResult.DisabledNoDiscrepancy;
        }
        if (referencePrice0Wad == 0 || referencePrice1Wad == 0) {
            return InitResult.InvalidReferencePrice;
        }

        tstore(slot(poolId, REFERENCE_PRICE_0), referencePrice0Wad);
        tstore(slot(poolId, REFERENCE_PRICE_1), referencePrice1Wad);
        tstore(statusSlot, STATUS_ENABLED);
        return InitResult.Enabled;
    }

    function status(bytes32 poolId) internal view returns (uint256) {
        return tload(slot(poolId, STATUS));
    }

    function watermark(bytes32 poolId) internal view returns (uint256) {
        return tload(slot(poolId, WATERMARK));
    }

    function disable(bytes32 poolId) internal {
        tstore(slot(poolId, STATUS), STATUS_DISABLED);
    }

    /// @dev Accumulates a baseline delta and returns the clamped-nonnegative
    /// cumulative surplus. ok = false on arithmetic failure; the caller should
    /// then call disable and treat the pool as fail-open for the rest of the tx.
    function accumulate(
        bytes32 poolId,
        int256 delta0Wad,
        int256 delta1Wad
    ) internal returns (bool ok, uint256 cumulativeSurplus) {
        bytes32 cumulative0Slot = slot(poolId, CUMULATIVE_DELTA_0);
        bytes32 cumulative1Slot = slot(poolId, CUMULATIVE_DELTA_1);
        (bool delta0Ok, int256 cumulativeDelta0) = tryAdd(
            tloadInt(cumulative0Slot),
            delta0Wad
        );
        (bool delta1Ok, int256 cumulativeDelta1) = tryAdd(
            tloadInt(cumulative1Slot),
            delta1Wad
        );
        if (!delta0Ok || !delta1Ok) return (false, 0);

        (bool value0Ok, int256 value0) = tryMulSignedWad(
            cumulativeDelta0,
            tload(slot(poolId, REFERENCE_PRICE_0))
        );
        (bool value1Ok, int256 value1) = tryMulSignedWad(
            cumulativeDelta1,
            tload(slot(poolId, REFERENCE_PRICE_1))
        );
        (bool sumOk, int256 totalValue) = tryAdd(value0, value1);
        if (!value0Ok || !value1Ok || !sumOk) return (false, 0);

        tstoreInt(cumulative0Slot, cumulativeDelta0);
        tstoreInt(cumulative1Slot, cumulativeDelta1);
        return (true, totalValue > 0 ? uint256(totalValue) : 0);
    }

    function loadLockedParams(
        bytes32 poolId
    )
        internal
        view
        returns (
            uint256 executionMarginHat,
            uint256 lambdaWad,
            uint256 gammaWad,
            uint256 delta
        )
    {
        executionMarginHat = tload(slot(poolId, EXECUTION_MARGIN));
        lambdaWad = tload(slot(poolId, LAMBDA));
        gammaWad = tload(slot(poolId, GAMMA));
        delta = tload(slot(poolId, DELTA));
    }

    /// @dev Raises the watermark to cumulativeTarget if it is higher than the
    /// previous watermark, and returns only the newly incurred charge.
    function updateWatermark(
        bytes32 poolId,
        uint256 cumulativeTarget
    ) internal returns (uint256 marginalCharge) {
        bytes32 watermarkSlot = slot(poolId, WATERMARK);
        uint256 previousWatermark = tload(watermarkSlot);
        if (cumulativeTarget > previousWatermark) {
            marginalCharge = cumulativeTarget - previousWatermark;
            tstore(watermarkSlot, cumulativeTarget);
        }
    }

    function loadState(
        bytes32 poolId
    )
        internal
        view
        returns (
            uint256 statusValue,
            int8 initialDirection,
            uint256 referencePrice0Wad,
            uint256 referencePrice1Wad,
            uint256 executionMarginHat,
            uint256 lambdaWad,
            uint256 gammaWad,
            uint256 delta,
            int256 cumulativeDelta0Wad,
            int256 cumulativeDelta1Wad,
            uint256 watermarkValue
        )
    {
        statusValue = tload(slot(poolId, STATUS));
        uint256 directionCode = tload(slot(poolId, DIRECTION));
        initialDirection = directionCode == 0
            ? int8(0)
            : int8(int256(directionCode) - 2);
        referencePrice0Wad = tload(slot(poolId, REFERENCE_PRICE_0));
        referencePrice1Wad = tload(slot(poolId, REFERENCE_PRICE_1));
        executionMarginHat = tload(slot(poolId, EXECUTION_MARGIN));
        lambdaWad = tload(slot(poolId, LAMBDA));
        gammaWad = tload(slot(poolId, GAMMA));
        delta = tload(slot(poolId, DELTA));
        cumulativeDelta0Wad = tloadInt(slot(poolId, CUMULATIVE_DELTA_0));
        cumulativeDelta1Wad = tloadInt(slot(poolId, CUMULATIVE_DELTA_1));
        watermarkValue = tload(slot(poolId, WATERMARK));
    }

    function tryMulSignedWad(
        int256 quantity,
        uint256 priceWad
    ) internal pure returns (bool ok, int256 value) {
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

    function tryAdd(
        int256 left,
        int256 right
    ) internal pure returns (bool ok, int256 result) {
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

    function slot(bytes32 poolId, bytes32 field) internal pure returns (bytes32) {
        return keccak256(abi.encodePacked(NAMESPACE, poolId, field));
    }

    function tload(bytes32 slotKey) internal view returns (uint256 value) {
        assembly ("memory-safe") {
            value := tload(slotKey)
        }
    }

    function tloadInt(bytes32 slotKey) internal view returns (int256 value) {
        assembly ("memory-safe") {
            value := tload(slotKey)
        }
    }

    function tstore(bytes32 slotKey, uint256 value) internal {
        assembly ("memory-safe") {
            tstore(slotKey, value)
        }
    }

    function tstoreInt(bytes32 slotKey, int256 value) internal {
        assembly ("memory-safe") {
            tstore(slotKey, value)
        }
    }
}