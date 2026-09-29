// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "v4-core/src/types/PoolId.sol";
import {BalanceDelta} from "v4-core/src/types/BalanceDelta.sol";
import {BeforeSwapDelta, BeforeSwapDeltaLibrary} from "v4-core/src/types/BeforeSwapDelta.sol";

import {MinimalBaseHook} from "./MinimalBaseHook.sol";
import {SurplusSharingAccounting} from "../SurplusSharingAccounting.sol";
import {CumulativeSurplusAccountingLib} from "../CumulativeSurplusAccountingLib.sol";
import {IReferenceOracle} from "../interfaces/IReferenceOracle.sol";

/// @notice Prototype v4 hook wiring the participation-aware surplus-sharing rule into
/// real beforeSwap/afterSwap callbacks, so its gas cost includes oracle access,
/// transient-storage tracking, and callback routing rather than only the arithmetic
/// core. Settlement (transferring marginalCharge to LPs) is intentionally out of
/// scope, matching the accompanying paper section: this hook only measures and
/// records the charge that a production integration would still need to collect.
///
/// Direction mapping: the correction direction locked for a pool at the first swap
/// of a transaction is taken directly from that swap's own zeroForOne flag
/// (zeroForOne => +1, oneForZero => -1). This is a deliberate simplification (not a
/// price-discrepancy computation against the AMM's own sqrtPriceX96) chosen to avoid
/// a 512-bit price-squaring step on every swap; ParticipationAwareHook.t.sol has a
/// dedicated test asserting this mapping.
///
/// Reference prices: the oracle reports a single priceWad (token1 priced in token0),
/// so token0 is treated as the numeraire (referencePrice0Wad = WAD) and
/// referencePrice1Wad = priceWad.
contract ParticipationAwareHook is MinimalBaseHook, SurplusSharingAccounting {
    using PoolIdLibrary for PoolKey;

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

    event OracleRejected(bytes32 indexed poolId, string reason);
    event HookCharge(bytes32 indexed poolId, uint256 cumulativeSurplus, uint256 cumulativeTarget, uint256 marginalCharge);

    IReferenceOracle public immutable oracle;
    uint256 public immutable executionMarginHatWad;
    uint256 public immutable lambdaWad;
    uint256 public immutable gammaWad;
    uint256 public immutable deltaWad;
    uint256 public immutable stalenessThresholdSeconds;

    constructor(
        IPoolManager _poolManager,
        IReferenceOracle _oracle,
        uint256 _executionMarginHatWad,
        uint256 _lambdaWad,
        uint256 _gammaWad,
        uint256 _deltaWad,
        uint256 _stalenessThresholdSeconds
    ) MinimalBaseHook(_poolManager) {
        if (_lambdaWad > WAD) revert InvalidLambda();
        if (_gammaWad >= WAD) revert InvalidGamma();
        oracle = _oracle;
        executionMarginHatWad = _executionMarginHatWad;
        lambdaWad = _lambdaWad;
        gammaWad = _gammaWad;
        deltaWad = _deltaWad;
        stalenessThresholdSeconds = _stalenessThresholdSeconds;
    }

    function beforeSwap(address, PoolKey calldata key, IPoolManager.SwapParams calldata params, bytes calldata)
        external
        override
        onlyPoolManager
        returns (bytes4, BeforeSwapDelta, uint24)
    {
        bytes32 poolId = PoolId.unwrap(key.toId());
        if (CumulativeSurplusAccountingLib.status(poolId) == 0) {
            _initializePoolFromOracle(poolId, params.zeroForOne);
        }
        return (this.beforeSwap.selector, BeforeSwapDeltaLibrary.ZERO_DELTA, 0);
    }

    function afterSwap(
        address,
        PoolKey calldata key,
        IPoolManager.SwapParams calldata,
        BalanceDelta delta,
        bytes calldata
    ) external override onlyPoolManager returns (bytes4, int128) {
        bytes32 poolId = PoolId.unwrap(key.toId());
        if (CumulativeSurplusAccountingLib.status(poolId) == CumulativeSurplusAccountingLib.STATUS_ENABLED) {
            _accumulateAndCharge(poolId, delta);
        }
        return (this.afterSwap.selector, 0);
    }

    function _initializePoolFromOracle(bytes32 poolId, bool zeroForOne) private {
        (uint256 priceWad, uint256 updatedAt, bool valid) = oracle.latestPrice(poolId);

        bool fresh = updatedAt != 0 && updatedAt <= block.timestamp
            && (block.timestamp - updatedAt) <= stalenessThresholdSeconds;
        bool oracleOk = valid && priceWad != 0 && fresh;

        if (!oracleOk) {
            CumulativeSurplusAccountingLib.initializePool(
                poolId, 0, 0, executionMarginHatWad, lambdaWad, gammaWad, deltaWad, 0
            );
            emit OracleRejected(poolId, _rejectReason(valid, priceWad, updatedAt));
            return;
        }

        int8 initialDirection = zeroForOne ? int8(1) : int8(-1);
        CumulativeSurplusAccountingLib.initializePool(
            poolId, WAD, priceWad, executionMarginHatWad, lambdaWad, gammaWad, deltaWad, initialDirection
        );
    }

    function _accumulateAndCharge(bytes32 poolId, BalanceDelta delta) private {
        int256 delta0Wad = int256(delta.amount0());
        int256 delta1Wad = int256(delta.amount1());

        (bool ok, uint256 surplus) = CumulativeSurplusAccountingLib.accumulate(poolId, delta0Wad, delta1Wad);
        if (!ok) {
            CumulativeSurplusAccountingLib.disable(poolId);
            emit HookCharge(poolId, 0, CumulativeSurplusAccountingLib.watermark(poolId), 0);
            return;
        }

        (uint256 executionMarginHat, uint256 lambdaWadLoaded, uint256 gammaWadLoaded, uint256 deltaLoaded) =
            CumulativeSurplusAccountingLib.loadLockedParams(poolId);
        (uint256 cumulativeTarget,) = computeTransfer(surplus, executionMarginHat, lambdaWadLoaded, gammaWadLoaded, deltaLoaded);
        uint256 marginalCharge = CumulativeSurplusAccountingLib.updateWatermark(poolId, cumulativeTarget);

        emit HookCharge(poolId, surplus, cumulativeTarget, marginalCharge);
    }

    /// @notice Inspect the transient state during the current transaction. Testing/
    /// diagnostic use only; a production deployment would not need this surface.
    function getTransientState(bytes32 poolId) external view returns (TransientState memory state) {
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

    function _rejectReason(bool valid, uint256 priceWad, uint256 updatedAt) private pure returns (string memory) {
        if (!valid) return "self-reported-invalid";
        if (priceWad == 0) return "zero-price";
        if (updatedAt == 0) return "incomplete-round";
        return "stale";
    }
}