// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "v4-core/src/types/PoolId.sol";
import {Currency} from "v4-core/src/types/Currency.sol";
import {BalanceDelta} from "v4-core/src/types/BalanceDelta.sol";
import {FullMath} from "v4-core/src/libraries/FullMath.sol";

import {BlockScopedHookTestBase} from "./BlockScopedHookTestBase.sol";
import {DeltaManager} from "./DeltaManager.sol";
import {ParticipationAwareHook} from "../../src/hooks/ParticipationAwareHook.sol";
import {IReferenceOracle} from "../../src/interfaces/IReferenceOracle.sol";
import {MockOracle} from "../../src/mocks/MockOracle.sol";

/// @notice BlockScopeBoundary: the frozen boundary behaviour of the block-scoped hook.
///   1. A new block resets the accumulator and re-locks the reference and kappa.
///   2. A mid-block oracle update (or a mid-block invalid oracle) does not move the locked
///      reference; charging continues at it.
///   3. An invalid oracle opens no scope: the swap is charged 0 and not accumulated. The
///      next swap retries, so once the oracle is valid again a scope opens that holds only
///      the later swaps (decided 2026-10-06: retry, not a block-long latch). An oracle that
///      stays invalid charges nothing for the whole block.
///   4. Overflow (cumulative delta outside int128, target above uint128, settlement amount
///      outside int128) sets W = 2^128 - 1: nothing more is charged in that block, and the
///      next block reopens normally.
/// BlockScopeBoundaryTest runs with or without --isolate (no assertion depends on
/// transaction boundaries). BlockScopeBoundaryBaseFeeTest must run WITHOUT --isolate:
/// forge 1.5.1 executes isolated calls with block.basefee = 0 whatever vm.fee sets, so a
/// base-fee re-lock cannot be observed there.
abstract contract BlockScopeBoundaryBase is BlockScopedHookTestBase {
    using PoolIdLibrary for PoolKey;

    uint256 internal constant K = 2e18;
    uint64 internal constant GAS_UNITS = 150_000;
    uint64 internal constant TAU = 5e7;

    ParticipationAwareHook internal hook;
    PoolKey internal pool;
    PoolId internal poolId;
    bytes32 internal id;
    address internal alice;
    address internal bob;

    function setUp() public virtual {
        setUpBase();
        ParticipationAwareHook.Params memory p = _params(K, 0, LAMBDA_BPS, GAMMA_BPS);
        p.gasUnits = GAS_UNITS;
        p.priorityFeeWei = TAU; // gasPriceToken0Wad = 0: kappa's gas part is priced at the locked reference
        hook = _deployHook(p);
        (pool, poolId) = _pool(IHooks(address(hook)), 1.2e18);
        id = PoolId.unwrap(poolId);
        alice = _actor(1);
        bob = _actor(2);
    }

    function _kappa(uint256 basefee, uint256 ref) internal pure returns (uint256) {
        return K + FullMath.mulDiv(uint256(GAS_UNITS) * (basefee + TAU), ref, 1e18);
    }

    function _nextBlock() internal {
        vm.roll(block.number + 1);
        vm.warp(block.timestamp + 12);
    }

    /// @dev A charged swap that settles in token0 (exact output, zeroForOne). Returns the
    /// charge (vault increase) and the core delta (token0 = caller delta + charge).
    function _buy(address who, uint256 amount1) internal returns (uint256 charge, int256 d0, int256 d1) {
        uint256 v = _vault(currency0);
        BalanceDelta d = _swapAs(who, pool, _exactOut(true, amount1));
        charge = _vault(currency0) - v;
        d0 = int256(d.amount0()) + int256(charge);
        d1 = d.amount1();
    }
}

