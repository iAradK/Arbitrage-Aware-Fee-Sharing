// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {console2} from "forge-std/console2.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId} from "v4-core/src/types/PoolId.sol";
import {BalanceDelta} from "v4-core/src/types/BalanceDelta.sol";

import {BlockScopedHookTestBase} from "./BlockScopedHookTestBase.sol";
import {HookMiner} from "../utils/HookMiner.sol";
import {ParticipationAwareHook} from "../../src/hooks/ParticipationAwareHook.sol";
import {TxScopedParticipationAwareHook} from "../../src/hooks/TxScopedParticipationAwareHook.sol";
import {SurplusSharingAccounting} from "../../src/SurplusSharingAccounting.sol";
import {MockOracle} from "../../src/mocks/MockOracle.sol";

/// @notice Block-scoped hook through real PoolManager swaps. Run with
/// `forge test --isolate`: every swap below is its own transaction, so state that
/// survives between swaps survives across transactions, as on chain.
contract BlockScopedHookTest is BlockScopedHookTestBase {
    ParticipationAwareHook internal hook;
    PoolKey internal poolA;
    PoolId internal poolAId;
    PoolKey internal poolB;
    PoolId internal poolBId;

    function setUp() public {
        setUpBase();
        hook = _deployHook(_defaultParams());
        (poolA, poolAId) = _pool(IHooks(address(hook)), 1e18);
        (poolB, poolBId) = _pool(IHooks(address(hook)), 1e18, 500);
    }

    // ---------------------------------------------------------------------------------
    // (1) The transaction-scoped hook's vectors, ported to the block-scoped state.
    // ---------------------------------------------------------------------------------

    function test_RunsIsolated() public {
        _assertIsolated();
    }

    /// @dev Boundary economic parameters and swap sizes from dust to large: W after a
    /// real swap equals computeTransfer(...) on the surplus the scope accumulated, with
    /// kappa = K_hat + delta and lambda, gamma on the basis-point grid.
    function testFuzz_BoundaryLimits_ExtremeMarginAndDeltaParameters(
        uint16 lambdaBps_,
        uint16 gammaBps_,
        uint256 kHat,
        uint256 delta,
        uint256 swapAmount
    ) public {
        lambdaBps_ = uint16(bound(lambdaBps_, 0, 10_000));
        gammaBps_ = uint16(bound(gammaBps_, 0, 9_999));
        kHat = bound(kHat, 0, 1e24);
        delta = bound(delta, 0, 1e24);
        swapAmount = bound(swapAmount, 1e6, 1e21);

        ParticipationAwareHook h = _deployHook(_params(kHat, delta, lambdaBps_, gammaBps_));
        (PoolKey memory key, PoolId id) = _pool(IHooks(address(h)), 1e18);
        _swap(key, _exactIn(true, swapAmount));

        ParticipationAwareHook.Scope memory s = h.getScope(id);
        assertEq(s.kappa, kHat + delta, "kappa = K_hat + delta");
        (uint256 expected,) =
            h.computeTransfer(_scopeSurplus(s), kHat, uint256(lambdaBps_) * 1e14, uint256(gammaBps_) * 1e14, delta);
        assertEq(s.watermark, expected, "watermark must equal the pure formula's target");
    }

    function testFuzz_ConstructorRejectsInvalidLambdaGamma(uint16 lambdaBps_, uint16 gammaBps_) public {
        vm.assume(lambdaBps_ > 10_000 || gammaBps_ >= 10_000);
        ParticipationAwareHook.Params memory p = _params(0, 0, lambdaBps_, gammaBps_);
        bytes memory args = abi.encode(manager, oracle, VAULT, p);
        (, bytes32 salt) = HookMiner.find(address(this), AFTER_ONLY_FLAGS, type(ParticipationAwareHook).creationCode, args);
        if (lambdaBps_ > 10_000) vm.expectRevert(SurplusSharingAccounting.InvalidLambda.selector);
        else vm.expectRevert(SurplusSharingAccounting.InvalidGamma.selector);
        new ParticipationAwareHook{salt: salt}(manager, oracle, VAULT, p);
    }

    /// @dev Same-direction fragments, each its own transaction in one block: W never
    /// decreases, across single-tick and tick-crossing sizes.
    function testFuzz_TickCrossing_ChargeMonotoneInSwapSize(uint8 fragmentCount, uint256 seed) public {
        fragmentCount = uint8(bound(fragmentCount, 1, 8));
        uint256 previous;
        for (uint256 i; i < fragmentCount; ++i) {
            _swap(poolA, _exactIn(true, bound(uint256(keccak256(abi.encode(seed, i))), 1e6, 5e20)));
            uint256 w = hook.getScope(poolAId).watermark;
            assertGe(w, previous, "watermark must be non-decreasing across transactions");
            previous = w;
        }
    }

    /// @dev Every invalid oracle mode fails open: the swap succeeds, no scope opens, no
    /// state is written and nothing is charged.
    function testFuzz_InvalidOracleStates_FailOpenNeverBlocksOrOvercharges(
        uint8 modeSeed,
        bool zeroForOne,
        uint256 swapAmount
    ) public {
        MockOracle.Mode mode = MockOracle.Mode(bound(modeSeed, 1, 5));
        swapAmount = bound(swapAmount, 1e6, 5e20);
        oracle.setMode(PoolId.unwrap(poolAId), mode);
        _swap(poolA, _exactIn(zeroForOne, swapAmount));

        ParticipationAwareHook.Scope memory s = hook.getScope(poolAId);
        assertTrue(s.blockNumber != block.number, "no scope opened");
        assertEq(s.watermark, 0);
        assertEq(s.cumulativeDelta0, 0);
        assertEq(s.cumulativeDelta1, 0);
        assertEq(_vault(currency0) + _vault(currency1), 0);
    }

    /// @dev Two pools on one hook, swapped in the same block: a swap on poolB leaves
    /// poolA's scope untouched.
    function testFuzz_CrossPoolIsolationThroughRealSwaps(uint256 amountA, uint256 amountB) public {
        amountA = bound(amountA, 1e6, 5e20);
        amountB = bound(amountB, 1e6, 5e20);
        _swap(poolA, _exactIn(true, amountA));
        ParticipationAwareHook.Scope memory a1 = hook.getScope(poolAId);
        _swap(poolB, _exactIn(false, amountB));
        ParticipationAwareHook.Scope memory a2 = hook.getScope(poolAId);
        assertEq(a2.cumulativeDelta0, a1.cumulativeDelta0);
        assertEq(a2.cumulativeDelta1, a1.cumulativeDelta1);
        assertEq(a2.watermark, a1.watermark);
        assertEq(hook.getScope(poolBId).blockNumber, block.number);
    }

    /// @dev Exact input charges the output token (token1) at the reference 1.2.
    function test_Settlement_ExactInput_ChargesOutputTokenToVault() public {
        oracle.setPrice(PoolId.unwrap(poolAId), 1.2e18);
        BalanceDelta swapperDelta = _swap(poolA, _exactIn(true, 100e18));
        ParticipationAwareHook.Scope memory s = hook.getScope(poolAId);

        uint256 target = _F(hook, s, _scopeSurplus(s));
        uint256 expectedTokens = (target * 1e18) / 1.2e18;
        assertGt(expectedTokens, 0, "scenario must produce a positive charge");
        assertEq(_vault(currency1), expectedTokens, "vault claims");
        assertEq(_vault(currency0), 0, "no charge in the specified token");
        assertEq(int256(swapperDelta.amount1()), int256(s.cumulativeDelta1) - int256(expectedTokens), "output reduced");
        assertEq(int256(swapperDelta.amount0()), int256(s.cumulativeDelta0), "input unchanged");
        assertLe(s.watermark, target, "watermark never exceeds the cumulative target");
        assertGe(uint256(s.watermark) * 1e18, expectedTokens * 1.2e18, "watermark covers the value collected");
    }

    /// @dev Exact output charges the input token (token0, the numeraire): no remainder.
    function test_Settlement_ExactOutput_ChargesInputTokenToVault() public {
        oracle.setPrice(PoolId.unwrap(poolAId), 1.2e18);
        BalanceDelta swapperDelta = _swap(poolA, _exactOut(true, 100e18));
        ParticipationAwareHook.Scope memory s = hook.getScope(poolAId);

        uint256 target = _F(hook, s, _scopeSurplus(s));
        assertGt(target, 0, "scenario must produce a positive charge");
        assertEq(_vault(currency0), target, "vault claims");
        assertEq(_vault(currency1), 0, "no charge in the specified token");
        assertEq(int256(swapperDelta.amount0()), int256(s.cumulativeDelta0) - int256(target), "input increased");
        assertEq(int256(swapperDelta.amount1()), int256(s.cumulativeDelta1), "output unchanged");
        assertEq(s.watermark, target);
    }

    /// @dev Exact-input fragments as separate transactions: the value minted never
    /// exceeds W, and W never exceeds the cumulative target.
    function testFuzz_Settlement_FragmentsNeverOvercollect(uint8 fragmentCount) public {
        fragmentCount = uint8(bound(fragmentCount, 1, 16));
        oracle.setPrice(PoolId.unwrap(poolAId), 1.2e18);
        for (uint256 i; i < fragmentCount; ++i) {
            _swap(poolA, _exactIn(true, 100e18 / fragmentCount));
        }
        ParticipationAwareHook.Scope memory s = hook.getScope(poolAId);
        uint256 minted = _vault(currency1);
        assertGt(minted, 0);
        assertLe(minted * 1.2e18, uint256(s.watermark) * 1e18, "collected value within watermark");
        assertLe(s.watermark, _F(hook, s, _scopeSurplus(s)), "watermark within cumulative target");
    }

    /// @dev The first swap of a block locks reference, kappa, lambda and gamma, and
    /// quote() of a zero delta afterwards is 0.
    function test_ScopeLocksParameters() public {
        oracle.setPrice(PoolId.unwrap(poolAId), 1.2e18);
        _swap(poolA, _exactIn(true, 1e18));
        ParticipationAwareHook.Scope memory s = hook.getScope(poolAId);
        assertEq(s.blockNumber, block.number);
        assertEq(s.referencePriceWad, 1.2e18);
        assertEq(s.kappa, K_HAT + DELTA);
        assertEq(s.lambdaBps, LAMBDA_BPS);
        assertEq(s.gammaBps, GAMMA_BPS);
        assertEq(hook.quote(poolA, 0, 0), 0);
    }

    // ---------------------------------------------------------------------------------
    // (2) Splits across 2/4/8 transactions in one block equal the unsplit charge; the
    //     transaction-scoped contract collects less on the same sequence.
    // ---------------------------------------------------------------------------------

    uint256 internal constant SPLIT_KAPPA = 5e18;
    uint256 internal constant SPLIT_OUT = 80e18; // token1 bought at pool price ~1 against a reference of 1.2

    function test_TxSplit_2_OneSender() public { _txSplit(2, false); }
    function test_TxSplit_4_OneSender() public { _txSplit(4, false); }
    function test_TxSplit_8_OneSender() public { _txSplit(8, false); }
    function test_TxSplit_2_ManySenders() public { _txSplit(2, true); }
    function test_TxSplit_4_ManySenders() public { _txSplit(4, true); }
    function test_TxSplit_8_ManySenders() public { _txSplit(8, true); }

    /// @dev Exact-output zeroForOne swaps charge token0, the numeraire, so the settled
    /// amounts equal the value charges with no conversion remainder. Three pools with
    /// identical liquidity: unsplit (new hook), split (new hook), split (old hook).
    function _txSplit(uint256 n, bool manySenders) internal {
        _assertIsolated();
        // Two instances of the new hook, so the unsplit and split pools differ only in
        // the hook address and price swaps identically.
        ParticipationAwareHook hu = _deployHook(_params(SPLIT_KAPPA, 0, LAMBDA_BPS, GAMMA_BPS));
        ParticipationAwareHook h = _deployHook(_params(SPLIT_KAPPA, 0, LAMBDA_BPS, GAMMA_BPS));
        TxScopedParticipationAwareHook old = _deployTxScopedHook(SPLIT_KAPPA, 0, LAMBDA_BPS, GAMMA_BPS);
        (PoolKey memory unsplitKey, PoolId unsplitId) = _pool(IHooks(address(hu)), 1.2e18);
        (PoolKey memory splitKey, PoolId splitId) = _pool(IHooks(address(h)), 1.2e18);
        (PoolKey memory oldKey,) = _pool(IHooks(address(old)), 1.2e18);

        uint256 v0 = _vault(currency0);
        _swap(unsplitKey, _exactOut(true, SPLIT_OUT));
        uint256 unsplitCharge = _vault(currency0) - v0;
        assertEq(unsplitCharge, hu.getScope(unsplitId).watermark);

        uint256 splitCharge = _runSplit(splitKey, n, manySenders, 0);
        uint256 oldCharge = _runSplit(oldKey, n, manySenders, 100);

        // Theorem 1(a) exactly: the fragments pay F(A) of their own net delta.
        ParticipationAwareHook.Scope memory s = h.getScope(splitId);
        assertEq(s.blockNumber, block.number, "all fragments in one block");
        assertEq(splitCharge, _F(h, s, _scopeSurplus(s)), "sum of charges = F(A) of the net delta");
        assertEq(splitCharge, s.watermark);
        // Against the unsplit swap: the net deltas differ only by the pool's per-swap
        // rounding of the input (at most a few base units per fragment).
        uint256 diff = splitCharge > unsplitCharge ? splitCharge - unsplitCharge : unsplitCharge - splitCharge;
        assertLe(diff, 4 * n, "split charge equals the unsplit charge up to AMM rounding");
        assertLt(oldCharge, splitCharge, "the transaction-scoped contract collects less");
        console2.log(
            string.concat(
                "TXSPLIT,n=", vm.toString(n), manySenders ? ",senders=many" : ",senders=one",
                ",unsplit=", vm.toString(unsplitCharge), ",block_scoped=", vm.toString(splitCharge),
                ",tx_scoped=", vm.toString(oldCharge)
            )
        );
    }

    function _runSplit(PoolKey memory key, uint256 n, bool manySenders, uint256 actorOffset)
        internal
        returns (uint256 charged)
    {
        uint256 v0 = _vault(currency0);
        for (uint256 j; j < n; ++j) {
            IPoolManager.SwapParams memory p = _exactOut(true, SPLIT_OUT / n);
            if (manySenders) _swapAs(_actor(actorOffset + j), key, p);
            else _swap(key, p);
        }
        charged = _vault(currency0) - v0;
    }

    // ---------------------------------------------------------------------------------
    // (3) A new block resets the scope (base-fee re-lock: ParticipationAwareHookFeeTest).
    // ---------------------------------------------------------------------------------

    function test_RollResetsScope() public {
        _assertIsolated();
        oracle.setPrice(PoolId.unwrap(poolAId), 1.2e18);
        _swap(poolA, _exactOut(true, 20e18));
        _swap(poolA, _exactOut(true, 20e18));
        ParticipationAwareHook.Scope memory s1 = hook.getScope(poolAId);
        assertEq(s1.blockNumber, block.number);

        vm.roll(block.number + 1);
        oracle.setPrice(PoolId.unwrap(poolAId), 1.1e18);
        BalanceDelta d = _swap(poolA, _exactOut(true, 20e18));
        ParticipationAwareHook.Scope memory s2 = hook.getScope(poolAId);
        assertEq(s2.blockNumber, block.number);
        assertEq(s2.referencePriceWad, 1.1e18, "reference re-locked");
        assertEq(int256(s2.cumulativeDelta1), int256(d.amount1()), "cumulative delta reset to this swap");
        assertEq(s2.watermark, _F(hook, s2, _scopeSurplus(s2)), "watermark reset and recomputed");
        assertLt(s2.watermark, s1.watermark);
    }
}

