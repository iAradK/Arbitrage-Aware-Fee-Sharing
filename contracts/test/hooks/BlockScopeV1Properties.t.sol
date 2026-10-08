// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "v4-core/src/types/PoolId.sol";
import {Currency} from "v4-core/src/types/Currency.sol";

import {BlockScopedHookTestBase} from "./BlockScopedHookTestBase.sol";
import {DeltaManager} from "./DeltaManager.sol";
import {ParticipationAwareHook} from "../../src/hooks/ParticipationAwareHook.sol";
import {IReferenceOracle} from "../../src/interfaces/IReferenceOracle.sol";

/// @notice Properties of the final hook (block scope V1, relative buffer), on exact core
/// deltas through DeltaManager (no AMM rounding). Run with `forge test --isolate` (asserted):
/// every DeltaManager call is its own transaction, swapMany is one transaction.
///   1. Splits within a block pay the unsplit charge: inside one transaction exactly; over n
///      transactions within [unsplit - 2n, unsplit] base units (per extra transaction at most
///      one unit from the truncated reference product and one from the buffer's upward
///      rounding, never in the splitter's favour beyond that).
///   2. An unrelated transaction's negative surplus never lowers another transaction's charge:
///      inserting a value-losing transaction anywhere in the block leaves every other
///      transaction's charge (and the block surplus after it) unchanged, and it pays nothing.
///   3. A wash round trip inside one transaction never lowers the charge: a correcting swap X
///      with round-trip legs (r, -r) in the same transaction, in any arrangement (contiguous
///      pairs, nested around X, or X's swap merged with the closing leg), pays at least X's
///      unwashed charge and leaves the same block surplus (the buffer is on the net token1
///      change, so the legs' volume does not count). A round trip that costs the trader l token0
///      base units lowers the charge by at most l + 1, so it never pays.
contract BlockScopeV1PropertiesTest is BlockScopedHookTestBase {
    using PoolIdLibrary for PoolKey;

    uint256 internal constant K = 1e18;
    DeltaManager internal dm;
    uint256 internal salt;

    function setUp() public {
        setUpBase();
        dm = new DeltaManager();
    }

    function _r(uint256 seed, uint256 i) internal pure returns (uint256) {
        return uint256(keccak256(abi.encode(seed, i)));
    }

    function _hook(uint256 ref) internal returns (ParticipationAwareHook h, PoolKey memory key) {
        h = new ParticipationAwareHook(IPoolManager(address(dm)), IReferenceOracle(address(oracle)), VAULT, _finalParams(K));
        ++salt;
        key = PoolKey(Currency.wrap(address(uint160(0x100 + salt))), Currency.wrap(address(uint160(0x200 + salt))), 3000, 60, IHooks(address(h)));
        oracle.setPrice(PoolId.unwrap(key.toId()), ref);
    }

    /// @dev A correcting trade: receives d1 > 0 token1 and pays token0 worth (1 - margin) of it
    /// at the reference, so its surplus is margin * d1 * ref (margin well above eps_rel).
    function _trade(uint256 d1, uint256 ref, uint256 marginBps) internal pure returns (int128 a0, int128 a1) {
        a1 = int128(int256(d1));
        a0 = -int128(int256((d1 * ref / 1e18) * (10_000 - marginBps) / 10_000));
    }

    function testFuzz_SplitsWithinABlockPayTheUnsplitCharge(uint256 seed, uint8 nRaw) public {
        transientProbe.set();
        require(!transientProbe.isSet(), "run with forge test --isolate");
        uint256 n = 2 + uint256(nRaw) % 7; // 2..8 pieces
        uint256 ref = 0.5e18 + _r(seed, 0) % 1.5e18;
        uint256 D1 = 1e21 + _r(seed, 1) % 1e22; // surplus >= 1e21 * 0.5 * 1% = 5e18 > kappa (~1e18)
        (int128 D0, int128 D1i) = _trade(D1, ref, 100 + _r(seed, 2) % 1900);
        // pieces: weights at least 1/(2n), integer parts, the last takes the remainder exactly
        int128[] memory p0 = new int128[](n);
        int128[] memory p1 = new int128[](n);
        uint256 wsum;
        uint256[] memory w = new uint256[](n);
        for (uint256 i; i < n; ++i) {
            w[i] = 1000 + _r(seed, 10 + i) % 1000;
            wsum += w[i];
        }
        int256 r0 = D0;
        int256 r1 = D1i;
        for (uint256 i; i + 1 < n; ++i) {
            p0[i] = int128(int256(D0) * int256(w[i]) / int256(wsum));
            p1[i] = int128(int256(D1i) * int256(w[i]) / int256(wsum));
            r0 -= p0[i];
            r1 -= p1[i];
        }
        p0[n - 1] = int128(r0);
        p1[n - 1] = int128(r1);

        (ParticipationAwareHook hu, PoolKey memory ku) = _hook(ref);
        uint256 unsplit = uint256(int256(dm.swap(hu, ku, D0, D1i, true)));
        assertGt(unsplit, 0, "the unsplit correction is charged");

        (ParticipationAwareHook h1, PoolKey memory k1) = _hook(ref);
        int128[] memory c1 = dm.swapMany(h1, k1, p0, p1, true);
        uint256 oneTx;
        for (uint256 i; i < n; ++i) {
            oneTx += uint256(int256(c1[i]));
        }
        assertEq(oneTx, unsplit, "split inside one transaction: exactly the unsplit charge");

        (ParticipationAwareHook hn, PoolKey memory kn) = _hook(ref);
        uint256 nTx;
        for (uint256 i; i < n; ++i) {
            nTx += uint256(int256(dm.swap(hn, kn, p0[i], p1[i], true)));
        }
        assertLe(nTx, unsplit, "split over transactions never pays more");
        assertGe(nTx + 2 * n, unsplit, "and at most 2 base units less per transaction");
    }

    function testFuzz_UnrelatedNegativeTransactionNeverLowersCharges(uint256 seed, uint8 mRaw) public {
        transientProbe.set();
        require(!transientProbe.isSet(), "run with forge test --isolate");
        uint256 m = 2 + uint256(mRaw) % 5; // 2..6 ordinary transactions
        uint256 ref = 0.5e18 + _r(seed, 0) % 1.5e18;
        int128[] memory t0 = new int128[](m);
        int128[] memory t1 = new int128[](m);
        for (uint256 j; j < m; ++j) {
            (t0[j], t1[j]) = _trade(1e18 + _r(seed, 1 + j) % 5e20, ref, 50 + _r(seed, 20 + j) % 1950);
        }
        // the unrelated transaction loses value: sells token1 for less than it is worth
        uint256 x = 1e18 + _r(seed, 40) % 1e21;
        int128 n0 = int128(int256((x * ref / 1e18) * (10_000 - 200 - _r(seed, 41) % 3000) / 10_000));
        int128 n1 = -int128(int256(x));
        uint256 at = _r(seed, 42) % (m + 1); // inserted before transaction `at` (m = at the end)

        (ParticipationAwareHook ha, PoolKey memory ka) = _hook(ref);
        (ParticipationAwareHook hb, PoolKey memory kb) = _hook(ref);
        PoolId ida = ka.toId();
        PoolId idb = kb.toId();
        for (uint256 j; j <= m; ++j) {
            if (j == at) {
                assertEq(dm.swap(hb, kb, n0, n1, true), 0, "the value-losing transaction pays nothing");
                if (j > 0) assertEq(hb.scopeSurplus(idb), ha.scopeSurplus(ida), "and does not lower the block surplus");
            }
            if (j == m) break;
            int128 ca = dm.swap(ha, ka, t0[j], t1[j], true);
            int128 cb = dm.swap(hb, kb, t0[j], t1[j], true);
            assertEq(cb, ca, "another transaction's charge is unchanged");
            assertEq(hb.scopeSurplus(idb), ha.scopeSurplus(ida), "block surplus unchanged");
        }
        assertEq(hb.getScope(idb).watermark, ha.getScope(ida).watermark, "same total collected");
    }

    /// @dev The swaps of a washed correction X: k round-trip legs r_i = (r0_i, r1_i) and their
    /// closing legs (-r0_i - l_i, -r1_i), sum of l_i = loss. mode 0: contiguous pairs with X
    /// inserted before pair `at` (mod k + 1); mode 1: every opening leg, then X, then the closing
    /// legs in reverse (nested); mode 2: the opening legs, then X merged with every closing leg
    /// into one swap (Q6a's wash).
    function _washed(int128 x0, int128 x1, int128[] memory r0, int128[] memory r1, uint256 loss, uint256 mode, uint256 at)
        internal
        pure
        returns (int128[] memory s0, int128[] memory s1)
    {
        uint256 k = r0.length;
        s0 = new int128[](mode == 2 ? k + 1 : 2 * k + 1);
        s1 = new int128[](s0.length);
        int128[] memory l = new int128[](k);
        for (uint256 i; i < k; ++i) l[i] = int128(int256(i + 1 == k ? loss - (loss / k) * (k - 1) : loss / k));
        uint256 j;
        if (mode == 0) {
            at %= k + 1;
            for (uint256 i; i <= k; ++i) {
                if (i == at) {
                    s0[j] = x0;
                    s1[j] = x1;
                    ++j;
                }
                if (i == k) break;
                s0[j] = r0[i];
                s1[j] = r1[i];
                s0[j + 1] = -r0[i] - l[i];
                s1[j + 1] = -r1[i];
                j += 2;
            }
            return (s0, s1);
        }
        for (uint256 i; i < k; ++i) {
            s0[i] = r0[i];
            s1[i] = r1[i];
        }
        if (mode == 1) {
            s0[k] = x0;
            s1[k] = x1;
            for (uint256 i; i < k; ++i) {
                s0[k + 1 + i] = -r0[k - 1 - i] - l[k - 1 - i];
                s1[k + 1 + i] = -r1[k - 1 - i];
            }
            return (s0, s1);
        }
        int128 c0 = x0;
        int128 c1 = x1;
        for (uint256 i; i < k; ++i) {
            c0 = c0 - r0[i] - l[i];
            c1 = c1 - r1[i];
        }
        s0[k] = c0;
        s1[k] = c1;
    }

    function _washCase(uint256 seed, uint8 kRaw, uint8 modeRaw, bool lossy)
        internal
        returns (uint256 unwashed, uint256 washed, uint256 loss, uint256 aU, uint256 aW)
    {
        uint256 k = 1 + uint256(kRaw) % 3; // 1..3 round trips
        uint256 ref = 0.5e18 + _r(seed, 0) % 1.5e18;
        // margin 1%..20%, well above eps_rel (0.283%): bracket >= 1e21 * 0.5 * 0.7% > kappa (~1e18)
        (int128 x0, int128 x1) = _trade(1e21 + _r(seed, 1) % 1e22, ref, 100 + _r(seed, 2) % 1900);
        int128[] memory r0 = new int128[](k);
        int128[] memory r1 = new int128[](k);
        for (uint256 i; i < k; ++i) {
            // legs in either direction, up to 3x the correction's token1, at a price within +-20% of the reference
            int256 v = int256(_r(seed, 10 + i) % (3 * uint256(int256(x1)) + 1));
            if (_r(seed, 20 + i) % 2 == 0) v = -v;
            r1[i] = int128(v);
            r0[i] = int128(-(v * int256(ref) / 1e18) * int256(8_000 + _r(seed, 30 + i) % 4_001) / 10_000);
        }
        // the round trip's cost: up to 0.5% of X's value, below its buffered bracket (no clipping)
        loss = lossy ? _r(seed, 40) % (uint256(int256(x1)) * ref / 1e18 / 200 + 1) : 0;
        (int128[] memory s0, int128[] memory s1) = _washed(x0, x1, r0, r1, loss, uint256(modeRaw) % 3, _r(seed, 41));

        (ParticipationAwareHook hu, PoolKey memory ku) = _hook(ref);
        unwashed = uint256(int256(dm.swap(hu, ku, x0, x1, true)));
        (ParticipationAwareHook hw, PoolKey memory kw) = _hook(ref);
        int128[] memory c = dm.swapMany(hw, kw, s0, s1, true);
        for (uint256 i; i < c.length; ++i) washed += uint256(int256(c[i]));
        aU = hu.scopeSurplus(ku.toId());
        aW = hw.scopeSurplus(kw.toId());
    }

    function testFuzz_WashRoundTripInsideATransactionNeverLowersTheCharge(uint256 seed, uint8 kRaw, uint8 modeRaw) public {
        transientProbe.set();
        require(!transientProbe.isSet(), "run with forge test --isolate");
        (uint256 unwashed, uint256 washed,, uint256 aU, uint256 aW) = _washCase(seed, kRaw, modeRaw, false);
        assertGt(unwashed, 0, "the unwashed correction is charged");
        assertGe(washed, unwashed, "a round trip inside the transaction never lowers the charge");
        assertEq(aW, aU, "and leaves the block surplus of the net delta");
    }

    function testFuzz_CostlyWashRoundTripNeverPays(uint256 seed, uint8 kRaw, uint8 modeRaw) public {
        transientProbe.set();
        require(!transientProbe.isSet(), "run with forge test --isolate");
        (uint256 unwashed, uint256 washed, uint256 loss, uint256 aU, uint256 aW) = _washCase(seed, kRaw, modeRaw, true);
        assertGt(unwashed, 0, "the unwashed correction is charged");
        assertGe(washed + loss + 1, unwashed, "the charge falls by at most the round trip's cost (+1 unit rounding)");
        assertEq(aW + loss, aU, "the block surplus falls by exactly the cost");
    }
}
