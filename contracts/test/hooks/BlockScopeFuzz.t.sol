// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Vm} from "forge-std/Vm.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId} from "v4-core/src/types/PoolId.sol";

import {BlockScopedHookTestBase, SwapProbe} from "./BlockScopedHookTestBase.sol";
import {ParticipationAwareHook} from "../../src/hooks/ParticipationAwareHook.sol";
import {MockOracle} from "../../src/mocks/MockOracle.sol";

/// @notice Final hook vs ScopedHookReference(scope="block", accumulation="tx_clip",
/// buffer="rel"), to the wei, per swap. Each run: 1-4 blocks; in each block 1-3 transactions
/// from up to 8 senders (each sender a SwapProbe contract; a transaction is one swapMany
/// call of 1-3 swaps), both directions, exact input and output (both settlement
/// currencies); oracle updates between blocks and sometimes mid-block; sometimes an invalid
/// oracle for the first transactions of a block. Per swap, the core delta comes from the
/// PoolManager's Swap event and the hook's marginal charge, block surplus A and settled amount
/// from its HookCharge / HookSettled events; the trace goes through
/// experiments/block_scope_v1_ffi.py and all three must equal the reference.
///
/// Needs `--ffi`. Runs both ways:
///   --isolate     each swapMany call is its own transaction; isolated calls see base fee 0
///   no --isolate  the whole run is one transaction (every block's swaps share it), and
///                 vm.fee sets a new base fee in every block
/// Each run appends one line to fuzz_v1_<mode>.csv in $BLOCK_SCOPE_OUT when an experiment script
/// sets it (e.g. ../results/e7_block_scope), else in the gitignored scratch directory
/// cache/scratch/e7_block_scope, so a plain test run never overwrites a result.
contract BlockScopeFuzzTest is BlockScopedHookTestBase {
    uint256 internal constant K = 1e18;
    uint256 internal constant NONE = type(uint256).max;
    bytes32 internal constant SWAP_TOPIC = keccak256("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)");
    bytes32 internal constant CHARGE_TOPIC = keccak256("HookCharge(bytes32,uint256,uint256,uint256)");
    bytes32 internal constant SETTLED_TOPIC = keccak256("HookSettled(bytes32,address,uint256)");

    ParticipationAwareHook internal hook;
    PoolKey internal pool;
    PoolId internal poolId;
    SwapProbe[8] internal senders;
    bool internal isolated;
    string internal logPath;

    struct Rec {
        uint256 blk;
        uint256 tx;
        uint256 price;
        uint256 basefee;
        int256 d0;
        int256 d1;
        bool settle0;
        uint256 w;
        uint256 token;
        uint256 surplus;
        bool hasCharge;
    }

    struct Run {
        uint256 ctr;
        uint256 n;
        uint256 txid;
        uint256 txs;
        uint256 multiSwapTxs;
        uint256 invalidSwaps;
        uint256 midBlockUpdates;
        uint256 price;
    }

    function setUp() public {
        setUpBase();
        hook = _deployHook(_finalParams(K));
        (pool, poolId) = _pool(IHooks(address(hook)), 1e18);
        for (uint256 i; i < 8; ++i) {
            senders[i] = _probe();
        }
        transientProbe.set();
        isolated = !transientProbe.isSet();
        string memory dir = vm.envOr("BLOCK_SCOPE_OUT", string("cache/scratch/e7_block_scope"));
        vm.createDir(dir, true);
        logPath = string.concat(dir, isolated ? "/fuzz_v1_isolate.csv" : "/fuzz_v1_no_isolate.csv");
        if (vm.exists(logPath)) vm.removeFile(logPath);
        vm.writeLine(logPath, "swaps,txs,multi_swap_txs,blocks,senders,charged,oracle_invalid_swaps,mid_block_updates,mismatches");
    }

    function _r(uint256 seed, uint256 i) internal pure returns (uint256) {
        return uint256(keccak256(abi.encode(seed, i)));
    }

    /// @dev Executes one transaction and appends one Rec per swap from its events.
    function _tx(uint256 seed, Run memory run, Rec[] memory recs, uint256 nSenders, bool valid) internal {
        uint256 k = 1 + _r(seed, run.ctr++) % 3;
        if (run.n + k > recs.length) k = recs.length - run.n;
        if (k == 0) return;
        IPoolManager.SwapParams[] memory ps = new IPoolManager.SwapParams[](k);
        for (uint256 j; j < k; ++j) {
            bool zeroForOne = _r(seed, run.ctr++) % 2 == 0;
            uint256 amount = 1e17 + _r(seed, run.ctr++) % 5e19;
            ps[j] = _r(seed, run.ctr++) % 2 == 0 ? _exactIn(zeroForOne, amount) : _exactOut(zeroForOne, amount);
        }
        SwapProbe who = senders[_r(seed, run.ctr++) % nSenders];
        vm.recordLogs();
        who.swapMany(pool, ps);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        uint256 first = run.n;
        uint256 idx;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(manager) && logs[i].topics[0] == SWAP_TOPIC) {
                (int128 a0, int128 a1,,,,) = abi.decode(logs[i].data, (int128, int128, uint160, uint128, int24, uint24));
                Rec memory rec = recs[run.n++];
                rec.blk = vm.getBlockNumber();
                rec.tx = run.txid;
                rec.price = valid ? run.price : 0;
                rec.basefee = isolated ? 0 : block.basefee;
                (rec.d0, rec.d1) = (a0, a1);
                rec.settle0 = (ps[idx].amountSpecified < 0) != ps[idx].zeroForOne;
                ++idx;
            } else if (logs[i].emitter == address(hook) && logs[i].topics[0] == CHARGE_TOPIC) {
                (uint256 surplus,, uint256 marginal) = abi.decode(logs[i].data, (uint256, uint256, uint256));
                Rec memory rec = recs[run.n - 1];
                (rec.surplus, rec.w, rec.hasCharge) = (surplus, marginal, true);
            } else if (logs[i].emitter == address(hook) && logs[i].topics[0] == SETTLED_TOPIC) {
                (, uint256 amount) = abi.decode(logs[i].data, (address, uint256));
                recs[run.n - 1].token = amount;
            }
        }
        assertEq(run.n - first, k, "one Swap event per swap");
        ++run.txs;
        if (k > 1) ++run.multiSwapTxs;
        if (isolated) ++run.txid; // without --isolate the whole run is one transaction
    }

    /// forge-config: default.fuzz.runs = 2000
    function testFuzz_ContractMatchesReference(uint256 seed) public {
        bytes32 id = PoolId.unwrap(poolId);
        Run memory run;
        uint256 nBlocks = 1 + _r(seed, run.ctr++) % 4;
        uint256 nSenders = 1 + _r(seed, run.ctr++) % 8;
        Rec[] memory recs = new Rec[](16);
        for (uint256 b; b < nBlocks; ++b) {
            vm.roll(vm.getBlockNumber() + 1 + _r(seed, run.ctr++) % 3);
            vm.warp(block.timestamp + 12);
            if (!isolated) vm.fee(1 gwei + _r(seed, run.ctr++) % 200 gwei);
            run.price = 0.8e18 + _r(seed, run.ctr++) % 0.45e18; // oracle update between blocks
            oracle.setPrice(id, run.price);
            oracle.setMode(id, MockOracle.Mode.Normal);
            uint256 ntx = 1 + _r(seed, run.ctr++) % 3;
            uint256 invalidFor = _r(seed, run.ctr++) % 5 == 0 ? 1 + _r(seed, run.ctr++) % ntx : 0;
            if (invalidFor != 0) oracle.setMode(id, MockOracle.Mode(1 + _r(seed, run.ctr++) % 5));
            for (uint256 t; t < ntx; ++t) {
                if (invalidFor != 0 && t == invalidFor) oracle.setMode(id, MockOracle.Mode.Normal);
                if (t != 0 && _r(seed, run.ctr++) % 4 == 0) {
                    run.price = 0.8e18 + _r(seed, run.ctr++) % 0.45e18; // mid-block update
                    oracle.setPrice(id, run.price);
                    ++run.midBlockUpdates;
                }
                bool valid = invalidFor == 0 || t >= invalidFor;
                uint256 before = run.n;
                _tx(seed, run, recs, nSenders, valid);
                if (!valid) run.invalidSwaps += run.n - before;
            }
        }
        uint256 n = run.n;
        string[] memory cmd = new string[](9 + 7 * n);
        cmd[0] = "python3";
        cmd[1] = "../experiments/block_scope_v1_ffi.py";
        cmd[2] = vm.toString(K);
        cmd[3] = vm.toString(uint256(LAMBDA_BPS));
        cmd[4] = vm.toString(uint256(FINAL_GAMMA_BPS));
        cmd[5] = vm.toString(uint256(FINAL_GAS_UNITS));
        cmd[6] = vm.toString(uint256(FINAL_TAU_WEI));
        cmd[7] = vm.toString(uint256(hook.gasPriceToken0Wad()));
        cmd[8] = vm.toString(uint256(FINAL_EPS_PPB) * 1e9);
        for (uint256 i; i < n; ++i) {
            Rec memory rec = recs[i];
            uint256 o = 9 + 7 * i;
            cmd[o] = vm.toString(rec.blk);
            cmd[o + 1] = vm.toString(rec.tx);
            cmd[o + 2] = vm.toString(rec.price);
            cmd[o + 3] = vm.toString(rec.basefee);
            cmd[o + 4] = vm.toString(rec.d0);
            cmd[o + 5] = vm.toString(rec.d1);
            cmd[o + 6] = rec.settle0 ? "1" : "0";
        }
        uint256[] memory exp = abi.decode(vm.ffi(cmd), (uint256[]));
        assertEq(exp.length, 3 * n, "reference output length");
        uint256 mismatches;
        uint256 charged;
        for (uint256 i; i < n; ++i) {
            Rec memory rec = recs[i];
            bool bad = rec.w != exp[3 * i] || rec.token != exp[3 * i + 1];
            if (rec.hasCharge && exp[3 * i + 2] != NONE && rec.surplus != exp[3 * i + 2]) bad = true;
            if (rec.hasCharge != (exp[3 * i + 2] != NONE)) bad = true; // a scope exists exactly when the reference has one
            if (bad) ++mismatches;
            if (rec.w != 0) ++charged;
        }
        vm.writeLine(
            logPath,
            string.concat(
                vm.toString(n), ",", vm.toString(run.txs), ",", vm.toString(run.multiSwapTxs), ",", vm.toString(nBlocks), ",",
                vm.toString(nSenders), ",", vm.toString(charged), ",", vm.toString(run.invalidSwaps), ",",
                vm.toString(run.midBlockUpdates), ",", vm.toString(mismatches)
            )
        );
        for (uint256 i; i < n; ++i) {
            assertEq(recs[i].w, exp[3 * i], "marginal charge differs from the reference");
            assertEq(recs[i].token, exp[3 * i + 1], "settled amount differs from the reference");
            if (recs[i].hasCharge && exp[3 * i + 2] != NONE) {
                assertEq(recs[i].surplus, exp[3 * i + 2], "block surplus differs from the reference");
            }
        }
        assertEq(mismatches, 0, "mismatches");
    }
}
