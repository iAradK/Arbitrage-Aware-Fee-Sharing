// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {SurplusSharingAccounting} from "../src/SurplusSharingAccounting.sol";
import {CumulativeSurplusAccounting} from "../src/CumulativeSurplusAccounting.sol";

/// @dev Runs one transaction-scoped sequence (initialize, then every fragment) inside a
/// single call, so the sequence shares one transaction also under `forge test --isolate`.
contract E7SequenceRunner {
    function run(
        CumulativeSurplusAccounting cum,
        bytes32 poolId,
        uint256[4] calldata kLambdaGammaDelta,
        int256[] calldata deltas
    ) external returns (uint256[] memory charges) {
        cum.initializePool(
            poolId, 1e18, 1e18, kLambdaGammaDelta[0], kLambdaGammaDelta[1], kLambdaGammaDelta[2], kLambdaGammaDelta[3], 1
        );
        charges = new uint256[](deltas.length);
        for (uint256 j; j < deltas.length; ++j) {
            (,, charges[j]) = cum.processCallback(poolId, deltas[j], 0);
        }
    }
}

/// @notice E7 conformance: asserts EXACT equality between the contracts and the Python integer reference
/// (results/e7/vectors.json), counts outcome classes, and records computeTransfer / callback gas.
contract E7ConformanceTest is Test {
    uint256 internal constant WAD = 1e18;

    SurplusSharingAccounting internal core;
    CumulativeSurplusAccounting internal cum;
    E7SequenceRunner internal runner;

    function setUp() public {
        core = new SurplusSharingAccounting();
        cum = new CumulativeSurplusAccounting();
        runner = new E7SequenceRunner();
    }

    function _u(string memory json, string memory key) internal view returns (uint256) {
        return vm.parseUint(vm.parseJsonString(json, key));
    }

    function _cls(uint256 transfer, bool capBinds) internal pure returns (string memory) {
        if (transfer == 0) return "zero";
        return capBinds ? "cap" : "prop";
    }

    function test_Conformance() public {
        string memory json = vm.readFile(vm.envOr("E7_VECTORS", string("../results/e7/vectors.json")));
        uint256 nA = vm.parseJsonUint(json, ".n_atomic");
        uint256 nS = vm.parseJsonUint(json, ".n_sequences");

        uint256 prop; uint256 cap; uint256 zero; uint256 mismatches; uint256 classMismatch;
        uint256 gMin = type(uint256).max; uint256 gMax; uint256 gSum;
        for (uint256 i; i < nA; ++i) {
            string memory p = string.concat(".atomic[", vm.toString(i), "].");
            uint256 g0 = gasleft();
            (uint256 t, bool capBinds) = core.computeTransfer(
                _u(json, string.concat(p, "surplus_hat_wad")),
                _u(json, string.concat(p, "k_hat_wad")),
                _u(json, string.concat(p, "lambda_wad")),
                _u(json, string.concat(p, "gamma_wad")),
                _u(json, string.concat(p, "delta_wad"))
            );
            uint256 g = g0 - gasleft();
            // gas excludes the JSON parsing above only approximately; recorded via a dedicated tight loop below
            g;
            if (t != _u(json, string.concat(p, "expected_transfer_wad"))) ++mismatches;
            string memory c = _cls(t, capBinds);
            if (keccak256(bytes(c)) != keccak256(bytes(vm.parseJsonString(json, string.concat(p, "class"))))) ++classMismatch;
            if (t == 0) ++zero; else if (capBinds) ++cap; else ++prop;
        }

        // tight gas loop over the first vectors (arguments pre-parsed so parsing is not measured)
        uint256 nG = nA < 32 ? nA : 32;
        for (uint256 i; i < nG; ++i) {
            string memory p = string.concat(".atomic[", vm.toString(i), "].");
            uint256 s = _u(json, string.concat(p, "surplus_hat_wad"));
            uint256 k = _u(json, string.concat(p, "k_hat_wad"));
            uint256 l = _u(json, string.concat(p, "lambda_wad"));
            uint256 gm = _u(json, string.concat(p, "gamma_wad"));
            uint256 d = _u(json, string.concat(p, "delta_wad"));
            uint256 g0 = gasleft();
            core.computeTransfer(s, k, l, gm, d);
            uint256 g = g0 - gasleft();
            if (g < gMin) gMin = g;
            if (g > gMax) gMax = g;
            gSum += g;
        }

        uint256 seqFragments; uint256 seqMismatch; uint256 nonNegative = 1;
        for (uint256 i; i < nS; ++i) {
            string memory p = string.concat(".sequences[", vm.toString(i), "].");
            bytes32 poolId = keccak256(abi.encode("e7-seq", i));
            uint256[4] memory params = [
                _u(json, string.concat(p, "k_hat_wad")),
                _u(json, string.concat(p, "lambda_wad")),
                _u(json, string.concat(p, "gamma_wad")),
                _u(json, string.concat(p, "delta_wad"))
            ];
            string[] memory deltaStrings = vm.parseJsonStringArray(json, string.concat(p, "deltas"));
            string[] memory exp = vm.parseJsonStringArray(json, string.concat(p, "expected_charges"));
            int256[] memory deltas = new int256[](deltaStrings.length);
            for (uint256 j; j < deltas.length; ++j) {
                deltas[j] = vm.parseInt(deltaStrings[j]);
            }
            uint256[] memory charges = runner.run(cum, poolId, params, deltas);
            for (uint256 j; j < deltas.length; ++j) {
                if (charges[j] != vm.parseUint(exp[j])) ++seqMismatch;
                ++seqFragments;
            }
        }

        string memory out = string.concat(
            "{\"n_atomic\":", vm.toString(nA),
            ",\"proportional\":", vm.toString(prop),
            ",\"cap\":", vm.toString(cap),
            ",\"zero\":", vm.toString(zero),
            ",\"atomic_mismatches\":", vm.toString(mismatches),
            ",\"class_mismatches\":", vm.toString(classMismatch),
            ",\"n_sequences\":", vm.toString(nS),
            ",\"sequence_fragments\":", vm.toString(seqFragments),
            ",\"sequence_mismatches\":", vm.toString(seqMismatch),
            ",\"computeTransfer_gas_min\":", vm.toString(gMin),
            ",\"computeTransfer_gas_max\":", vm.toString(gMax),
            ",\"computeTransfer_gas_mean\":", vm.toString(nG == 0 ? 0 : gSum / nG),
            "}"
        );
        // Written only when E7_COUNTS is set (experiments/e7_solidity_conformance.py sets it), so a
        // plain test run never overwrites a result file.
        string memory countsPath = vm.envOr("E7_COUNTS", string(""));
        if (bytes(countsPath).length != 0) vm.writeFile(countsPath, out);
        nonNegative;

        assertEq(mismatches, 0, "atomic transfer mismatch");
        assertEq(classMismatch, 0, "outcome class mismatch");
        assertEq(seqMismatch, 0, "sequence charge mismatch");
        assertEq(prop + cap + zero, nA);
    }
}
