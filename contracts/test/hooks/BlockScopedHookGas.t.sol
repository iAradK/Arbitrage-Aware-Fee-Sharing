// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {console2} from "forge-std/console2.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "v4-core/src/types/PoolId.sol";

import {BlockScopedHookTestBase, SwapProbe} from "./BlockScopedHookTestBase.sol";
import {MockOracle} from "../../src/mocks/MockOracle.sol";

/// @notice Gas of the block-scoped hook against the transaction-scoped contract and a pool without a hook, scenario by scenario. Run with
/// `forge test --isolate`: every probe call is its own transaction, so storage and
/// accounts start cold in each, and a slot written in an earlier transaction is nonzero
/// but cold, as on chain. Gas is measured inside the transaction around the router call
/// (no intrinsic or calldata gas). Overhead = hooked minus unhooked for the identical
/// sequence. Output: BSGAS,<variant>,<scenario>,<gas> and snapshots/blockScopedHookGas.json.
///
/// Scenarios (W = watermark, A/B/C = the hook's slots):
///   first_ever    first swap the pool ever sees (A, B, C go from zero to nonzero)
///   new_block     first swap of a block after a scope in an earlier block (steady state)
///   second_tx     first swap of a second transaction in the same block
///   second_in_tx  second swap of one transaction (first opened the block's scope)
///   charged       new_block with a positive, settled charge (vault balance nonzero)
///   invalid       new_block with a stale oracle (no scope opens, charge 0)
contract BlockScopedHookGasTest is BlockScopedHookTestBase {
    using PoolIdLibrary for PoolKey;

    uint256 internal constant SMALL = 1e17; // single tick range, price 1.0: no surplus
    uint256 internal constant CHARGED = 10e18; // price 1.2: surplus above kappa

    uint256 internal constant NO_HOOK = 2;

    SwapProbe internal probe;
    mapping(uint256 => PoolKey) internal uncharged;
    mapping(uint256 => PoolKey) internal charged;

    string[3] internal names = ["block_scoped", "tx_scoped", "no_hook"];

    function setUp() public {
        setUpBase();
        probe = _probe();
        IHooks[3] memory hooks = [
            IHooks(address(_deployHook(_defaultParams()))),
            IHooks(address(_deployTxScopedHook(K_HAT, DELTA, LAMBDA_BPS, GAMMA_BPS))),
            IHooks(address(0))
        ];
        for (uint256 v; v < 3; ++v) {
            (uncharged[v],) = _pool(hooks[v], v == NO_HOOK ? 0 : 1e18, 3000);
            (charged[v],) = _pool(hooks[v], v == NO_HOOK ? 0 : 1.2e18, 500);
        }
    }

    function _one(PoolKey memory key, uint256 amount) internal returns (uint256) {
        return probe.swap(key, _exactIn(true, amount));
    }

    function _record(uint256 v, string memory scenario, uint256 gas_) internal {
        string memory name = string.concat(names[v], "__", scenario);
        console2.log(string.concat("BSGAS,", names[v], ",", scenario, ",", vm.toString(gas_)));
        vm.snapshotValue("blockScopedHookGas", name, gas_);
    }

    function _firstEver(uint256 v) internal {
        _assertIsolated();
        _record(v, "first_ever", _one(uncharged[v], SMALL));
    }

    function _newBlock(uint256 v) internal {
        _assertIsolated();
        _one(uncharged[v], SMALL);
        vm.roll(block.number + 1);
        _record(v, "new_block", _one(uncharged[v], SMALL));
    }

    function _secondTx(uint256 v) internal {
        _assertIsolated();
        _one(uncharged[v], SMALL);
        _record(v, "second_tx", _one(uncharged[v], SMALL));
    }

    function _secondInTx(uint256 v) internal {
        _assertIsolated();
        _one(uncharged[v], SMALL);
        vm.roll(block.number + 1);
        IPoolManager.SwapParams[] memory ps = new IPoolManager.SwapParams[](2);
        ps[0] = _exactIn(true, SMALL);
        ps[1] = _exactIn(true, SMALL);
        uint256[] memory used = probe.swapMany(uncharged[v], ps);
        _record(v, "first_in_multi_tx", used[0]);
        _record(v, "second_in_tx", used[1]);
    }

    function _charged(uint256 v) internal {
        _assertIsolated();
        _one(charged[v], CHARGED);
        vm.roll(block.number + 1);
        uint256 before = _vault(currency1);
        uint256 used = _one(charged[v], CHARGED);
        if (v != NO_HOOK) require(_vault(currency1) > before, "the measured swap must be charged");
        _record(v, "charged", used);
    }

    function _invalid(uint256 v) internal {
        _assertIsolated();
        _one(uncharged[v], SMALL);
        if (v != NO_HOOK) oracle.setMode(PoolId.unwrap(uncharged[v].toId()), MockOracle.Mode.ForceStale);
        vm.roll(block.number + 1);
        _record(v, "invalid", _one(uncharged[v], SMALL));
    }

    function test_block_scoped_first_ever() public { _firstEver(0); }
    function test_block_scoped_new_block() public { _newBlock(0); }
    function test_block_scoped_second_tx() public { _secondTx(0); }
    function test_block_scoped_second_in_tx() public { _secondInTx(0); }
    function test_block_scoped_charged() public { _charged(0); }
    function test_block_scoped_invalid() public { _invalid(0); }

    function test_tx_scoped_first_ever() public { _firstEver(1); }
    function test_tx_scoped_new_block() public { _newBlock(1); }
    function test_tx_scoped_second_tx() public { _secondTx(1); }
    function test_tx_scoped_second_in_tx() public { _secondInTx(1); }
    function test_tx_scoped_charged() public { _charged(1); }
    function test_tx_scoped_invalid() public { _invalid(1); }

    function test_no_hook_first_ever() public { _firstEver(2); }
    function test_no_hook_new_block() public { _newBlock(2); }
    function test_no_hook_second_tx() public { _secondTx(2); }
    function test_no_hook_second_in_tx() public { _secondInTx(2); }
    function test_no_hook_charged() public { _charged(2); }
    function test_no_hook_invalid() public { _invalid(2); }
}
