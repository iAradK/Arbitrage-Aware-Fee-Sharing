// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Vm} from "forge-std/Vm.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId} from "v4-core/src/types/PoolId.sol";

import {BlockScopedHookTestBase} from "./BlockScopedHookTestBase.sol";
import {ParticipationAwareHook} from "../../src/hooks/ParticipationAwareHook.sol";
import {MockOracle} from "../../src/mocks/MockOracle.sol";

/// @notice Contract vs reference, to the wei. Each run interleaves real swaps through the
/// PoolManager from up to 8 senders, both directions, exact input and exact output (so both
/// settlement currencies), over 1-4 blocks. The oracle price is updated between blocks and
/// sometimes mid-block, and is sometimes invalid for the first swaps of a block. After the
/// run, the swap trace goes through common.fixedpoint.ScopedHookReference
/// (experiments/block_scope_ffi.py, vm.ffi), and every swap's watermark increase and
/// settled token amount must equal the reference's.
///
/// Needs `--ffi`. Runs both ways:
///   --isolate     every swap is its own transaction; isolated calls see block.basefee = 0
///                 (forge 1.5.1), so kappa varies only through the locked reference
///   no --isolate  one transaction per run, with vm.fee setting a new base fee in every block
/// Each run appends one line to results/e7_block_scope/fuzz_<mode>.csv.
contract BlockScopeFuzzTest is BlockScopedHookTestBase {
    uint256 internal constant K = 1e18;
    uint256 internal constant BUFFER = 0.25e18;
    uint64 internal constant GAS_UNITS = 150_000;
    uint64 internal constant TAU = 5e7;
    bytes32 internal constant SWAP_TOPIC = keccak256("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)");

    ParticipationAwareHook internal hook;
    PoolKey internal pool;
    PoolId internal poolId;
    address[8] internal actors;
    bool internal isolated;
    string internal logPath;

    struct Rec {
        uint256 blk;
        uint256 price;
        uint256 basefee;
        int256 d0;
        int256 d1;
        bool settle0;
        uint256 wInc;
        uint256 token;
    }

    function setUp() public {
        setUpBase();
        ParticipationAwareHook.Params memory p = _params(K, BUFFER, LAMBDA_BPS, GAMMA_BPS);
        p.gasUnits = GAS_UNITS;
        p.priorityFeeWei = TAU; // gasPriceToken0Wad = 0: the gas cost is priced at the locked reference
        hook = _deployHook(p);
        (pool, poolId) = _pool(IHooks(address(hook)), 1e18);
        for (uint256 i; i < 8; ++i) {
            actors[i] = _actor(i);
        }
        transientProbe.set();
        isolated = !transientProbe.isSet();
        logPath = isolated ? "../results/e7_block_scope/fuzz_isolate.csv" : "../results/e7_block_scope/fuzz_no_isolate.csv";
        if (vm.exists(logPath)) vm.removeFile(logPath);
        vm.writeLine(logPath, "swaps,blocks,senders,charged,oracle_invalid_swaps,mid_block_updates,mismatches");
    }

    function _r(uint256 seed, uint256 i) internal pure returns (uint256) {
        return uint256(keccak256(abi.encode(seed, i)));
    }

    function _coreDelta() internal returns (int256 d0, int256 d1) {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(manager) && logs[i].topics[0] == SWAP_TOPIC) {
                (int128 a0, int128 a1,,,,) = abi.decode(logs[i].data, (int128, int128, uint160, uint128, int24, uint24));
                return (a0, a1);
            }
        }
        revert("no Swap event");
    }

    function _vaults() internal view returns (uint256) {
        return _vault(currency0) + _vault(currency1);
    }

    /// forge-config: default.fuzz.runs = 2000
    function testFuzz_ContractMatchesReference(uint256 seed) public {
        bytes32 id = PoolId.unwrap(poolId);
        uint256 ctr;
        uint256 nBlocks = 1 + _r(seed, ctr++) % 4;
        uint256 nSenders = 1 + _r(seed, ctr++) % 8;
        Rec[] memory recs = new Rec[](16);
        uint256[3] memory stats; // oracle-invalid swaps, mid-block updates, charged swaps
        uint256 n;
        for (uint256 b; b < nBlocks; ++b) {
            vm.roll(block.number + 1 + _r(seed, ctr++) % 3);
            vm.warp(block.timestamp + 12);
            if (!isolated) vm.fee(1 gwei + _r(seed, ctr++) % 200 gwei);
            uint256 price = 0.8e18 + _r(seed, ctr++) % 0.45e18; // oracle update between blocks
            oracle.setPrice(id, price);
            oracle.setMode(id, MockOracle.Mode.Normal);
            uint256 swaps = 1 + _r(seed, ctr++) % 4;
            // with probability 1/5 the oracle is invalid for the first `invalidFor` swaps, then valid again
            uint256 invalidFor = _r(seed, ctr++) % 5 == 0 ? 1 + _r(seed, ctr++) % swaps : 0;
            if (invalidFor != 0) oracle.setMode(id, MockOracle.Mode(1 + _r(seed, ctr++) % 5));
            for (uint256 s; s < swaps; ++s) {
                if (invalidFor != 0 && s == invalidFor) oracle.setMode(id, MockOracle.Mode.Normal);
                if (s != 0 && _r(seed, ctr++) % 4 == 0) {
                    price = 0.8e18 + _r(seed, ctr++) % 0.45e18; // mid-block update
                    oracle.setPrice(id, price);
                    ++stats[1];
                }
                bool valid = invalidFor == 0 || s >= invalidFor;
                if (!valid) ++stats[0];
                bool zeroForOne = _r(seed, ctr++) % 2 == 0;
                bool exactIn = _r(seed, ctr++) % 2 == 0;
                uint256 amount = 1e17 + _r(seed, ctr++) % 5e19;
                address who = actors[_r(seed, ctr++) % nSenders];

                ParticipationAwareHook.Scope memory before = hook.getScope(poolId);
                uint256 v = _vaults();
                vm.recordLogs();
                _swapAs(who, pool, exactIn ? _exactIn(zeroForOne, amount) : _exactOut(zeroForOne, amount));
                (int256 d0, int256 d1) = _coreDelta();
                ParticipationAwareHook.Scope memory afterSwap_ = hook.getScope(poolId);

                Rec memory rec = recs[n++];
                rec.blk = block.number;
                rec.price = valid ? price : 0;
                rec.basefee = isolated ? 0 : block.basefee;
                (rec.d0, rec.d1) = (d0, d1);
                rec.settle0 = exactIn != zeroForOne; // the unspecified currency is token0
                rec.wInc = afterSwap_.blockNumber != before.blockNumber
                    ? afterSwap_.watermark
                    : afterSwap_.watermark - before.watermark;
                rec.token = _vaults() - v;
                if (rec.wInc != 0) ++stats[2];
            }
        }

        string[] memory cmd = new string[](9 + 6 * n);
        cmd[0] = "python3";
        cmd[1] = "../experiments/block_scope_ffi.py";
        cmd[2] = vm.toString(K);
        cmd[3] = vm.toString(BUFFER);
        cmd[4] = vm.toString(uint256(LAMBDA_BPS));
        cmd[5] = vm.toString(uint256(GAMMA_BPS));
        cmd[6] = vm.toString(uint256(GAS_UNITS));
        cmd[7] = vm.toString(uint256(TAU));
        cmd[8] = "0";
        for (uint256 i; i < n; ++i) {
            Rec memory rec = recs[i];
            cmd[9 + 6 * i] = vm.toString(rec.blk);
            cmd[10 + 6 * i] = vm.toString(rec.price);
            cmd[11 + 6 * i] = vm.toString(rec.basefee);
            cmd[12 + 6 * i] = vm.toString(rec.d0);
            cmd[13 + 6 * i] = vm.toString(rec.d1);
            cmd[14 + 6 * i] = rec.settle0 ? "1" : "0";
        }
        uint256[] memory exp = abi.decode(vm.ffi(cmd), (uint256[]));
        assertEq(exp.length, 2 * n, "reference output length");
        uint256 mismatches;
        for (uint256 i; i < n; ++i) {
            if (recs[i].wInc != exp[2 * i] || recs[i].token != exp[2 * i + 1]) ++mismatches;
        }
        vm.writeLine(
            logPath,
            string.concat(
                vm.toString(n), ",", vm.toString(nBlocks), ",", vm.toString(nSenders), ",", vm.toString(stats[2]), ",",
                vm.toString(stats[0]), ",", vm.toString(stats[1]), ",", vm.toString(mismatches)
            )
        );
        for (uint256 i; i < n; ++i) {
            assertEq(recs[i].wInc, exp[2 * i], "watermark increase differs from the reference");
            assertEq(recs[i].token, exp[2 * i + 1], "settled token amount differs from the reference");
        }
    }
}
