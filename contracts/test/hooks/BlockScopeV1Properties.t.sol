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
}
