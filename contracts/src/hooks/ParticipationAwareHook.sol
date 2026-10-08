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

/// @notice Uniswap v4 hook for participation-aware surplus sharing: block scope with
/// per-transaction clipping (V1) and a relative proportional buffer on each transaction's net
/// token1 change (P-net). All logic runs in
/// afterSwap (permissions: AFTER_SWAP, AFTER_SWAP_RETURNS_DELTA). The oracle is read in the
/// afterSwap of the block scope's first swap.
///
/// Accounting (integer mirror: common.fixedpoint.ScopedHookReference(scope="block",
/// accumulation="tx_clip", buffer="rel")). One scope per pool and block. Within it, each
/// transaction t has a bracket
///     b_t = [ X_t . pi_hat  -  ceil(eps_rel * pi_hat * |X_t,1|) ]^+
/// where X_t is the transaction's net core delta, pi_hat the locked reference (token0 is the
/// numeraire) and |X_t,1| the absolute net token1 change of the transaction. A round trip
/// inside the transaction (volume that nets out) therefore does not enlarge the buffer, and
/// the bracket of a transaction depends only on its net delta. The block's surplus
/// is A = sum of the completed transactions' brackets + the current transaction's bracket, and
/// the watermark rule of Definition 3 applies to F(A): each swap pays F(A) - W, W never falls.
/// A transaction's negative surplus is clipped at zero before it enters the block total, so an
/// unrelated transaction can never lower another's charge; within a transaction the surplus
/// nets as before, so splitting inside a transaction gains nothing. kappa = K_hat only
/// (deducted once per block, inside F); the buffer is charged per unit of net token1 change,
/// so splitting a one-directional trade across transactions does not shrink it.
///
/// The current transaction's state lives in transient storage and vanishes when the
/// transaction ends; the persistent slot P holds the block surplus A including the current
/// transaction's clipped bracket and is rewritten on every swap. A transaction's first swap on
/// the pool (transient flag not set) therefore finds every earlier transaction already folded
/// into P and takes closed := P. eps_rel is a configured value (parts per billion of the price,
/// settable by `epsilonAdmin` for a rolling calibration), locked into the scope when the block
/// scope opens, together with the reference, kappa, lambda and gamma.
///
/// State, persistent and keyed by PoolId, in three slots:
///   A: uint64 block | uint128 watermark W | uint16 lambdaBps | uint16 gammaBps | uint32 epsRelPpb
///   C: uint128 referencePriceWad | uint128 kappa
///   P: P_SET | (closed + b_t)                      (block surplus A after the latest swap)
/// and per pool and transaction, transient, at keccak256(TX_NAMESPACE, poolId) + k:
///   T0: int128 txDelta0 | int128 txDelta1             (current transaction, net)
///   T1: uint128 closed | flag at bit 128              (sum of the earlier transactions' brackets;
///                                                      flag = this transaction swapped here)
/// A scope opens on the first swap of a block whose oracle read is valid; an invalid read opens
/// nothing, charges 0, accumulates nothing, and the next swap retries. Opening never zeroes the
/// old slots (every write is nonzero to nonzero after the pool's first swap).
///
/// kappa = K_const + g_hat * (basefee + tau_hat) * p(ETH -> token0), p = gasPriceToken0Wad or the
/// locked reference when that is 0; clamped to uint128. The evaluated configuration uses
/// tau_hat = 3 gwei (Q3) and g_hat = 180,214.
///
/// Bit widths: the reference must fit uint128 (larger oracle prices are rejected), so
/// txDelta1 * reference fits int256. A transaction delta outside int128, a closed surplus (P at
/// a transaction's first swap) above uint128, a target above uint128 or a charge that does not
/// fit the int128 return delta saturates W (W = 2^128 - 1): no further charge on the pool in
/// that block.
///
/// Settlement as before: each marginal charge is converted into the swap's unspecified currency
/// at the locked reference (rounded down), returned as the afterSwap delta and minted as
/// ERC-6909 claims to the vault; W advances by the value actually collected (rounded up).
contract ParticipationAwareHook is MinimalBaseHook, SurplusSharingAccounting {
    using PoolIdLibrary for PoolKey;

    struct Params {
        uint256 kHatConstant; // B + C_hedge + C_slippage + R, token0 base units
        uint64 gasUnits; // g_hat
        uint64 priorityFeeWei; // tau_hat (3 gwei in the evaluated configuration)
        uint128 gasPriceToken0Wad; // token0 base units per wei, WAD; 0 = use the reference
        uint16 lambdaBps;
        uint16 gammaBps;
        uint32 stalenessThresholdSeconds;
        uint32 epsilonRelPpb; // initial eps_rel, parts per billion of the price
        address epsilonAdmin; // may update eps_rel; 0 = fixed
    }

    struct Scope {
        uint64 blockNumber;
        uint128 watermark;
        uint16 lambdaBps;
        uint16 gammaBps;
        uint32 epsilonRelPpb;
        int128 cumulativeDelta0; // the current transaction's net delta
        int128 cumulativeDelta1;
        uint128 referencePriceWad;
        uint128 kappa;
        uint256 closedSurplus; // sum of the completed transactions' clipped, buffered brackets
    }

    struct Slots {
        uint256 a;
        uint256 c;
        uint256 p; // block surplus A: closed + the current transaction's bracket
    }

    uint256 internal constant BPS = 10_000;
    uint256 internal constant BPS_TO_WAD = 1e14;
    uint256 internal constant PPB_TO_WAD = 1e9;
    uint256 internal constant MAX_EPSILON_PPB = 1e9; // 100% of the price
    uint256 internal constant SATURATED = type(uint128).max;
    uint256 internal constant W_MASK = SATURATED << 64;
    bytes32 internal constant TX_NAMESPACE = keccak256("ParticipationAwareHook.tx");
    uint256 internal constant TX_FLAG = 1 << 128;
    /// @dev Marks P as written, so P never returns to zero and every later write is nonzero to
    /// nonzero (a zero-to-nonzero SSTORE costs 20,000 gas more). The block surplus is below
    /// 2^197 (closed < 2^128, a bracket below 2^127 + 2^127 * 2^128 / WAD), so bit 255 is free.
    uint256 internal constant P_SET = 1 << 255;

    uint8 internal constant ORACLE_OK = 0;
    uint8 internal constant ORACLE_REVERTED = 1;
    uint8 internal constant ORACLE_SELF_INVALID = 2;
    uint8 internal constant ORACLE_ZERO_PRICE = 3;
    uint8 internal constant ORACLE_INCOMPLETE_ROUND = 4;
    uint8 internal constant ORACLE_STALE = 5;
    uint8 internal constant ORACLE_OUT_OF_RANGE = 6;

    error InvalidVault();
    error InvalidEpsilon();
    error NotEpsilonAdmin();

    event OracleRejected(bytes32 indexed poolId, string reason);
    event HookCharge(bytes32 indexed poolId, uint256 cumulativeSurplus, uint256 cumulativeTarget, uint256 marginalCharge);
    event HookSettled(bytes32 indexed poolId, address currency, uint256 amount);
    event EpsilonUpdated(uint32 epsilonRelPpb);

    IReferenceOracle public immutable oracle;
    address public immutable vault;
    /// @notice min(K_const, 2^128 - 1): the part of kappa that does not depend on gas.
    uint256 public immutable kappaConstant;
    uint64 public immutable gasUnits;
    uint64 public immutable priorityFeeWei;
    uint128 public immutable gasPriceToken0Wad;
    uint16 public immutable lambdaBps;
    uint16 public immutable gammaBps;
    uint256 public immutable stalenessThresholdSeconds;
    address public immutable epsilonAdmin;

    /// @notice eps_rel in parts per billion; a scope locks the value current at its opening.
    uint32 public epsilonRelPpb;

    mapping(PoolId => Slots) internal _scopes;

    constructor(IPoolManager _poolManager, IReferenceOracle _oracle, address _vault, Params memory p)
        MinimalBaseHook(_poolManager)
    {
        if (p.lambdaBps > BPS) revert InvalidLambda();
        if (p.gammaBps >= BPS) revert InvalidGamma();
        if (_vault == address(0)) revert InvalidVault();
        if (p.epsilonRelPpb > MAX_EPSILON_PPB) revert InvalidEpsilon();
        oracle = _oracle;
        vault = _vault;
        kappaConstant = p.kHatConstant > SATURATED ? SATURATED : p.kHatConstant;
        gasUnits = p.gasUnits;
        priorityFeeWei = p.priorityFeeWei;
        gasPriceToken0Wad = p.gasPriceToken0Wad;
        lambdaBps = p.lambdaBps;
        gammaBps = p.gammaBps;
        stalenessThresholdSeconds = p.stalenessThresholdSeconds;
        epsilonAdmin = p.epsilonAdmin;
        epsilonRelPpb = p.epsilonRelPpb;
    }

    /// @notice Updates eps_rel (rolling calibration). Scopes already open keep their locked value.
    function setEpsilonRelPpb(uint32 v) external {
        if (msg.sender != epsilonAdmin || epsilonAdmin == address(0)) revert NotEpsilonAdmin();
        if (v > MAX_EPSILON_PPB) revert InvalidEpsilon();
        epsilonRelPpb = v;
        emit EpsilonUpdated(v);
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
    /// (delta0, delta1) would raise the watermark by if it executed now in the current
    /// transaction (positive = received by the swapper). In a block without an open scope it
    /// reads the oracle and the base fee as the opening swap would. Returns 0 wherever the hook
    /// would fail open. The settled amount can be lower by the conversion rounding.
    function quote(PoolKey calldata key, int256 delta0, int256 delta1) external view returns (uint256) {
        PoolId id = key.toId();
        Slots storage s = _scopes[id];
        uint256 a = s.a;
        uint256 c;
        if (delta0 != int128(delta0) || delta1 != int128(delta1)) return 0;
        uint256 closed;
        if (uint64(a) == block.number) {
            if ((a & W_MASK) == W_MASK) return 0;
            c = s.c;
            uint256 t = _txSlot(id);
            uint256 g = _tload(t + 1);
            if (g & TX_FLAG == 0) {
                closed = s.p & ~P_SET; // a new transaction: every earlier one is folded into P
                if (closed > SATURATED) return 0;
            } else {
                closed = uint128(g);
                uint256 x = _tload(t);
                delta0 += int128(uint128(x));
                delta1 += int128(uint128(x >> 128));
                if (delta0 != int128(delta0) || delta1 != int128(delta1)) return 0;
            }
        } else {
            (uint8 code, uint256 price) = _observe(id);
            if (code != ORACLE_OK) return 0;
            c = price | (_kappa(price) << 128);
            a = _freshA();
        }
        uint256 target = _target(closed + _bracket(delta0, delta1, a, c), a, c);
        if (target > SATURATED) return 0;
        uint256 w = uint128(a >> 64);
        return target > w ? target - w : 0;
    }

    /// @notice Decoded scope of a pool (testing and diagnostics), as seen from the calling
    /// transaction. The current transaction's fields (cumulativeDelta0/1) are transient. Inside a transaction that has swapped on the pool they hold its running
    /// values, and closedSurplus holds the earlier transactions' brackets. Anywhere else
    /// (another transaction, or a later call under `forge test --isolate`) they read 0 and
    /// closedSurplus is P, the whole block surplus, since every transaction is then complete.
    /// In both cases closedSurplus + the bracket of the current fields = scopeSurplus().
    function getScope(PoolId id) external view returns (Scope memory scope) {
        Slots storage s = _scopes[id];
        (uint256 a, uint256 c) = (s.a, s.c);
        scope.blockNumber = uint64(a);
        scope.watermark = uint128(a >> 64);
        scope.lambdaBps = uint16(a >> 192);
        scope.gammaBps = uint16(a >> 208);
        scope.epsilonRelPpb = uint32(a >> 224);
        scope.referencePriceWad = uint128(c);
        scope.kappa = uint128(c >> 128);
        uint256 t = _txSlot(id);
        uint256 g = _tload(t + 1);
        if (g & TX_FLAG == 0) {
            scope.closedSurplus = s.p & ~P_SET;
        } else {
            uint256 x = _tload(t);
            scope.cumulativeDelta0 = int128(uint128(x));
            scope.cumulativeDelta1 = int128(uint128(x >> 128));
            scope.closedSurplus = uint128(g);
        }
    }

    /// @notice The block surplus A of the pool's open scope (closed brackets + the current
    /// transaction's bracket), as the hook computes it: the slot P.
    function scopeSurplus(PoolId id) external view returns (uint256) {
        return _scopes[id].p & ~P_SET;
    }

    /// @notice kappa that a scope opened now would lock, given a reference price.
    function currentKappa(uint256 referencePriceWad) external view returns (uint256) {
        return _kappa(referencePriceWad);
    }

    /// @dev Reads the oracle; on success writes slot C and returns it. A rejected read opens no
    /// scope and leaves every slot untouched.
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

    /// @dev A for a scope opened in this block: W = 0, current lambda, gamma and eps_rel.
    function _freshA() internal view returns (uint256) {
        return uint256(uint64(block.number)) | (uint256(lambdaBps) << 192) | (uint256(gammaBps) << 208)
            | (uint256(epsilonRelPpb) << 224);
    }

    /// @dev Accumulates the swap into the current transaction (a transaction's first swap takes
    /// closed := P, which already holds the previous transaction's bracket), rewrites P, raises
    /// W and settles the marginal charge.
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
        uint256 closed;
        uint256 t = _txSlot(id);
        bool newTx = fresh; // a fresh scope starts the transaction's state, whatever the flag says
        if (!fresh) {
            uint256 g = _tload(t + 1);
            if (g & TX_FLAG == 0) {
                closed = s.p & ~P_SET; // fold the completed transactions
                if (closed > SATURATED) return _saturate(id, s, a);
                newTx = true;
            } else {
                closed = uint128(g);
                uint256 x = _tload(t);
                n0 += int128(uint128(x));
                n1 += int128(uint128(x >> 128));
                if (n0 != int128(n0) || n1 != int128(n1)) return _saturate(id, s, a);
            }
        }
        _tstore(t, uint256(uint128(int128(n0))) | (uint256(uint128(int128(n1))) << 128));
        if (newTx) _tstore(t + 1, closed | TX_FLAG);

        uint256 surplus = closed + _bracket(n0, n1, a, c);
        s.p = surplus | P_SET;
        uint256 target = _target(surplus, a, c);
        uint256 w = uint128(a >> 64);
        if (target <= w) {
            if (fresh) s.a = a;
            emit HookCharge(PoolId.unwrap(id), surplus, target, 0);
            return 0;
        }
        if (target > SATURATED) return _saturate(id, s, a);

        // Exact input (amountSpecified < 0) charges the output token, exact output charges the
        // input token: in both cases the unspecified currency.
        bool unspecifiedIs0 = (params.amountSpecified < 0) != params.zeroForOne;
        (bool ok, uint256 tokenAmount, uint256 collectedValue) =
            _toSettlementAmount(target - w, unspecifiedIs0 ? WAD : uint128(c));
        if (!ok) return _saturate(id, s, a);

        // W advances by the value actually collected, so the rounding remainder stays owed.
        if (collectedValue != 0 || fresh) s.a = (a & ~W_MASK) | ((w + collectedValue) << 64);
        emit HookCharge(PoolId.unwrap(id), surplus, target, collectedValue);
        if (tokenAmount == 0) return 0;

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

    /// @dev [x0 + x1 * reference / WAD - ceil(eps * reference * |x1| / WAD^2)]^+ with token0 as
    /// the numeraire and (x0, x1) the transaction's net delta: the product rounds toward zero,
    /// the buffer up (in the hook's favour). |x1| <= 2^127 and reference < 2^128, so the product
    /// fits int256; eps * reference < 2^190 and the 512-bit mulDiv keeps the buffer exact.
    function _bracket(int256 x0, int256 x1, uint256 a, uint256 c) internal pure returns (uint256) {
        uint256 ref = uint128(c);
        int256 value = x0 + (x1 * int256(ref)) / int256(WAD);
        uint256 eps = uint256(uint32(a >> 224)) * PPB_TO_WAD;
        if (eps != 0 && x1 != 0) value -= int256(FullMath.mulDivRoundingUp(eps * ref, _abs(x1), WAD * WAD));
        return value > 0 ? uint256(value) : 0;
    }

    function _abs(int256 x) internal pure returns (uint256) {
        return x >= 0 ? uint256(x) : uint256(-x);
    }

    /// @dev The pool's first transient slot T0; T1 follows it.
    function _txSlot(PoolId id) internal pure returns (uint256) {
        return uint256(keccak256(abi.encode(TX_NAMESPACE, PoolId.unwrap(id))));
    }

    function _tload(uint256 slot) internal view returns (uint256 v) {
        assembly ("memory-safe") {
            v := tload(slot)
        }
    }

    function _tstore(uint256 slot, uint256 v) internal {
        assembly ("memory-safe") {
            tstore(slot, v)
        }
    }

    function _kappa(uint256 referencePriceWad) internal view returns (uint256 kappa) {
        kappa = kappaConstant;
        if (gasUnits != 0) {
            uint256 baseFee = block.basefee;
            if (baseFee > type(uint64).max) return SATURATED;
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

    /// @dev Converts a numeraire charge into base units of the unspecified currency, rounding
    /// down; collectedValue is that amount valued back in the numeraire, rounded up. ok = false
    /// when the charge cannot be represented as an int128 return delta.
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
