// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "v4-core/src/types/PoolId.sol";
import {BalanceDelta} from "v4-core/src/types/BalanceDelta.sol";
import {Currency} from "v4-core/src/types/Currency.sol";
import {FullMath} from "v4-core/src/libraries/FullMath.sol";

import {MinimalBaseHook} from "./MinimalBaseHook.sol";
import {SurplusSharingAccounting} from "../SurplusSharingAccounting.sol";
import {IReferenceOracle} from "../interfaces/IReferenceOracle.sol";

/// @notice Uniswap v4 hook for participation-aware surplus sharing with block-scoped
/// accounting. All logic runs in afterSwap (permissions: AFTER_SWAP,
/// AFTER_SWAP_RETURNS_DELTA); a variant that opened the scope in beforeSwap cost 2.5k to
/// 3.2k gas more per swap and was dropped. The oracle is read in the afterSwap of the
/// scope's first swap: the reference is external to the pool, so reading it after the
/// core swap gives the value a beforeSwap read would.
///
/// Scope: the cumulative accounting of Definition 3 (watermark) covers all swaps of one
/// pool within one block, across transactions and senders. The state is persistent and
/// keyed by PoolId, in three packed slots:
///   A: uint64 block | uint128 watermark W | uint16 lambdaBps | uint16 gammaBps
///   B: int128 cumulativeDelta0 | int128 cumulativeDelta1
///   C: uint128 referencePriceWad | uint128 kappa
/// A scope opens on the first swap of a block (A.block != block.number) whose oracle
/// read is valid; reference, kappa, lambda and gamma are locked then. If the oracle is
/// invalid no scope opens, the swap is charged nothing and is not accumulated, and the
/// next swap retries. Opening a scope never zeroes the old slots: the first swap of a
/// fresh scope writes B = its own delta and A = (block, W, lambda, gamma) over the
/// previous block's values, so every write is nonzero to nonzero. After that, A is
/// written only when W changes.
///
/// kappa = K_const + delta + g_hat * (basefee + tau_hat) * p(ETH -> token0), with
/// p = gasPriceToken0Wad, or the locked reference when that is 0 (token1 is the gas
/// token). It is clamped to uint128 (a larger kappa only lowers the charge).
///
/// Bit widths: the reference must fit uint128 (oracle prices above it are rejected),
/// so cumulativeDelta1 * reference fits int256 and the surplus cannot overflow. A
/// cumulative delta outside int128, a target above uint128 or a charge that does not
/// fit the int128 return delta saturates W (W = type(uint128).max), which disables
/// further charges on the pool for the rest of the block (fail open).
///
/// Settlement is unchanged from the transaction-scoped hook: each marginal charge is
/// converted into the swap's unspecified currency at the locked reference (rounded
/// down), returned as the afterSwap delta and minted as ERC-6909 claims to the vault;
/// W advances by the value actually collected (rounded up).
contract ParticipationAwareHook is MinimalBaseHook, SurplusSharingAccounting {
    using PoolIdLibrary for PoolKey;

    struct Params {
        uint256 kHatConstant; // B + C_hedge + C_slippage + R, token0 base units
        uint256 delta; // buffer, token0 base units
        uint64 gasUnits; // g_hat
        uint64 priorityFeeWei; // tau_hat
        uint128 gasPriceToken0Wad; // token0 base units per wei, WAD; 0 = use the reference
        uint16 lambdaBps;
        uint16 gammaBps;
        uint32 stalenessThresholdSeconds;
    }

    struct Scope {
        uint64 blockNumber;
        uint128 watermark;
        uint16 lambdaBps;
        uint16 gammaBps;
        int128 cumulativeDelta0;
        int128 cumulativeDelta1;
        uint128 referencePriceWad;
        uint128 kappa;
    }

    struct Slots {
        uint256 a;
        uint256 b;
        uint256 c;
    }

    uint256 internal constant BPS = 10_000;
    uint256 internal constant BPS_TO_WAD = 1e14;
    uint256 internal constant SATURATED = type(uint128).max;
    uint256 internal constant W_MASK = SATURATED << 64;

    uint8 internal constant ORACLE_OK = 0;
    uint8 internal constant ORACLE_REVERTED = 1;
    uint8 internal constant ORACLE_SELF_INVALID = 2;
    uint8 internal constant ORACLE_ZERO_PRICE = 3;
    uint8 internal constant ORACLE_INCOMPLETE_ROUND = 4;
    uint8 internal constant ORACLE_STALE = 5;
    uint8 internal constant ORACLE_OUT_OF_RANGE = 6;

    error InvalidVault();

    event OracleRejected(bytes32 indexed poolId, string reason);
    event HookCharge(bytes32 indexed poolId, uint256 cumulativeSurplus, uint256 cumulativeTarget, uint256 marginalCharge);
    event HookSettled(bytes32 indexed poolId, address currency, uint256 amount);

    IReferenceOracle public immutable oracle;
    address public immutable vault;
    /// @notice min(K_const + delta, 2^128 - 1): the part of kappa that does not depend on gas.
    uint256 public immutable kappaConstant;
    uint64 public immutable gasUnits;
    uint64 public immutable priorityFeeWei;
    uint128 public immutable gasPriceToken0Wad;
    uint16 public immutable lambdaBps;
    uint16 public immutable gammaBps;
    uint256 public immutable stalenessThresholdSeconds;

    mapping(PoolId => Slots) internal _scopes;

    constructor(IPoolManager _poolManager, IReferenceOracle _oracle, address _vault, Params memory p)
        MinimalBaseHook(_poolManager)
    {
        if (p.lambdaBps > BPS) revert InvalidLambda();
        if (p.gammaBps >= BPS) revert InvalidGamma();
        if (_vault == address(0)) revert InvalidVault();
        oracle = _oracle;
        vault = _vault;
        uint256 k = p.kHatConstant >= SATURATED || p.delta >= SATURATED ? SATURATED : p.kHatConstant + p.delta;
        kappaConstant = k > SATURATED ? SATURATED : k;
        gasUnits = p.gasUnits;
        priorityFeeWei = p.priorityFeeWei;
        gasPriceToken0Wad = p.gasPriceToken0Wad;
        lambdaBps = p.lambdaBps;
        gammaBps = p.gammaBps;
        stalenessThresholdSeconds = p.stalenessThresholdSeconds;
    }

    function afterSwap(
        address,
        PoolKey calldata key,
        IPoolManager.SwapParams calldata params,
        BalanceDelta delta,
        bytes calldata
    ) external override onlyPoolManager returns (bytes4, int128) {
        PoolId id = key.toId();
        Slots storage s = _scopes[id];
        uint256 a = s.a;
        uint256 c;
        bool fresh = uint64(a) != block.number;
        if (fresh) {
            bool ok;
            (ok, c) = _openScope(id, s);
            if (!ok) return (this.afterSwap.selector, 0);
            a = _freshA();
        } else {
            if ((a & W_MASK) == W_MASK) return (this.afterSwap.selector, 0);
            c = s.c;
        }
        return (this.afterSwap.selector, _accumulateAndCharge(id, s, key, params, delta, a, c, fresh));
    }

    /// @notice The charge, in token0 base units, that a swap with core balance delta
    /// (delta0, delta1) would raise the watermark by if it executed now (positive =
    /// received by the swapper, as in afterSwap). In a block without an open scope it
    /// reads the oracle and the base fee as the opening swap would. Returns 0 wherever
    /// the hook would fail open. The settled amount can be lower by the rounding of the
    /// conversion into the unspecified token, whose remainder stays owed.
    function quote(PoolKey calldata key, int256 delta0, int256 delta1) external view returns (uint256) {
        PoolId id = key.toId();
        Slots storage s = _scopes[id];
        uint256 a = s.a;
        uint256 c;
        if (delta0 != int128(delta0) || delta1 != int128(delta1)) return 0;
        if (uint64(a) == block.number) {
            if ((a & W_MASK) == W_MASK) return 0;
            c = s.c;
            uint256 b = s.b;
            delta0 += int128(uint128(b));
            delta1 += int128(uint128(b >> 128));
            if (delta0 != int128(delta0) || delta1 != int128(delta1)) return 0;
        } else {
            (uint8 code, uint256 price) = _observe(id);
            if (code != ORACLE_OK) return 0;
            c = price | (_kappa(price) << 128);
            a = _freshA();
        }
        uint256 target = _target(_surplus(delta0, delta1, uint128(c)), a, c);
        if (target > SATURATED) return 0;
        uint256 w = uint128(a >> 64);
        return target > w ? target - w : 0;
    }

    /// @notice Decoded scope of a pool (testing and diagnostics).
    function getScope(PoolId id) external view returns (Scope memory scope) {
        Slots storage s = _scopes[id];
        (uint256 a, uint256 b, uint256 c) = (s.a, s.b, s.c);
        scope.blockNumber = uint64(a);
        scope.watermark = uint128(a >> 64);
        scope.lambdaBps = uint16(a >> 192);
        scope.gammaBps = uint16(a >> 208);
        scope.cumulativeDelta0 = int128(uint128(b));
        scope.cumulativeDelta1 = int128(uint128(b >> 128));
        scope.referencePriceWad = uint128(c);
        scope.kappa = uint128(c >> 128);
    }

    /// @notice kappa that a scope opened now would lock, given a reference price.
    function currentKappa(uint256 referencePriceWad) external view returns (uint256) {
        return _kappa(referencePriceWad);
    }

    /// @dev Reads the oracle; on success writes slot C and returns it. A rejected read
    /// opens no scope and leaves every slot untouched.
    function _openScope(PoolId id, Slots storage s) internal returns (bool ok, uint256 c) {
        (uint8 code, uint256 price) = _observe(id);
        if (code != ORACLE_OK) {
            emit OracleRejected(PoolId.unwrap(id), _reason(code));
            return (false, 0);
        }
        c = price | (_kappa(price) << 128);
        s.c = c;
        ok = true;
    }

    /// @dev A for a scope opened in this block: W = 0 and the current lambda and gamma.
    function _freshA() internal view returns (uint256) {
        return uint256(uint64(block.number)) | (uint256(lambdaBps) << 192) | (uint256(gammaBps) << 208);
    }

    /// @dev Accumulates the swap's core delta into B, raises W and settles the marginal
    /// charge. `fresh` = first swap of the scope: B and A in storage still hold an earlier
    /// block's values, so B is overwritten with this delta and A is written even if W
    /// stays 0.
    function _accumulateAndCharge(
        PoolId id,
        Slots storage s,
        PoolKey calldata key,
        IPoolManager.SwapParams calldata params,
        BalanceDelta delta,
        uint256 a,
        uint256 c,
        bool fresh
    ) internal returns (int128) {
        int256 n0 = delta.amount0();
        int256 n1 = delta.amount1();
        if (!fresh) {
            uint256 b = s.b;
            n0 += int128(uint128(b));
            n1 += int128(uint128(b >> 128));
            if (n0 != int128(n0) || n1 != int128(n1)) return _saturate(id, s, a);
        }
        s.b = uint256(uint128(int128(n0))) | (uint256(uint128(int128(n1))) << 128);

        uint256 surplus = _surplus(n0, n1, uint128(c));
        uint256 target = _target(surplus, a, c);
        uint256 w = uint128(a >> 64);
        if (target <= w) {
            if (fresh) s.a = a;
            emit HookCharge(PoolId.unwrap(id), surplus, target, 0);
            return 0;
        }
        if (target > SATURATED) return _saturate(id, s, a);

        // Exact input (amountSpecified < 0) charges the output token, exact output
        // charges the input token: in both cases the unspecified currency.
        bool unspecifiedIs0 = (params.amountSpecified < 0) != params.zeroForOne;
        (bool ok, uint256 tokenAmount, uint256 collectedValue) =
            _toSettlementAmount(target - w, unspecifiedIs0 ? WAD : uint128(c));
        if (!ok) return _saturate(id, s, a);

        // W advances by the value actually collected, so the rounding remainder stays
        // owed and is picked up by the next swap of the scope.
        if (collectedValue != 0 || fresh) s.a = (a & ~W_MASK) | ((w + collectedValue) << 64);
        emit HookCharge(PoolId.unwrap(id), surplus, target, collectedValue);
        if (tokenAmount == 0) return 0;

        // The positive return delta credits the hook with tokenAmount; minting ERC-6909
        // claims to the vault debits it by the same amount, so the hook's net delta is 0.
        Currency currency = unspecifiedIs0 ? key.currency0 : key.currency1;
        poolManager.mint(vault, currency.toId(), tokenAmount);
        emit HookSettled(PoolId.unwrap(id), Currency.unwrap(currency), tokenAmount);
        return int128(int256(tokenAmount));
    }

    /// @dev Fail open for the rest of the block: W = 2^128 - 1 blocks every further charge.
    function _saturate(PoolId id, Slots storage s, uint256 a) internal returns (int128) {
        s.a = a | W_MASK;
        emit HookCharge(PoolId.unwrap(id), 0, uint128(a >> 64), 0);
        return 0;
    }

    function _target(uint256 surplus, uint256 a, uint256 c) internal pure returns (uint256 target) {
        (target,) = computeTransfer(
            surplus, c >> 128, uint256(uint16(a >> 192)) * BPS_TO_WAD, uint256(uint16(a >> 208)) * BPS_TO_WAD, 0
        );
    }

    /// @dev [cum0 + cum1 * reference / WAD]^+ with token0 as the numeraire. The product
    /// rounds toward zero, as in the transaction-scoped library. |cum1| <= 2^127 and
    /// reference < 2^128, so nothing here can overflow.
    function _surplus(int256 cum0, int256 cum1, uint256 referencePriceWad) internal pure returns (uint256) {
        int256 value = cum0 + (cum1 * int256(referencePriceWad)) / int256(WAD);
        return value > 0 ? uint256(value) : 0;
    }

    function _kappa(uint256 referencePriceWad) internal view returns (uint256 kappa) {
        kappa = kappaConstant;
        if (gasUnits != 0) {
            uint256 baseFee = block.basefee;
            if (baseFee > type(uint64).max) return SATURATED;
            // g * (basefee + tau) < 2^129 and the price < 2^128, so the quotient < 2^197.
            uint256 gasFeeWei = uint256(gasUnits) * (baseFee + priorityFeeWei);
            uint256 price = gasPriceToken0Wad == 0 ? referencePriceWad : gasPriceToken0Wad;
            kappa += FullMath.mulDiv(gasFeeWei, price, WAD);
            if (kappa > SATURATED) kappa = SATURATED;
        }
    }

    function _observe(PoolId id) internal view returns (uint8 code, uint256 price) {
        uint256 updatedAt;
        bool valid;
        // A reverting oracle must not revert the swap: it is a rejected observation.
        try oracle.latestPrice(PoolId.unwrap(id)) returns (uint256 p, uint256 u, bool v) {
            (price, updatedAt, valid) = (p, u, v);
        } catch {
            return (ORACLE_REVERTED, 0);
        }
        if (!valid) return (ORACLE_SELF_INVALID, 0);
        if (price == 0) return (ORACLE_ZERO_PRICE, 0);
        if (updatedAt == 0) return (ORACLE_INCOMPLETE_ROUND, 0);
        if (updatedAt > block.timestamp || block.timestamp - updatedAt > stalenessThresholdSeconds) {
            return (ORACLE_STALE, 0);
        }
        if (price > SATURATED) return (ORACLE_OUT_OF_RANGE, 0);
    }

    /// @dev Converts a numeraire charge into base units of the unspecified currency,
    /// rounding down so the collected value never exceeds the charge. collectedValue
    /// is that token amount valued back in the numeraire, rounded up, so the watermark
    /// never records less than was taken. ok = false when the charge cannot be
    /// represented as an int128 return delta.
    function _toSettlementAmount(uint256 marginalCharge, uint256 referencePriceWad)
        internal
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

    function _reason(uint8 code) internal pure returns (string memory) {
        if (code == ORACLE_REVERTED) return "oracle-reverted";
        if (code == ORACLE_SELF_INVALID) return "self-reported-invalid";
        if (code == ORACLE_ZERO_PRICE) return "zero-price";
        if (code == ORACLE_INCOMPLETE_ROUND) return "incomplete-round";
        if (code == ORACLE_STALE) return "stale";
        return "price-out-of-range";
    }
}
