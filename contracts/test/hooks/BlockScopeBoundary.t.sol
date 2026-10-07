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
    uint64 internal constant TAU = 3 gwei; // the configured tau_hat (Q3)

    ParticipationAwareHook internal hook;
    PoolKey internal pool;
    PoolId internal poolId;
    bytes32 internal id;
    address internal alice;
    address internal bob;
    bool internal isolated; // every external call is its own transaction
    uint256 internal px = 1.2e18; // oracle price a scope opened now would lock (0 = invalid), for the reference trace

    function setUp() public virtual {
        setUpBase();
        ParticipationAwareHook.Params memory p = _params(K, 0, LAMBDA_BPS, GAMMA_BPS);
        p.gasUnits = GAS_UNITS;
        p.priorityFeeWei = TAU; // gasPriceToken0Wad = 0: kappa's gas part is priced at the locked reference
        p.epsilonRelPpb = FINAL_EPS_PPB; // relative buffer, locked per scope
        p.epsilonAdmin = address(this);
        hook = _deployHook(p);
        (pool, poolId) = _pool(IHooks(address(hook)), 1.2e18);
        id = PoolId.unwrap(poolId);
        alice = _actor(1);
        bob = _actor(2);
        transientProbe.set();
        isolated = !transientProbe.isSet();
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
        BalanceDelta d = _swapRec(who, pool, _exactOut(true, amount1), px);
        charge = _vault(currency0) - v;
        d0 = int256(d.amount0()) + int256(charge);
        d1 = d.amount1();
    }
}

