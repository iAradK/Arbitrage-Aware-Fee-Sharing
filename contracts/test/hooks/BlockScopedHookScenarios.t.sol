// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId} from "v4-core/src/types/PoolId.sol";
import {BalanceDelta} from "v4-core/src/types/BalanceDelta.sol";

import {BlockScopedHookTestBase} from "./BlockScopedHookTestBase.sol";
import {ParticipationAwareHook} from "../../src/hooks/ParticipationAwareHook.sol";
import {TxScopedParticipationAwareHook} from "../../src/hooks/TxScopedParticipationAwareHook.sol";
import {MockOracle} from "../../src/mocks/MockOracle.sol";

/// @notice Cross-transaction scenarios for the block-scoped hook. Run with
/// `forge test --isolate`. Every swap here settles in token0, the numeraire (exact-output
/// zeroForOne and exact-input oneForZero both leave token0 unspecified), so charges and
/// watermarks are exact integers with no conversion remainder.
contract BlockScopedHookScenariosTest is BlockScopedHookTestBase {
    uint256 internal constant KAPPA = 5e18;

    ParticipationAwareHook internal hook; // pool under test
    ParticipationAwareHook internal twin; // same economics, separate pool: unsplit runs
    TxScopedParticipationAwareHook internal old;
    PoolKey internal pool;
    PoolId internal poolId;
    PoolKey internal twinPool;
    PoolId internal twinPoolId;
    PoolKey internal oldPool;

    function setUp() public {
        setUpBase();
        hook = _deployHook(_params(KAPPA, 0, LAMBDA_BPS, GAMMA_BPS));
        twin = _deployHook(_params(KAPPA, 0, LAMBDA_BPS, GAMMA_BPS));
        old = _deployTxScopedHook(KAPPA, 0, LAMBDA_BPS, GAMMA_BPS);
        (pool, poolId) = _pool(IHooks(address(hook)), 1.2e18);
        (twinPool, twinPoolId) = _pool(IHooks(address(twin)), 1.2e18);
        (oldPool,) = _pool(IHooks(address(old)), 1.2e18);
    }

    /// @dev Core delta of the last swap on the pool. Under --isolate every swap is its own
    /// transaction, so with per-transaction accounting (V1) the scope's current transaction
    /// delta is exactly that swap's delta.
    function _lastDelta(ParticipationAwareHook.Scope memory, ParticipationAwareHook.Scope memory afterSwap_, bool)
        internal
        pure
        returns (int256 d0, int256 d1)
    {
        d0 = int256(afterSwap_.cumulativeDelta0);
        d1 = int256(afterSwap_.cumulativeDelta1);
    }

    // ---------------------------------------------------------------------------------
    // Reversal across transactions (V1): an adverse transaction's negative surplus is clipped
    // at zero, so it neither lowers the block surplus nor refunds anything; W = F(A) after
    // every transaction and the total paid is F(A) of the final block total.
    // ---------------------------------------------------------------------------------

    function test_ReversalAcrossTransactions() public {
        _assertIsolated();
        IPoolManager.SwapParams[5] memory path = [
            _exactOut(true, 40e18), // forward, charged
            _exactIn(false, 30e18), // adverse: its bracket is clipped at zero
            _exactOut(true, 10e18), // recovery: a new transaction, charged on its own bracket
            _exactOut(true, 40e18),
            _exactIn(false, 5e18) // adverse again
        ];
        bool[5] memory adverse = [false, true, false, false, true];
        address[5] memory senders = [_actor(1), _actor(2), _actor(3), _actor(1), _actor(4)];
        _refInit();
        uint256[5] memory r;
        uint256[5] memory A;
        uint256 paid;
        for (uint256 j; j < path.length; ++j) {
            uint256 v0 = _vault(currency0);
            _swapRec(senders[j], pool, path[j], 1.2e18);
            r[j] = _vault(currency0) - v0;
            A[j] = hook.scopeSurplus(poolId);
            paid += r[j];
            assertEq(hook.getScope(poolId).blockNumber, block.number, "one block");
        }
        // expected values from ScopedHookReference(scope="block", accumulation="tx_clip", buffer="rel")
        RefOut memory e = _refRun(hook);
        uint256 expectedTotal;
        for (uint256 j; j < path.length; ++j) {
            assertEq(r[j], e.w[j], "charge = reference");
            assertEq(A[j], e.surplus[j], "block surplus = reference");
            if (j > 0) assertGe(e.surplus[j], e.surplus[j - 1], "the block surplus never falls");
            if (adverse[j]) {
                assertEq(e.surplus[j], e.surplus[j - 1], "an adverse transaction adds nothing");
                assertEq(e.w[j], 0, "and pays nothing");
            }
            expectedTotal += e.w[j];
        }
        assertEq(paid, expectedTotal, "total paid = reference total");
        assertEq(hook.getScope(poolId).watermark, expectedTotal, "W = total collected, no refund");
    }

    // ---------------------------------------------------------------------------------
    // Oracle update mid-block: the reference stays locked until the next block.
    // ---------------------------------------------------------------------------------

    function test_OracleUpdateMidBlockDoesNotMoveLockedReference() public {
        _assertIsolated();
        _refInit();
        _swapRec(address(0), pool, _exactOut(true, 20e18), 1.2e18);
        oracle.setPrice(PoolId.unwrap(poolId), 1.5e18);

        ParticipationAwareHook.Scope memory before = hook.getScope(poolId);
        uint256 snap = vm.snapshotState();
        uint256 v0 = _vault(currency0);
        _swapRec(address(0), pool, _exactOut(true, 20e18), 1.5e18);
        uint256 r = _vault(currency0) - v0;
        ParticipationAwareHook.Scope memory s = hook.getScope(poolId);
        assertEq(s.referencePriceWad, 1.2e18, "reference locked for the block");
        // expected charge from ScopedHookReference (computed before the revert, which also
        // reverts the recorded trace)
        RefOut memory e = _refRun(hook);
        assertEq(r, e.w[1], "charge = reference (locked reference 1.2)");

        // quote() before the swap predicted exactly this charge.
        (int256 d0, int256 d1) = _lastDelta(before, s, false);
        vm.revertToState(snap);
        assertEq(hook.quote(pool, d0, d1), r, "quote = charge of the next swap, mid-scope");

        // Next block: the new price is locked, and quote() of a fresh scope matches too.
        vm.roll(block.number + 1);
        snap = vm.snapshotState();
        v0 = _vault(currency0);
        _swapRec(address(0), pool, _exactOut(true, 20e18), 1.5e18);
        r = _vault(currency0) - v0;
        s = hook.getScope(poolId);
        assertEq(s.referencePriceWad, 1.5e18, "new reference locked in the next block");
        e = _refRun(hook);
        assertEq(r, e.w[1], "charge = reference (new block, reference 1.5)");
        (d0, d1) = _lastDelta(s, s, true);
        vm.revertToState(snap);
        assertEq(hook.quote(pool, d0, d1), r, "quote = charge of the opening swap");
    }

    // ---------------------------------------------------------------------------------
    // Invalid oracle on the first swap, then valid: no scope, then a scope that holds
    // only the later swap.
    // ---------------------------------------------------------------------------------

    function test_InvalidOracleOnFirstSwapThenValid() public {
        _assertIsolated();
        oracle.setMode(PoolId.unwrap(poolId), MockOracle.Mode.ForceStale);
        assertEq(hook.quote(pool, -20e18, 20e18), 0, "quote is 0 while the oracle is invalid");
        uint256 v0 = _vault(currency0);
        _swapAs(_actor(1), pool, _exactOut(true, 40e18));
        assertEq(_vault(currency0), v0, "invalid oracle: charge 0");
        ParticipationAwareHook.Scope memory s0 = hook.getScope(poolId);
        assertTrue(s0.blockNumber != block.number, "no scope opened");
        assertEq(s0.cumulativeDelta0, 0, "the swap is not accumulated");

        oracle.setMode(PoolId.unwrap(poolId), MockOracle.Mode.Normal);
        oracle.setPrice(PoolId.unwrap(poolId), 1.2e18);
        BalanceDelta d = _swapAs(_actor(2), pool, _exactOut(true, 40e18));
        ParticipationAwareHook.Scope memory s = hook.getScope(poolId);
        assertEq(s.blockNumber, block.number, "retry opened the scope");
        assertEq(int256(s.cumulativeDelta1), int256(d.amount1()), "scope holds only the second swap");
        uint256 charge = _vault(currency0) - v0;
        assertGt(charge, 0);
        assertEq(charge, _F(hook, s, _scopeSurplus(s)), "charged as an unsplit swap");
        assertEq(s.watermark, charge);
    }

    // ---------------------------------------------------------------------------------
    // Spillover: a swap pays at most F(a_j) + (1 - gamma) * kappa on top of whatever
    // others did earlier in the block (Proposition 2 with two parts).
    // ---------------------------------------------------------------------------------

    function testFuzz_SpilloverBound(uint256 seed, uint8 count) public {
        _assertIsolated();
        count = uint8(bound(count, 2, 8));
        uint256 spill = (KAPPA * (10_000 - GAMMA_BPS)) / 10_000 + 1;
        _refInit();
        uint256[] memory r = new uint256[](count);
        for (uint256 j; j < count; ++j) {
            uint256 x = uint256(keccak256(abi.encode(seed, j)));
            // Mostly correcting swaps of varied size; one in four is adverse.
            IPoolManager.SwapParams memory p = x % 4 == 0
                ? _exactIn(false, bound(x >> 8, 1e15, 20e18))
                : _exactOut(true, bound(x >> 8, 1e15, 40e18));
            uint256 v0 = _vault(currency0);
            _swapRec(_actor(x % 5), pool, p, 1.2e18);
            r[j] = _vault(currency0) - v0;
        }
        RefOut memory e = _refRun(hook);
        for (uint256 j; j < count; ++j) {
            assertEq(r[j], e.w[j], "charge = reference");
            // a_j from the swap's own core delta (Swap event) at the locked reference 1.2
            RefSwap memory t = refTrace[j];
            int256 own = t.d0 + (t.d1 * int256(uint256(1.2e18))) / 1e18;
            uint256 ownSurplus = own > 0 ? uint256(own) : 0;
            (uint256 fOwn,) = hook.computeTransfer(ownSurplus, KAPPA, uint256(LAMBDA_BPS) * 1e14, uint256(GAMMA_BPS) * 1e14, 0);
            assertLe(r[j], fOwn + spill + 4, "r_j <= F(a_j) + (1 - gamma) * kappa");
        }
    }

    /// @dev The bound is tight: a first swap with surplus exactly kappa pays nothing
    /// (F(kappa) = 0), and a second swap with the same surplus then pays (1 - gamma) kappa,
    /// although F of its own surplus is 0.
    function test_SpilloverBoundIsAttained() public {
        _assertIsolated();
        // Find, by quote(), an exact-output size whose surplus is just above kappa.
        uint256 lo = 1e18;
        uint256 hi = 200e18;
        for (uint256 i; i < 80; ++i) {
            uint256 mid = (lo + hi) / 2;
            int256 out = int256(mid);
            int256 in_ = -int256((mid * 1003) / 1000); // fee 0.3%, negligible impact
            if (hook.quote(pool, in_, out) == 0) lo = mid;
            else hi = mid;
        }
        // The estimate of the input ignores price impact and fee rounding, so the first
        // swap's surplus is kappa up to ~1e14 and its charge is at most that.
        uint256 v0 = _vault(currency0);
        _swapAs(_actor(1), pool, _exactOut(true, lo));
        uint256 r1 = _vault(currency0) - v0;
        assertLe(r1, 1e15, "first swap: surplus ~kappa, pays ~0");
        v0 = _vault(currency0);
        _swapAs(_actor(2), pool, _exactOut(true, lo));
        uint256 r2 = _vault(currency0) - v0;
        uint256 spill = (KAPPA * (10_000 - GAMMA_BPS)) / 10_000;
        assertApproxEqAbs(r2 + r1, spill, 1e15, "second swap pays (1 - gamma) * kappa, less r1");
    }

    // ---------------------------------------------------------------------------------
    // Random splits into at most 8 transactions in one block.
    // ---------------------------------------------------------------------------------

    function testFuzz_RandomTxSplits(uint256 seed, uint8 n, bool manySenders) public {
        _assertIsolated();
        n = uint8(bound(n, 1, 8));
        uint256 total = 60e18;
        uint256[] memory parts = new uint256[](n);
        uint256 rest = total;
        for (uint256 j; j + 1 < n; ++j) {
            uint256 maxPart = rest - (n - j - 1) * 1e15;
            parts[j] = bound(uint256(keccak256(abi.encode(seed, j))), 1e15, maxPart);
            rest -= parts[j];
        }
        parts[n - 1] = rest;

        uint256 v0 = _vault(currency0);
        _swap(twinPool, _exactOut(true, total));
        uint256 unsplit = _vault(currency0) - v0;

        v0 = _vault(currency0);
        for (uint256 j; j < n; ++j) {
            if (manySenders) _swapAs(_actor(j), pool, _exactOut(true, parts[j]));
            else _swap(pool, _exactOut(true, parts[j]));
        }
        uint256 split = _vault(currency0) - v0;

        v0 = _vault(currency0);
        for (uint256 j; j < n; ++j) {
            if (manySenders) _swapAs(_actor(100 + j), oldPool, _exactOut(true, parts[j]));
            else _swap(oldPool, _exactOut(true, parts[j]));
        }
        uint256 txScoped = _vault(currency0) - v0;

        ParticipationAwareHook.Scope memory s = hook.getScope(poolId);
        assertEq(split, _F(hook, s, _scopeSurplus(s)), "sum of charges = F(A) of the net delta");
        uint256 diff = split > unsplit ? split - unsplit : unsplit - split;
        assertLe(diff, 4 * uint256(n), "equal to the unsplit charge up to AMM rounding");
        if (n == 1) assertEq(txScoped, split, "one transaction: both scopes agree");
        else assertLt(txScoped, split, "the transaction-scoped contract collects less");
    }
}
