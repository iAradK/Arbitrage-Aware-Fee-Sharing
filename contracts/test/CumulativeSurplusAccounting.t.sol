// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {CumulativeSurplusAccounting} from "../src/CumulativeSurplusAccounting.sol";

contract AccountingRouter {
    function process(
        CumulativeSurplusAccounting accounting,
        bytes32 poolId,
        int256 delta0Wad,
        int256 delta1Wad
    ) external returns (uint256, uint256, uint256) {
        return accounting.processCallback(poolId, delta0Wad, delta1Wad);
    }

    function processAndRevert(
        CumulativeSurplusAccounting accounting,
        bytes32 poolId,
        int256 delta0Wad,
        int256 delta1Wad
    ) external {
        accounting.processCallback(poolId, delta0Wad, delta1Wad);
        revert("router revert");
    }
}

contract CumulativeSurplusAccountingTest is Test {
    uint256 internal constant WAD = 1e18;
    uint256 internal constant K_HAT = 40e18;
    uint256 internal constant LAMBDA = 0.95e18;
    uint256 internal constant GAMMA = 0.05e18;
    uint256 internal constant DELTA = 5e18;

    CumulativeSurplusAccounting internal accounting;
    AccountingRouter internal routerA;
    AccountingRouter internal routerB;

    function setUp() public {
        accounting = new CumulativeSurplusAccounting();
        routerA = new AccountingRouter();
        routerB = new AccountingRouter();
    }

    function test_MonotoneSplitInvarianceEqualPartitions() public {
        uint256[5] memory counts = [uint256(1), 2, 4, 8, 16];
        uint256 expected = _canonicalTransfer(100e18);

        for (uint256 index; index < counts.length; ++index) {
            uint256 count = counts[index];
            bytes32 poolId = keccak256(
                abi.encode("equal-partition", count)
            );
            _initialize(poolId, K_HAT, LAMBDA, GAMMA, DELTA, 1);

            uint256 totalCharge;
            int256 fragment = int256(100e18 / count);
            for (uint256 j; j < count; ++j) {
                (, , uint256 charge) = accounting.processCallback(
                    poolId,
                    fragment,
                    0
                );
                totalCharge += charge;
            }

            assertEq(totalCharge, expected, vm.toString(count));
            assertEq(_watermark(poolId), expected);
        }
    }

    function test_MonotoneSplitInvarianceUnequalPartition() public {
        bytes32 poolId = keccak256("unequal-partition");
        _initialize(poolId, K_HAT, LAMBDA, GAMMA, DELTA, 1);

        uint256 totalCharge;
        int256[4] memory fragments = [
            int256(7e18),
            int256(13e18),
            int256(29e18),
            int256(51e18)
        ];
        for (uint256 i; i < fragments.length; ++i) {
            (, , uint256 charge) = accounting.processCallback(
                poolId,
                fragments[i],
                0
            );
            totalCharge += charge;
        }

        assertEq(totalCharge, _canonicalTransfer(100e18));
    }

    function test_ReversalCannotReduceWatermark() public {
        bytes32 poolId = keccak256("reversal");
        _initialize(poolId, K_HAT, LAMBDA, GAMMA, DELTA, 1);

        (, uint256 peakTarget, uint256 firstCharge) = accounting.processCallback(
            poolId,
            100e18,
            0
        );
        (uint256 finalSurplus, uint256 finalTarget, uint256 secondCharge) = accounting
            .processCallback(poolId, -80e18, 0);

        assertEq(firstCharge, peakTarget);
        assertEq(finalSurplus, 20e18);
        assertEq(finalTarget, 0);
        assertEq(secondCharge, 0);
        assertEq(_watermark(poolId), peakTarget);
    }

    function test_StateIsSharedAcrossRoutersWithinTransaction() public {
        bytes32 poolId = keccak256("multiple-routers");
        _initialize(poolId, K_HAT, LAMBDA, GAMMA, DELTA, 1);

        (, , uint256 firstCharge) = routerA.process(
            accounting,
            poolId,
            30e18,
            0
        );
        (, , uint256 secondCharge) = routerB.process(
            accounting,
            poolId,
            70e18,
            0
        );

        assertEq(firstCharge + secondCharge, _canonicalTransfer(100e18));
    }

    function test_PoolIdsRemainIsolated() public {
        bytes32 firstPool = keccak256("pool-a");
        bytes32 secondPool = keccak256("pool-b");
        _initialize(firstPool, 40e18, LAMBDA, GAMMA, DELTA, 1);
        _initialize(secondPool, 10e18, LAMBDA, GAMMA, DELTA, -1);

        (, , uint256 firstCharge) = accounting.processCallback(
            firstPool,
            100e18,
            0
        );
        (, , uint256 secondCharge) = accounting.processCallback(
            secondPool,
            100e18,
            0
        );

        assertEq(firstCharge, 52_250_000_000_000_000_000);
        assertEq(secondCharge, 80_750_000_000_000_000_000);
        assertEq(_watermark(firstPool), firstCharge);
        assertEq(_watermark(secondPool), secondCharge);
    }

    function test_InitializationLocksParameters() public {
        bytes32 poolId = keccak256("parameter-lock");
        bool initialized = _initialize(
            poolId,
            K_HAT,
            LAMBDA,
            GAMMA,
            DELTA,
            1
        );
        bool initializedAgain = accounting.initializePool(
            poolId,
            2e18,
            3e18,
            0,
            0,
            0,
            0,
            -1
        );

        CumulativeSurplusAccounting.TransientState memory state = accounting
            .getTransientState(poolId);

        assertTrue(initialized);
        assertFalse(initializedAgain);
        assertEq(state.status, 2);
        assertEq(int256(state.initialDirection), 1);
        assertEq(state.referencePrice0Wad, WAD);
        assertEq(state.referencePrice1Wad, WAD);
        assertEq(state.executionMarginHat, K_HAT);
        assertEq(state.lambdaWad, LAMBDA);
        assertEq(state.gammaWad, GAMMA);
        assertEq(state.delta, DELTA);
        assertEq(state.cumulativeDelta0Wad, 0);
        assertEq(state.cumulativeDelta1Wad, 0);
        assertEq(state.watermark, 0);
    }

    function test_NoInitialDiscrepancyDisablesCharging() public {
        bytes32 poolId = keccak256("no-discrepancy");
        _initialize(poolId, K_HAT, LAMBDA, GAMMA, DELTA, 0);

        (uint256 surplus, uint256 target, uint256 charge) = accounting
            .processCallback(poolId, 100e18, 0);

        assertEq(surplus, 0);
        assertEq(target, 0);
        assertEq(charge, 0);
    }

    function test_RevertWhenPoolIsUninitialized() public {
        vm.expectRevert(
            CumulativeSurplusAccounting.PoolNotInitialized.selector
        );
        accounting.processCallback(keccak256("missing"), 1e18, 0);
    }

    function test_CumulativeRoundingDoesNotCreateFragmentDiscount() public {
        bytes32 poolId = keccak256("rounding");
        uint256 lambdaWad = 333_333_333_333_333_333;
        _initialize(poolId, 0, lambdaWad, 0, 0, 1);

        (, , uint256 firstCharge) = accounting.processCallback(
            poolId,
            3,
            0
        );
        (, , uint256 secondCharge) = accounting.processCallback(
            poolId,
            4,
            0
        );
        (uint256 unsplit,) = accounting.computeTransfer(
            7,
            0,
            lambdaWad,
            0,
            0
        );

        assertEq(firstCharge + secondCharge, unsplit);
        assertEq(_watermark(poolId), unsplit);
    }

    function test_RevertingSubcallRollsBackTransientChanges() public {
        bytes32 poolId = keccak256("reverting-router");
        _initialize(poolId, K_HAT, LAMBDA, GAMMA, DELTA, 1);

        (bool success,) = address(routerA).call(
            abi.encodeCall(
                AccountingRouter.processAndRevert,
                (accounting, poolId, int256(100e18), int256(0))
            )
        );
        assertFalse(success);
        assertEq(_watermark(poolId), 0);

        (, , uint256 charge) = accounting.processCallback(poolId, 100e18, 0);
        assertEq(charge, _canonicalTransfer(100e18));
    }

    function test_GasBenchmarkCumulative() public {
        string memory output = vm.envOr(
            "CUMULATIVE_GAS_OUTPUT",
            string("results/cumulative_solidity_gas.csv")
        );
        vm.writeFile(
            output,
            "fragments,fragment_index,phase,gas_used,marginal_charge_wad,cumulative_target_wad\n"
        );

        uint256[5] memory counts = [uint256(1), 2, 4, 8, 16];
        for (uint256 i; i < counts.length; ++i) {
            uint256 count = counts[i];
            bytes32 poolId = keccak256(abi.encode("gas", count));

            uint256 gasBefore = gasleft();
            _initialize(poolId, K_HAT, LAMBDA, GAMMA, DELTA, 1);
            uint256 initializationGas = gasBefore - gasleft();
            _writeGasRow(output, count, 0, "initialize", initializationGas, 0, 0);

            int256 fragment = int256(100e18 / count);
            for (uint256 j; j < count; ++j) {
                gasBefore = gasleft();
                (, uint256 target, uint256 charge) = accounting.processCallback(
                    poolId,
                    fragment,
                    0
                );
                uint256 callbackGas = gasBefore - gasleft();
                _writeGasRow(
                    output,
                    count,
                    j + 1,
                    "callback",
                    callbackGas,
                    charge,
                    target
                );
            }
        }
    }

    function _initialize(
        bytes32 poolId,
        uint256 margin,
        uint256 lambdaWad,
        uint256 gammaWad,
        uint256 delta,
        int8 direction
    ) internal returns (bool) {
        return
            accounting.initializePool(
                poolId,
                WAD,
                WAD,
                margin,
                lambdaWad,
                gammaWad,
                delta,
                direction
            );
    }

    function _canonicalTransfer(
        uint256 surplus
    ) internal view returns (uint256 transfer) {
        (transfer,) = accounting.computeTransfer(
            surplus,
            K_HAT,
            LAMBDA,
            GAMMA,
            DELTA
        );
    }

    function _watermark(bytes32 poolId) internal view returns (uint256 value) {
        CumulativeSurplusAccounting.TransientState memory state = accounting
            .getTransientState(poolId);
        return state.watermark;
    }

    function _writeGasRow(
        string memory output,
        uint256 fragments,
        uint256 fragmentIndex,
        string memory phase,
        uint256 gasUsed,
        uint256 marginalCharge,
        uint256 cumulativeTarget
    ) internal {
        vm.writeLine(
            output,
            string.concat(
                vm.toString(fragments),
                ",",
                vm.toString(fragmentIndex),
                ",",
                phase,
                ",",
                vm.toString(gasUsed),
                ",",
                vm.toString(marginalCharge),
                ",",
                vm.toString(cumulativeTarget)
            )
        );
    }
}