contract BlockScopeBoundaryTest is BlockScopeBoundaryBase {
    // ---------------------------------------------------------------- 1. new block
    function test_NewBlockResetsAccumulatorAndRelocksReferenceAndKappa() public {
        uint256 b = vm.getBlockNumber(); // not block.number: via_ir may re-read it after vm.roll
        _refInit();
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
        px = 1.5e18;
        (uint256 c3, int256 d0, int256 d1) = _buy(alice, 20e18);
        ParticipationAwareHook.Scope memory t = hook.getScope(poolId);
        assertEq(t.blockNumber, b + 1, "new scope");
        assertEq(hook.scopeSurplus(poolId), _txBracket(t, d0, d1), "accumulator reset: holds only this swap");
        if (!isolated) {
            // the transaction is still open, so its transient fields are readable
            assertEq(int256(t.cumulativeDelta0), d0, "accumulator reset: holds only this swap (token0)");
            assertEq(int256(t.cumulativeDelta1), d1, "accumulator reset: holds only this swap (token1)");
            assertEq(t.closedSurplus, 0, "no closed transactions carried over");
        }
        assertEq(t.referencePriceWad, 1.5e18, "reference re-locked");
        assertEq(t.kappa, _kappa(block.basefee, 1.5e18), "kappa re-locked (gas part priced at the new reference)");
        assertTrue(t.kappa != s.kappa, "kappa changed with the reference");
        assertEq(t.watermark, c3, "W restarts at 0");
        // expected charges from ScopedHookReference(scope="block", accumulation="tx_clip", buffer="rel")
        RefOut memory e = _refRun(hook);
        assertEq(c1, e.w[0], "charge 1 = reference");
        assertEq(c2, e.w[1], "charge 2 = reference");
        assertEq(c3, e.w[2], "charge 3 = reference: nothing carried over");
        assertEq(hook.scopeSurplus(poolId), e.surplus[2], "block surplus = reference");
    }

    function test_EpsilonLockedWhenTheScopeOpens() public {
        _buy(alice, 20e18);
        assertEq(hook.getScope(poolId).epsilonRelPpb, FINAL_EPS_PPB, "eps locked at open");
        hook.setEpsilonRelPpb(5_000_000); // mid-block update (rolling calibration)
        (uint256 c,,) = _buy(bob, 20e18);
        ParticipationAwareHook.Scope memory s = hook.getScope(poolId);
        assertEq(s.epsilonRelPpb, FINAL_EPS_PPB, "the open scope keeps its eps");
        assertEq(c, _F(hook, s, _scopeSurplus(s)) - (s.watermark - c), "charged with the locked eps");
        _nextBlock();
        oracle.setPrice(id, 1.2e18);
        _buy(alice, 20e18);
        assertEq(hook.getScope(poolId).epsilonRelPpb, 5_000_000, "the next block locks the new eps");
        vm.prank(alice);
        vm.expectRevert(ParticipationAwareHook.NotEpsilonAdmin.selector);
        hook.setEpsilonRelPpb(1);
    }

    // ---------------------------------------------------------------- 2. mid-block oracle update
    function test_MidBlockOracleUpdateDoesNotMoveLockedReference() public {
        _refInit();
        (uint256 c0,,) = _buy(alice, 20e18);
        ParticipationAwareHook.Scope memory s0 = hook.getScope(poolId);
        oracle.setPrice(id, 2e18); // mid-block update
        px = 2e18;
        (uint256 c, int256 d0, int256 d1) = _buy(bob, 20e18);
        ParticipationAwareHook.Scope memory s1 = hook.getScope(poolId);
        assertEq(s1.referencePriceWad, 1.2e18, "reference stays locked");
        assertEq(s1.kappa, s0.kappa, "kappa stays locked");
        if (isolated) {
            // Bob's swap is a new transaction: Alice's bracket is folded into the block total. Read
            // from a later transaction, Bob's bracket is folded too (closedSurplus = P) and the
            // transient transaction fields are 0.
            assertEq(s1.closedSurplus, _scopeSurplus(s0) + _txBracket(s1, d0, d1), "previous transaction folded");
            assertEq(s1.cumulativeDelta0, 0, "current transaction's state is transient");
            assertEq(s1.cumulativeDelta1, 0);
        } else {
            // one transaction: the swaps net as before
            assertEq(s1.closedSurplus, 0);
            assertEq(int256(s1.cumulativeDelta0), int256(s0.cumulativeDelta0) + d0);
            assertEq(int256(s1.cumulativeDelta1), int256(s0.cumulativeDelta1) + d1);
        }

        oracle.setMode(id, MockOracle.Mode.Revert); // the oracle fails mid-block: not read again
        px = 0;
        (uint256 c2,,) = _buy(alice, 20e18);
        assertGt(c2, 0, "charging continues at the locked reference");

        _nextBlock();
        oracle.setMode(id, MockOracle.Mode.Normal);
        oracle.setPrice(id, 2e18);
        px = 2e18;
        (uint256 c3,,) = _buy(bob, 1e18);
        assertEq(hook.getScope(poolId).referencePriceWad, 2e18, "the next block locks the updated price");

        // expected charges from ScopedHookReference(scope="block", accumulation="tx_clip", buffer="rel"):
        // the trace gives the reference 2e18 / an invalid price at the mid-block swaps, which it ignores
        // because the scope is open, so matching it means the hook charged at the locked 1.2e18
        RefOut memory e = _refRun(hook);
        assertEq(c0, e.w[0], "charge 0 = reference");
        assertEq(c, e.w[1], "charged at the locked reference = reference");
        assertEq(c2, e.w[2], "charge after the oracle failed = reference");
        assertEq(c3, e.w[3], "next block = reference");
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
            assertEq(hook.scopeSurplus(poolId), _txBracket(s1, d0, d1), "scope holds only the later swap");
            if (!isolated) {
                // the transaction is still open, so its transient fields are readable
                assertEq(int256(s1.cumulativeDelta0), d0, "scope holds only the later swap");
                assertEq(int256(s1.cumulativeDelta1), d1);
            }
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
        assertEq(_dmSwap(-1e30, 0, true), 0, "later swap: no charge");
        assertEq(_dmSwap(1e30, 0, true), 0, "later swap with more surplus: no charge");
        assertEq(_dmSwap(0, 1e30, false), 0, "later swap settling in token1: no charge");
        assertEq(raw.getScope(rawKey.toId()).watermark, type(uint128).max, "still saturated");
        _nextBlock();
        oracle.setMode(rawId, MockOracle.Mode.Normal);
        oracle.setPrice(rawId, 1e18);
        rawPx = 1e18;
        int128 r = _dmSwap(10e18, 0, true);
        ParticipationAwareHook.Scope memory t = raw.getScope(rawKey.toId());
        assertEq(t.blockNumber, block.number, "next block reopens");
        assertEq(uint256(int256(r)), _F(raw, t, 10e18), "and charges normally");
        assertEq(t.watermark, uint256(int256(r)));
    }

    /// @dev Swaps inside one call: one transaction in every mode.
    function _oneTx(int128[] memory d0, int128[] memory d1) internal returns (int128[] memory) {
        for (uint256 i; i < d0.length; ++i) _refPush(d0[i], d1[i], true, rawPx);
        _refEndTx();
        return dm.swapMany(raw, rawKey, d0, d1, true);
    }

    uint256 internal rawPx = 1e18;

    /// @dev One DeltaManager swap (one call), recorded for the reference.
    function _dmSwap(int128 d0, int128 d1, bool settle0) internal returns (int128 r) {
        _refPush(d0, d1, settle0, rawPx);
        _refEndTx();
        return dm.swap(raw, rawKey, d0, d1, settle0);
    }

    /// @dev The reference reports a saturated (or absent) scope as surplus = 2^256 - 1. The trace
    /// ends with _assertSaturatedForRestOfBlock: three swaps in the saturated block, one in the next.
    function _assertReferenceSaturatedFrom(uint256 trigger) internal {
        RefOut memory e = _refRun(raw);
        uint256 n = e.w.length;
        for (uint256 i = trigger; i < n - 1; ++i) {
            assertEq(e.surplus[i], type(uint256).max, "reference saturated for the rest of the block");
            assertEq(e.w[i], 0, "reference charges nothing after saturation");
        }
        assertTrue(e.surplus[n - 1] != type(uint256).max, "reference reopens in the next block");
        assertEq(e.w[n - 1], raw.getScope(rawKey.toId()).watermark, "next-block charge = reference");
    }

    function _pair(int128 a, int128 b) internal pure returns (int128[] memory v) {
        v = new int128[](2);
        (v[0], v[1]) = (a, b);
    }

    /// @dev quote() read inside a transaction, before each of its swaps, equals that swap's
    /// charge: the quote sees the transaction's transient running state. Two transactions of
    /// mixed directions (one in every mode without --isolate), relative buffer on, settlement in
    /// token0 so the charge is the W increase exactly; every charge also equals the reference.
    function test_QuoteWithinATransactionEqualsTheCharge() public {
        dm = new DeltaManager();
        ParticipationAwareHook.Params memory p = _params(1e18, 0, LAMBDA_BPS, GAMMA_BPS);
        p.epsilonRelPpb = FINAL_EPS_PPB;
        raw = new ParticipationAwareHook(IPoolManager(address(dm)), IReferenceOracle(address(oracle)), VAULT, p);
        rawKey = PoolKey(Currency.wrap(address(0xA0)), Currency.wrap(address(0xB0)), 3000, 60, IHooks(address(raw)));
        rawId = PoolId.unwrap(rawKey.toId());
        oracle.setPrice(rawId, 1.2e18);
        rawPx = 1.2e18;
        _refInit();
        int128[3][2] memory x0 = [[int128(-10e18), 3e18, -2e18], [int128(-1e18), 0, 4e18]];
        int128[3][2] memory x1 = [[int128(12e18), -3e18, 4e18], [int128(2e18), 1e18, -2e18]];
        int128[] memory charges = new int128[](6);
        uint256 charged;
        for (uint256 t; t < 2; ++t) {
            int128[] memory d0 = new int128[](3);
            int128[] memory d1 = new int128[](3);
            for (uint256 i; i < 3; ++i) {
                (d0[i], d1[i]) = (x0[t][i], x1[t][i]);
                _refPush(d0[i], d1[i], true, rawPx);
            }
            _refEndTx();
            (int128[] memory r, uint256[] memory q) = dm.quoteAndSwapMany(raw, rawKey, d0, d1, true);
            for (uint256 i; i < 3; ++i) {
                assertEq(q[i], uint256(int256(r[i])), "quote inside the transaction = charge");
                charges[3 * t + i] = r[i];
                if (r[i] > 0) ++charged;
            }
        }
        assertGe(charged, 3, "the sequence must charge several swaps");
        RefOut memory e = _refRun(raw);
        for (uint256 i; i < 6; ++i) assertEq(uint256(int256(charges[i])), e.w[i], "charge = reference");
        assertEq(raw.scopeSurplus(rawKey.toId()), e.surplus[5], "block surplus = reference");
    }

    function test_OverflowCumulativeDeltaSaturatesForRestOfBlock() public {
        _rawSetUp();
        _refInit();
        // the transaction's net delta leaves int128 within one transaction
        int128[] memory r = _oneTx(_pair(type(int128).max - 10, 11), _pair(0, 0));
        assertGt(r[0], 0, "first swap charged");
        assertEq(r[1], 0, "cum0 leaves int128: charge 0");
        _assertSaturatedForRestOfBlock();
        assertEq(uint256(int256(r[0])), _refRun(raw).w[0], "first charge = reference");
        _assertReferenceSaturatedFrom(1);
    }

    function test_OverflowNegativeCumulativeDeltaSaturates() public {
        _rawSetUp();
        _refInit();
        int128[] memory r = _oneTx(_pair(0, 0), _pair(type(int128).min + 5, -6));
        assertEq(r[0], 0);
        assertEq(r[1], 0, "cum1 below int128 min");
        _assertSaturatedForRestOfBlock();
        assertEq(_refRun(raw).w[0], 0, "reference: first swap uncharged");
        _assertReferenceSaturatedFrom(1);
    }

    function test_OverflowClosedSurplusSaturates() public {
        // Needs separate transactions (each call one transaction): skipped, and reported as
        // skipped, without --isolate.
        if (!isolated) vm.skip(true);
        _rawSetUp();
        _nextBlock();
        oracle.setPrice(rawId, 2e18);
        // tx 1: bracket = (2^127 - 1) * 2 = 2^128 - 2 (fits); its charge settles in token1
        int128 r1 = dm.swap(raw, rawKey, 0, type(int128).max, false);
        assertGt(r1, 0, "first transaction charged");
        // tx 2: folds tx 1 (closed = 2^128 - 2, fits) and adds a small bracket
        dm.swap(raw, rawKey, 1e18, 0, true);
        // P = tx 1's folded bracket (2^128 - 2) + tx 2's bracket (1e18 at the reference 2)
        assertEq(raw.scopeSurplus(rawKey.toId()), uint256(type(uint128).max) - 1 + 1e18, "tx 1 folded");
        // tx 3: folding tx 2 pushes the closed surplus above 2^128 - 1
        assertEq(dm.swap(raw, rawKey, 1e18, 0, true), 0, "closed surplus above 2^128 - 1: charge 0");
        _assertSaturatedForRestOfBlock();
    }

    function test_OverflowGrossVolumeSaturates() public {
        _rawSetUp();
        // one transaction trading token1 back and forth: its net delta stays in int128 but its
        // gross volume sum |delta1| exceeds 2^128 - 1 on the third swap
        int128[] memory d0 = new int128[](3);
        int128[] memory d1 = new int128[](3);
        (d1[0], d1[1], d1[2]) = (type(int128).max, -type(int128).max, type(int128).max);
        int128[] memory r = _oneTx(d0, d1);
        assertGt(r[0], 0, "first swap charged");
        assertEq(r[2], 0, "gross volume above 2^128 - 1: charge 0");
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
