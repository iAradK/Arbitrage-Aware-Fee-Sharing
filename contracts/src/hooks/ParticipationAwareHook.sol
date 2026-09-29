// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "v4-core/src/types/PoolId.sol";
import {BalanceDelta} from "v4-core/src/types/BalanceDelta.sol";
import {BeforeSwapDelta, BeforeSwapDeltaLibrary} from "v4-core/src/types/BeforeSwapDelta.sol";
import {Currency} from "v4-core/src/types/Currency.sol";

import {MinimalBaseHook} from "./MinimalBaseHook.sol";
import {SurplusSharingAccounting} from "../SurplusSharingAccounting.sol";
import {CumulativeSurplusAccountingLib} from "../CumulativeSurplusAccountingLib.sol";
import {IReferenceOracle} from "../interfaces/IReferenceOracle.sol";

/// @notice Prototype v4 hook wiring the participation-aware surplus-sharing rule into
/// real beforeSwap/afterSwap callbacks, so its gas cost includes oracle access,
/// transient-storage tracking, callback routing, and settlement rather than only the
/// arithmetic core.
///
/// Settlement: each fragment's marginal charge is converted into the swap's
/// unspecified currency at the locked reference price (rounded down) and returned as
/// the afterSwap return delta, so it is taken from the swapper's output (exact input)
/// or added to the swapper's input (exact output). The hook resolves the resulting
/// credit by minting ERC-6909 claims on the PoolManager to the LP-protection vault.
/// How the vault distributes those claims to LPs is out of scope. The hook address
/// must carry the AFTER_SWAP_RETURNS_DELTA permission flag.
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

    error InvalidVault();

    event OracleRejected(bytes32 indexed poolId, string reason);
    event HookCharge(bytes32 indexed poolId, uint256 cumulativeSurplus, uint256 cumulativeTarget, uint256 marginalCharge);
    event HookSettled(bytes32 indexed poolId, address currency, uint256 amount);

    IReferenceOracle public immutable oracle;
    address public immutable vault;
    uint256 public immutable executionMarginHatWad;
    uint256 public immutable lambdaWad;
    uint256 public immutable gammaWad;
    uint256 public immutable deltaWad;
    uint256 public immutable stalenessThresholdSeconds;

    constructor(
        IPoolManager _poolManager,
        IReferenceOracle _oracle,
        address _vault,
        uint256 _executionMarginHatWad,
        uint256 _lambdaWad,
        uint256 _gammaWad,
        uint256 _deltaWad,
        uint256 _stalenessThresholdSeconds
    ) MinimalBaseHook(_poolManager) {
        if (_lambdaWad > WAD) revert InvalidLambda();
        if (_gammaWad >= WAD) revert InvalidGamma();
        if (_vault == address(0)) revert InvalidVault();
        oracle = _oracle;
        vault = _vault;
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
        IPoolManager.SwapParams calldata params,
        BalanceDelta delta,
        bytes calldata
    ) external override onlyPoolManager returns (bytes4, int128) {
        bytes32 poolId = PoolId.unwrap(key.toId());
        int128 hookDeltaUnspecified;
        if (CumulativeSurplusAccountingLib.status(poolId) == CumulativeSurplusAccountingLib.STATUS_ENABLED) {
            hookDeltaUnspecified = _accumulateAndCharge(poolId, key, params, delta);
        }
        return (this.afterSwap.selector, hookDeltaUnspecified);
    }

    function _initializePoolFromOracle(bytes32 poolId, bool zeroForOne) private {
        uint256 priceWad;
        uint256 updatedAt;
        bool valid;
        // A reverting oracle must not revert the base swap: treat it like any other
        // rejected observation and fail open for the rest of the transaction.
        try oracle.latestPrice(poolId) returns (uint256 p, uint256 u, bool v) {
            (priceWad, updatedAt, valid) = (p, u, v);
        } catch {
            _disablePool(poolId);
            emit OracleRejected(poolId, "oracle-reverted");
            return;
        }

        bool fresh = updatedAt != 0 && updatedAt <= block.timestamp
            && (block.timestamp - updatedAt) <= stalenessThresholdSeconds;
        bool oracleOk = valid && priceWad != 0 && fresh;

        if (!oracleOk) {
            _disablePool(poolId);
            emit OracleRejected(poolId, _rejectReason(valid, priceWad, updatedAt));
            return;
        }

        int8 initialDirection = zeroForOne ? int8(1) : int8(-1);
        CumulativeSurplusAccountingLib.initializePool(
            poolId, WAD, priceWad, executionMarginHatWad, lambdaWad, gammaWad, deltaWad, initialDirection
        );
    }

    function _disablePool(bytes32 poolId) private {
        CumulativeSurplusAccountingLib.initializePool(
            poolId, 0, 0, executionMarginHatWad, lambdaWad, gammaWad, deltaWad, 0
        );
    }

    /// @dev Returns the hook's delta in the swap's unspecified currency (positive =
    /// taken from the swapper). The delta passed in is the core swap delta, before
    /// this return value is applied, so the charge never feeds back into the surplus.
    function _accumulateAndCharge(
        bytes32 poolId,
        PoolKey calldata key,
        IPoolManager.SwapParams calldata params,
        BalanceDelta delta
    ) private returns (int128) {
        (bool ok, uint256 surplus) =
            CumulativeSurplusAccountingLib.accumulate(poolId, int256(delta.amount0()), int256(delta.amount1()));
        if (!ok) return _failOpen(poolId);

        (uint256 executionMarginHat, uint256 lambdaWadLoaded, uint256 gammaWadLoaded, uint256 deltaLoaded) =
            CumulativeSurplusAccountingLib.loadLockedParams(poolId);
        (uint256 cumulativeTarget,) = computeTransfer(surplus, executionMarginHat, lambdaWadLoaded, gammaWadLoaded, deltaLoaded);

        uint256 previousWatermark = CumulativeSurplusAccountingLib.watermark(poolId);
        if (cumulativeTarget <= previousWatermark) {
            emit HookCharge(poolId, surplus, cumulativeTarget, 0);
            return 0;
        }

        // Exact input (amountSpecified < 0) charges the output token, exact output
        // charges the input token: in both cases the unspecified currency.
        bool unspecifiedIs0 = (params.amountSpecified < 0) != params.zeroForOne;
        (bool convertOk, uint256 tokenAmount, uint256 collectedValue) = _toSettlementAmount(
            cumulativeTarget - previousWatermark, CumulativeSurplusAccountingLib.referencePrice(poolId, unspecifiedIs0)
        );
        if (!convertOk) return _failOpen(poolId);

        // The watermark advances by the value actually collected, so the rounding
        // remainder stays owed and is picked up by the next fragment's charge.
        uint256 marginalCharge = CumulativeSurplusAccountingLib.updateWatermark(poolId, previousWatermark + collectedValue);
        emit HookCharge(poolId, surplus, cumulativeTarget, marginalCharge);
        if (tokenAmount == 0) return 0;

        // The positive return delta credits the hook with tokenAmount. Minting ERC-6909
        // claims to the vault debits the hook by the same amount, so the hook's net
        // delta is zero and nothing is transferred out of the PoolManager mid-swap.
        Currency currency = unspecifiedIs0 ? key.currency0 : key.currency1;
        poolManager.mint(vault, currency.toId(), tokenAmount);
        emit HookSettled(poolId, Currency.unwrap(currency), tokenAmount);
        return int128(int256(tokenAmount));
    }

    /// @dev Converts a numeraire charge into base units of the unspecified currency,
    /// rounding down so the collected value never exceeds the charge. collectedValue
    /// is that token amount valued back in the numeraire, rounded up, so the
    /// watermark never records less than was taken. ok = false when the charge
    /// cannot be represented as an int128 return delta.
    function _toSettlementAmount(uint256 marginalCharge, uint256 referencePriceWad)
        private
        pure
        returns (bool ok, uint256 tokenAmount, uint256 collectedValue)
    {
        if (marginalCharge > type(uint256).max / WAD) return (false, 0, 0);
        tokenAmount = (marginalCharge * WAD) / referencePriceWad;
        if (tokenAmount > uint256(uint128(type(int128).max))) return (false, 0, 0);
        uint256 scaled = tokenAmount * referencePriceWad;
        collectedValue = scaled / WAD + (scaled % WAD == 0 ? 0 : 1);
        ok = true;
    }

    function _failOpen(bytes32 poolId) private returns (int128) {
        CumulativeSurplusAccountingLib.disable(poolId);
        emit HookCharge(poolId, 0, CumulativeSurplusAccountingLib.watermark(poolId), 0);
        return 0;
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