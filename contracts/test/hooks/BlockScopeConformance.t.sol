// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "v4-core/src/types/PoolId.sol";
import {Currency} from "v4-core/src/types/Currency.sol";

import {ParticipationAwareHook} from "../../src/hooks/ParticipationAwareHook.sol";
import {IReferenceOracle} from "../../src/interfaces/IReferenceOracle.sol";
import {MockOracle} from "../../src/mocks/MockOracle.sol";
import {DeltaManager} from "./DeltaManager.sol";
import {TransientProbe} from "./BlockScopedHookTestBase.sol";

/// @notice The 48 E7 multi-swap sequences (validation months) replayed through the final hook
/// (block scope V1 with per-transaction clipping, relative proportional buffer) in three modes;
/// every marginal charge is compared with ScopedHookReference(scope="block",
/// accumulation="tx_clip", buffer="rel") via experiments/e7_v1_vectors.py ->
/// results/e7_block_scope/vectors_v1.json:
///   a  all fragments in one transaction
///   b  each fragment its own transaction, one block
///   c  each fragment its own transaction, 2-4 blocks (vm.roll)
/// Run with `forge test --isolate` (asserted). Writes results/e7_block_scope/conformance_counts_v1.json.
contract BlockScopeConformanceTest is Test {
    using PoolIdLibrary for PoolKey;

    string internal constant VECTORS = "../results/e7_block_scope/vectors_v1.json";
    string internal constant COUNTS = "../results/e7_block_scope/conformance_counts_v1.json";
    address internal constant VAULT = address(0x7A017);

    DeltaManager internal mgr;
    MockOracle internal oracle;
    TransientProbe internal probe;
    uint256 internal refWad;
    uint32 internal epsPpb;

    struct Seq {
        uint256 kHat;
        uint16 lambdaBps;
        uint16 gammaBps;
        int128[] d0;
        int128[] d1;
        uint256[] blocksC;
        uint256[][3] expected;
    }

    function setUp() public {
        mgr = new DeltaManager();
        oracle = new MockOracle();
        probe = new TransientProbe();
    }

    function _ints(string memory json, string memory key) internal pure returns (int128[] memory v) {
        string[] memory s = vm.parseJsonStringArray(json, key);
        v = new int128[](s.length);
        for (uint256 j; j < s.length; ++j) {
            v[j] = int128(vm.parseInt(s[j]));
        }
    }

    function _load(string memory json, uint256 i) internal pure returns (Seq memory s) {
        string memory p = string.concat(".sequences[", vm.toString(i), "].");
        s.kHat = vm.parseUint(vm.parseJsonString(json, string.concat(p, "k_hat_wad")));
        s.lambdaBps = uint16(vm.parseJsonUint(json, string.concat(p, "lambda_bps")));
        s.gammaBps = uint16(vm.parseJsonUint(json, string.concat(p, "gamma_bps")));
        s.d0 = _ints(json, string.concat(p, "d0"));
        s.d1 = _ints(json, string.concat(p, "d1"));
        s.blocksC = vm.parseJsonUintArray(json, string.concat(p, "blocks_c"));
        string[3] memory keys = ["expected_a", "expected_b", "expected_c"];
        for (uint256 m; m < 3; ++m) {
            string[] memory e = vm.parseJsonStringArray(json, string.concat(p, keys[m]));
            s.expected[m] = new uint256[](e.length);
            for (uint256 j; j < e.length; ++j) {
                s.expected[m][j] = vm.parseUint(e[j]);
            }
        }
    }

    function _fresh(Seq memory s, uint256 salt) internal returns (ParticipationAwareHook hook, PoolKey memory key) {
        ParticipationAwareHook.Params memory p;
        p.kHatConstant = s.kHat;
        p.lambdaBps = s.lambdaBps;
        p.gammaBps = s.gammaBps;
        p.stalenessThresholdSeconds = 3600;
        p.epsilonRelPpb = epsPpb;
        hook = new ParticipationAwareHook(IPoolManager(address(mgr)), IReferenceOracle(address(oracle)), VAULT, p);
        key = PoolKey(
            Currency.wrap(address(uint160(0x1000 + salt))), Currency.wrap(address(uint160(0x2000 + salt))), 3000, 60, IHooks(address(hook))
        );
        oracle.setPrice(PoolId.unwrap(key.toId()), refWad);
    }

    function _check(uint256[] memory exp, uint256 j, int128 got) internal pure returns (uint256) {
        return (got < 0 || uint256(int256(got)) != exp[j]) ? 1 : 0;
    }

    function test_E7SequencesThreeModes() public {
        probe.set();
        require(!probe.isSet(), "run with forge test --isolate");
        string memory json = vm.readFile(VECTORS);
        uint256 n = vm.parseJsonUint(json, ".n_sequences");
        refWad = vm.parseUint(vm.parseJsonString(json, ".reference_wad"));
        epsPpb = uint32(vm.parseJsonUint(json, ".eps_rel_ppb"));
        uint256[3] memory mismatch;
        uint256[3] memory charged;
        uint256 fragments;
        uint256 blk = vm.getBlockNumber(); // not block.number: via_ir may re-read it after vm.roll
        for (uint256 i; i < n; ++i) {
            Seq memory s = _load(json, i);
            fragments += s.d0.length;

            // a: one transaction in one block
            blk += 10;
            vm.roll(blk);
            (ParticipationAwareHook ha, PoolKey memory ka) = _fresh(s, 3 * i);
            int128[] memory got = mgr.swapMany(ha, ka, s.d0, s.d1, true);
            for (uint256 j; j < s.d0.length; ++j) {
                mismatch[0] += _check(s.expected[0], j, got[j]);
                if (got[j] > 0) ++charged[0];
            }

            // b: one transaction per fragment, one block
            blk += 10;
            vm.roll(blk);
            (ParticipationAwareHook hb, PoolKey memory kb) = _fresh(s, 3 * i + 1);
            for (uint256 j; j < s.d0.length; ++j) {
                int128 r = mgr.swap(hb, kb, s.d0[j], s.d1[j], true);
                mismatch[1] += _check(s.expected[1], j, r);
                if (r > 0) ++charged[1];
            }

            // c: one transaction per fragment, blocks_c[j] blocks after the first
            blk += 10;
            vm.roll(blk);
            (ParticipationAwareHook hc, PoolKey memory kc) = _fresh(s, 3 * i + 2);
            uint256 start = blk;
            for (uint256 j; j < s.d0.length; ++j) {
                if (start + s.blocksC[j] != vm.getBlockNumber()) {
                    vm.roll(start + s.blocksC[j]);
                    vm.warp(block.timestamp + 12);
                }
                int128 r = mgr.swap(hc, kc, s.d0[j], s.d1[j], true);
                mismatch[2] += _check(s.expected[2], j, r);
                if (r > 0) ++charged[2];
            }
            blk = vm.getBlockNumber();
        }
        string memory o = "counts";
        vm.serializeUint(o, "sequences", n);
        vm.serializeUint(o, "fragments_per_mode", fragments);
        vm.serializeUint(o, "charged_a", charged[0]);
        vm.serializeUint(o, "charged_b", charged[1]);
        vm.serializeUint(o, "charged_c", charged[2]);
        vm.serializeUint(o, "mismatch_a_one_tx", mismatch[0]);
        vm.serializeUint(o, "mismatch_b_tx_per_fragment", mismatch[1]);
        string memory out = vm.serializeUint(o, "mismatch_c_multi_block", mismatch[2]);
        vm.writeJson(out, COUNTS);
        console.log("fragments per mode", fragments);
        console.log("mismatches a/b/c", mismatch[0], mismatch[1], mismatch[2]);
        console.log("charged a/b/c", charged[0], charged[1], charged[2]);
        assertEq(mismatch[0] + mismatch[1] + mismatch[2], 0, "contract differs from ScopedHookReference");
    }
}