/// @notice kappa re-locks at the new block's base fee. Runs WITHOUT --isolate: forge
/// 1.5.1 executes isolated calls with block.basefee = 0 whatever vm.fee or
/// --block-base-fee-per-gas set (static calls still see it), so the base-fee term cannot
/// be exercised under isolation. The scope itself is persistent storage, so a reset
/// still has to come from vm.roll here, as it does under --isolate.
contract ParticipationAwareHookFeeTest is BlockScopedHookTestBase {
    function setUp() public {
        setUpBase();
    }

    function test_NotIsolated() public {
        transientProbe.set();
        require(transientProbe.isSet(), "run this contract without --isolate (isolated calls see basefee 0)");
    }

    function test_RollResetsScopeAndRelocksKappaAtNewBaseFee() public {
        ParticipationAwareHook.Params memory p = _defaultParams();
        p.gasUnits = 200_000;
        p.priorityFeeWei = 0.05 gwei;
        p.gasPriceToken0Wad = 2000e18; // 2000 token0 base units per wei
        ParticipationAwareHook h = _deployHook(p);
        (PoolKey memory key, PoolId id) = _pool(IHooks(address(h)), 1.2e18);

        vm.roll(100);
        vm.fee(10 gwei);
        uint256 kappa10 = K_HAT + DELTA + 200_000 * (10 gwei + 0.05 gwei) * 2000;
        _swap(key, _exactOut(true, 20e18));
        ParticipationAwareHook.Scope memory s1 = h.getScope(id);
        assertEq(s1.blockNumber, 100);
        assertEq(s1.kappa, kappa10, "kappa locked at the block's base fee");

        // A base fee change within the block (not possible on chain, forced here) does
        // not move the locked kappa; the second transaction accumulates on the first.
        vm.fee(50 gwei);
        _swap(key, _exactOut(true, 20e18));
        ParticipationAwareHook.Scope memory s2 = h.getScope(id);
        assertEq(s2.kappa, kappa10, "kappa stays locked within the block");
        assertLt(s2.cumulativeDelta0, s1.cumulativeDelta0, "second transaction accumulates");
        assertGe(s2.watermark, s1.watermark);

        // Next block: fresh scope, kappa re-locked at 50 gwei, B holds only this swap.
        vm.roll(101);
        uint256 kappa50 = K_HAT + DELTA + 200_000 * (50 gwei + 0.05 gwei) * 2000;
        assertEq(h.currentKappa(1.2e18), kappa50);
        BalanceDelta d = _swap(key, _exactOut(true, 20e18));
        ParticipationAwareHook.Scope memory s3 = h.getScope(id);
        assertEq(s3.blockNumber, 101);
        assertEq(s3.kappa, kappa50, "kappa re-locked at the new base fee");
        assertGt(s3.kappa, s1.kappa);
        assertEq(int256(s3.cumulativeDelta1), int256(d.amount1()), "cumulative delta reset to this swap");
        assertEq(s3.watermark, _F(h, s3, _scopeSurplus(s3)), "watermark reset and recomputed");
        assertLt(s3.watermark, s2.watermark);
    }

    /// @dev With gasPriceToken0Wad = 0 the gas term is priced at the locked reference.
    function test_KappaUsesReferenceWhenNoGasPriceConfigured() public {
        ParticipationAwareHook.Params memory p = _defaultParams();
        p.gasUnits = 100_000;
        ParticipationAwareHook h = _deployHook(p);
        (PoolKey memory key, PoolId id) = _pool(IHooks(address(h)), 1.5e18);
        vm.fee(3 gwei);
        _swap(key, _exactIn(true, 1e18));
        assertEq(h.getScope(id).kappa, K_HAT + DELTA + (100_000 * 3 gwei * 1.5e18) / 1e18);
    }
}
