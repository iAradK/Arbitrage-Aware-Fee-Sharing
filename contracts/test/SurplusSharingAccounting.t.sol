// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {SurplusSharingAccounting} from "../src/SurplusSharingAccounting.sol";

contract SurplusSharingAccountingTest is Test {
    uint256 internal constant WAD = 1e18;

    SurplusSharingAccounting internal accounting;

    function setUp() public {
        accounting = new SurplusSharingAccounting();
    }

    function test_CanonicalOrderOfOperations() public view {
        // F(100) = min(80, 0.9 * [100 - 40 - 10]) = 45.
        (uint256 transfer, bool capBinds) = accounting.computeTransfer(
            100 * WAD,
            40 * WAD,
            0.8e18,
            0.1e18,
            10 * WAD
        );

        assertEq(transfer, 45 * WAD);
        assertTrue(capBinds);
    }

    function test_ZeroWhenBufferedMarginIsNonpositive() public view {
        (uint256 transfer, bool capBinds) = accounting.computeTransfer(
            50 * WAD,
            45 * WAD,
            0.95e18,
            0.05e18,
            5 * WAD
        );

        assertEq(transfer, 0);
        assertTrue(capBinds);
    }

    function test_ProportionalTargetCanBind() public view {
        (uint256 transfer, bool capBinds) = accounting.computeTransfer(
            100 * WAD,
            10 * WAD,
            0.25e18,
            0.05e18,
            5 * WAD
        );

        assertEq(transfer, 25 * WAD);
        assertFalse(capBinds);
    }

    function test_LargeInputDoesNotOverflowMulWadDown() public view {
        uint256 surplus = type(uint256).max;
        (uint256 transfer,) = accounting.computeTransfer(
            surplus,
            0,
            WAD,
            0,
            0
        );

        assertEq(transfer, surplus);
    }

    function test_RevertInvalidLambda() public {
        vm.expectRevert(SurplusSharingAccounting.InvalidLambda.selector);
        accounting.computeTransfer(1, 0, WAD + 1, 0, 0);
    }

    function test_RevertInvalidGamma() public {
        vm.expectRevert(SurplusSharingAccounting.InvalidGamma.selector);
        accounting.computeTransfer(1, 0, WAD, WAD, 0);
    }

    function testFuzz_TransferNeverExceedsTargetOrBufferedMargin(
        uint128 surplusRaw,
        uint128 marginRaw,
        uint64 deltaRaw,
        uint64 lambdaRaw,
        uint64 gammaRaw
    ) public view {
        uint256 surplus = uint256(surplusRaw);
        uint256 margin = uint256(marginRaw);
        uint256 delta = uint256(deltaRaw);
        uint256 lambdaWad = bound(uint256(lambdaRaw), 0, WAD);
        uint256 gammaWad = bound(uint256(gammaRaw), 0, WAD - 1);

        (uint256 transfer,) = accounting.computeTransfer(
            surplus,
            margin,
            lambdaWad,
            gammaWad,
            delta
        );

        uint256 target = _mulWadDown(surplus, lambdaWad);
        uint256 bufferedMargin;
        if (surplus > margin && surplus - margin > delta) {
            bufferedMargin = surplus - margin - delta;
        }

        assertLe(transfer, target);
        assertLe(transfer, bufferedMargin);
    }

    function test_VectorFile() public view {
        string memory path = vm.envOr(
            "VECTORS",
            string("results/test_vectors.json")
        );
        string memory json = vm.readFile(path);
        uint256 count = vm.parseJsonUint(json, ".count");

        for (uint256 i; i < count; ++i) {
            string memory root = string.concat(
                ".vectors[",
                vm.toString(i),
                "]"
            );
            uint256 surplus = _readUint(json, root, "surplus_hat_wad");
            uint256 margin = _readUint(
                json,
                root,
                "execution_margin_hat_wad"
            );
            uint256 lambdaWad = _readUint(json, root, "lambda_wad");
            uint256 gammaWad = _readUint(json, root, "gamma_wad");
            uint256 delta = _readUint(json, root, "delta_wad");
            uint256 expected = _readUint(
                json,
                root,
                "expected_transfer_wad"
            );

            (uint256 actual,) = accounting.computeTransfer(
                surplus,
                margin,
                lambdaWad,
                gammaWad,
                delta
            );
            assertEq(actual, expected, string.concat("vector ", vm.toString(i)));
        }
    }

    function test_GasBenchmark() public {
        string memory path = vm.envOr(
            "VECTORS",
            string("results/test_vectors.json")
        );
        string memory output = vm.envOr(
            "GAS_OUTPUT",
            string("results/solidity_gas.csv")
        );
        string memory json = vm.readFile(path);
        uint256 count = vm.parseJsonUint(json, ".count");
        vm.writeFile(
            output,
            "event_id,transfer_solidity_wad,gas_used,cap_binds\n"
        );

        for (uint256 i; i < count; ++i) {
            string memory root = string.concat(
                ".vectors[",
                vm.toString(i),
                "]"
            );
            string memory eventId = vm.parseJsonString(
                json,
                string.concat(root, ".event_id")
            );
            uint256 surplus = _readUint(json, root, "surplus_hat_wad");
            uint256 margin = _readUint(
                json,
                root,
                "execution_margin_hat_wad"
            );
            uint256 lambdaWad = _readUint(json, root, "lambda_wad");
            uint256 gammaWad = _readUint(json, root, "gamma_wad");
            uint256 delta = _readUint(json, root, "delta_wad");

            uint256 gasBefore = gasleft();
            (uint256 transfer, bool capBinds) = accounting.computeTransfer(
                surplus,
                margin,
                lambdaWad,
                gammaWad,
                delta
            );
            uint256 gasUsed = gasBefore - gasleft();

            vm.writeLine(
                output,
                string.concat(
                    eventId,
                    ",",
                    vm.toString(transfer),
                    ",",
                    vm.toString(gasUsed),
                    ",",
                    capBinds ? "1" : "0"
                )
            );
        }
    }

    function _readUint(
        string memory json,
        string memory root,
        string memory field
    ) internal pure returns (uint256) {
        string memory decimal = vm.parseJsonString(
            json,
            string.concat(root, ".", field)
        );
        return vm.parseUint(decimal);
    }

    function _mulWadDown(
        uint256 value,
        uint256 fractionWad
    ) internal pure returns (uint256) {
        return
            (value / WAD) * fractionWad +
            ((value % WAD) * fractionWad) /
            WAD;
    }
}