contract BlockScopeBoundaryTest is BlockScopeBoundaryBase {
    // ---------------------------------------------------------------- 1. new block
    function test_NewBlockResetsAccumulatorAndRelocksReferenceAndKappa() public {
        uint256 b = vm.getBlockNumber(); // not block.number: via_ir may re-read it after vm.roll
        (uint256 c1,,) = _buy(alice, 40e18);
        (uint256 c2,,) = _buy(bob, 10e18);
        ParticipationAwareHook.Scope memory s = hook.getScope(poolId);
        assertEq(s.blockNumber, b);
        assertEq(s.referencePriceWad, 1.2e18, "reference locked at open");
        assertEq(s.kappa, _kappa(block.basefee, 1.2e18), "kappa locked at open");
        assertGt(c1, 0);
        assertEq(s.watermark, c1 + c2, "W = sum of the block's charges");

        _nextBlock();
        oracle.setPrice(id, 1.5e18);
        (uint256 c3, int256 d0, int256 d1) = _buy(alice, 20e18);
        ParticipationAwareHook.Scope memory t = hook.getScope(poolId);
        assertEq(t.blockNumber, b + 1, "new scope");
        assertEq(int256(t.cumulativeDelta0), d0, "accumulator reset: holds only this swap (token0)");
        assertEq(int256(t.cumulativeDelta1), d1, "accumulator reset: holds only this swap (token1)");
        assertEq(t.referencePriceWad, 1.5e18, "reference re-locked");
        assertEq(t.kappa, _kappa(block.basefee, 1.5e18), "kappa re-locked (gas part priced at the new reference)");
        assertTrue(t.kappa != s.kappa, "kappa changed with the reference");
        assertEq(t.watermark, c3, "W restarts at 0");
        assertEq(c3, _F(hook, t, _scopeSurplus(t)), "charged F(own surplus): nothing carried over");
    }

    // ---------------------------------------------------------------- 2. mid-block oracle update
    function test_MidBlockOracleUpdateDoesNotMoveLockedReference() public {
        _buy(alice, 20e18);
        ParticipationAwareHook.Scope memory s0 = hook.getScope(poolId);
        oracle.setPrice(id, 2e18); // mid-block update
        (uint256 c, int256 d0, int256 d1) = _buy(bob, 20e18);
        ParticipationAwareHook.Scope memory s1 = hook.getScope(poolId);
        assertEq(s1.referencePriceWad, 1.2e18, "reference stays locked");
        assertEq(s1.kappa, s0.kappa, "kappa stays locked");
        assertEq(int256(s1.cumulativeDelta0), int256(s0.cumulativeDelta0) + d0);
        assertEq(int256(s1.cumulativeDelta1), int256(s0.cumulativeDelta1) + d1);
        assertEq(c, _F(hook, s1, _scopeSurplus(s1)) - s0.watermark, "charged at the locked reference");

        oracle.setMode(id, MockOracle.Mode.Revert); // the oracle fails mid-block: not read again
        (uint256 c2,,) = _buy(alice, 20e18);
        ParticipationAwareHook.Scope memory s2 = hook.getScope(poolId);
        assertGt(c2, 0, "charging continues at the locked reference");
        assertEq(c2, _F(hook, s2, _scopeSurplus(s2)) - s1.watermark);

        _nextBlock();
        oracle.setMode(id, MockOracle.Mode.Normal);
        oracle.setPrice(id, 2e18);
        _buy(bob, 1e18);
        assertEq(hook.getScope(poolId).referencePriceWad, 2e18, "the next block locks the updated price");
    }

    // ---------------------------------------------------------------- 3. invalid oracle (retry)
    function _invalidate(uint256 kind) internal {
        if (kind < 5) oracle.setMode(id, MockOracle.Mode(kind + 1)); // ForceStale .. Revert
        else oracle.setPrice(id, uint256(type(uint128).max) + 1); // out of range
    }

    function test_InvalidOracleChargesNothingAndRetriesWhenValidAgain() public {
        for (uint256 kind; kind < 6; ++kind) {
            _nextBlock();
            oracle.setMode(id, MockOracle.Mode.Normal);
            oracle.setPrice(id, 1.2e18);
            _invalidate(kind);
            ParticipationAwareHook.Scope memory before = hook.getScope(poolId);
            assertEq(hook.quote(pool, -20e18, 20e18), 0, "quote 0 while invalid");
            (uint256 c0,,) = _buy(alice, 40e18);
            ParticipationAwareHook.Scope memory s0 = hook.getScope(poolId);
            assertEq(c0, 0, "invalid oracle: charge 0");
            assertTrue(s0.blockNumber != block.number, "no scope opened");
            assertEq(abi.encode(s0), abi.encode(before), "nothing written");

            oracle.setMode(id, MockOracle.Mode.Normal); // valid again in the same block
            oracle.setPrice(id, 1.2e18);
            (uint256 c1, int256 d0, int256 d1) = _buy(bob, 40e18);
            ParticipationAwareHook.Scope memory s1 = hook.getScope(poolId);
            assertEq(s1.blockNumber, block.number, "retry opened the scope");
            assertEq(int256(s1.cumulativeDelta0), d0, "scope holds only the later swap");
            assertEq(int256(s1.cumulativeDelta1), d1);
            assertGt(c1, 0);
            assertEq(c1, _F(hook, s1, _scopeSurplus(s1)), "charged as an unsplit swap");
        }
    }

    function test_OracleInvalidForTheWholeBlockChargesNothing() public {
        for (uint256 kind; kind < 6; ++kind) {
            _nextBlock();
            oracle.setMode(id, MockOracle.Mode.Normal);
            oracle.setPrice(id, 1.2e18);
            _invalidate(kind);
            for (uint256 j; j < 3; ++j) {
                (uint256 c,,) = _buy(j % 2 == 0 ? alice : bob, 30e18);
                assertEq(c, 0, "no charge while the oracle is invalid");
            }
            assertTrue(hook.getScope(poolId).blockNumber != block.number, "no scope in this block");
        }
        _nextBlock();
        oracle.setMode(id, MockOracle.Mode.Normal);
        oracle.setPrice(id, 1.2e18);
        (uint256 c2,,) = _buy(alice, 30e18);
        assertGt(c2, 0, "valid in the next block: charged again");
    }

    // ---------------------------------------------------------------- 4. overflow fail-safe
    // Driven through DeltaManager: real pools cannot produce cumulative deltas near 2^127.
    DeltaManager internal dm;
    ParticipationAwareHook internal raw;
    PoolKey internal rawKey;
    bytes32 internal rawId;

    function _rawSetUp() internal {
        dm = new DeltaManager();
        raw = new ParticipationAwareHook(
            IPoolManager(address(dm)), IReferenceOracle(address(oracle)), VAULT, _params(1e18, 0, LAMBDA_BPS, GAMMA_BPS)
        );
        rawKey = PoolKey(Currency.wrap(address(0xA0)), Currency.wrap(address(0xB0)), 3000, 60, IHooks(address(raw)));
        rawId = PoolId.unwrap(rawKey.toId());
        oracle.setPrice(rawId, 1e18);
    }

    function _assertSaturatedForRestOfBlock() internal {
        ParticipationAwareHook.Scope memory s = raw.getScope(rawKey.toId());
        assertEq(s.blockNumber, block.number);
        assertEq(s.watermark, type(uint128).max, "W saturated");
        assertEq(raw.quote(rawKey, 1e30, 0), 0, "quote 0 after saturation");
        assertEq(dm.swap(raw, rawKey, -1e30, 0, true), 0, "later swap: no charge");
        assertEq(dm.swap(raw, rawKey, 1e30, 0, true), 0, "later swap with more surplus: no charge");
        assertEq(dm.swap(raw, rawKey, 0, 1e30, false), 0, "later swap settling in token1: no charge");
        assertEq(raw.getScope(rawKey.toId()).watermark, type(uint128).max, "still saturated");
        _nextBlock();
        oracle.setMode(rawId, MockOracle.Mode.Normal);
        oracle.setPrice(rawId, 1e18);
        int128 r = dm.swap(raw, rawKey, 10e18, 0, true);
        ParticipationAwareHook.Scope memory t = raw.getScope(rawKey.toId());
        assertEq(t.blockNumber, block.number, "next block reopens");
        assertEq(uint256(int256(r)), _F(raw, t, 10e18), "and charges normally");
        assertEq(t.watermark, uint256(int256(r)));
    }

    function test_OverflowCumulativeDeltaSaturatesForRestOfBlock() public {
        _rawSetUp();
        int128 r0 = dm.swap(raw, rawKey, type(int128).max - 10, 0, true);
        assertGt(r0, 0, "first swap charged");
        assertEq(dm.swap(raw, rawKey, 11, 0, true), 0, "cum0 leaves int128: charge 0");
        _assertSaturatedForRestOfBlock();
    }

    function test_OverflowNegativeCumulativeDeltaSaturates() public {
        _rawSetUp();
        assertEq(dm.swap(raw, rawKey, 0, type(int128).min + 5, true), 0);
        assertEq(dm.swap(raw, rawKey, 0, -6, true), 0, "cum1 below int128 min");
        _assertSaturatedForRestOfBlock();
    }

    function test_OverflowTargetAboveUint128Saturates() public {
        _rawSetUp();
        _nextBlock();
        oracle.setPrice(rawId, 2 ** 127); // valid (< 2^128), so cum1 * ref can exceed 2^128
        assertEq(dm.swap(raw, rawKey, 0, 1e19, true), 0, "target > 2^128 - 1: charge 0");
        _assertSaturatedForRestOfBlock();
    }

    function test_OverflowSettlementAmountSaturates() public {
        _rawSetUp();
        _nextBlock();
        oracle.setPrice(rawId, 1); // token1 worth 1e-18 token0 per unit
        // target ~ 7.5e20 fits uint128, but settling it in token1 needs 7.5e38 > int128 max units
        assertEq(dm.swap(raw, rawKey, 1e21, 0, false), 0, "settlement outside int128: charge 0");
        _assertSaturatedForRestOfBlock();
    }
}

contract BlockScopeBoundaryBaseFeeTest is BlockScopeBoundaryBase {
    function test_NewBlockRelocksKappaAtNewBaseFee() public {
        transientProbe.set();
        require(transientProbe.isSet(), "run this contract without --isolate (isolated calls see basefee 0)");
        vm.fee(10 gwei);
        _buy(alice, 20e18);
        ParticipationAwareHook.Scope memory s = hook.getScope(poolId);
        assertEq(s.kappa, _kappa(10 gwei, 1.2e18), "kappa at the opening base fee");

        vm.fee(100 gwei); // base fee changes within the block (not possible on chain; checks the lock)
        _buy(bob, 20e18);
        assertEq(hook.getScope(poolId).kappa, s.kappa, "kappa stays locked within the block");

        _nextBlock();
        vm.fee(50 gwei);
        (uint256 c,,) = _buy(alice, 20e18);
        ParticipationAwareHook.Scope memory t = hook.getScope(poolId);
        assertEq(t.kappa, _kappa(50 gwei, 1.2e18), "re-locked at the new block's base fee");
        assertEq(c, _F(hook, t, _scopeSurplus(t)));
    }
}
