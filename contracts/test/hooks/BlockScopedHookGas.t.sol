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
///   new_block     first swap of a block after a scope in an earlier block (steady state;
///                 the oracle price moves between the blocks, as on chain)
///   second_tx     first swap of a second transaction in the same block
///   second_in_tx  second swap of one transaction (first opened the block's scope)
///   charged       new_block with a positive, settled charge (vault balance nonzero)
///   charged_vault_empty   as charged, but the vault holds none of the charged token yet,
///                 so its ERC-6909 balance goes from zero to nonzero (earlier block uncharged)
///   charged_second_tx     charged first swap of a second transaction in the same block
///   charged_first_ever    first swap the pool ever sees, charged, vault empty: the conditions
///                 of HookGasSettled (the paper's 61,807)
///   invalid       new_block with a stale oracle (no scope opens, charge 0)
///   invalid_second_in_tx  second swap of one transaction with a stale oracle: the
///                 transaction-scoped hook has disabled the pool, the block-scoped one retries
///   charged_after_other_tx  charged transaction after another sender's uncharged transaction
///                 in the same block (block scope V1: its first swap folds the earlier
///                 transaction's bracket into the block total)
/// The block-scoped hook runs in the evaluated configuration (_finalParams: V1 with the
/// relative buffer eps_rel = 0.283%, g_hat = 180,214, tau_hat = 3 gwei, gamma = 0.02).
contract BlockScopedHookGasTest is BlockScopedHookTestBase {
    using PoolIdLibrary for PoolKey;

    uint256 internal constant SMALL = 1e17; // single tick range, price 1.0: no surplus
    uint256 internal constant CHARGED = 10e18; // price 1.2: surplus above kappa

    uint256 internal constant NO_HOOK = 2;

    SwapProbe internal probe;
    SwapProbe internal other; // a second sender
    mapping(uint256 => PoolKey) internal uncharged;
    mapping(uint256 => PoolKey) internal charged;

    string[3] internal names = ["block_scoped", "tx_scoped", "no_hook"];

    function setUp() public {
        setUpBase();
        probe = _probe();
        other = _probe();
        IHooks[3] memory hooks = [
            IHooks(address(_deployHook(_finalParams(K_HAT)))),
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

    /// @dev Moves the oracle price slightly before a measured new-block swap. On chain the
    /// reference (and the base fee, hence kappa) changes from block to block, so the
    /// opening swap rewrites slot C with a new value; with an unchanged price the rewrite
    /// would cost 100 gas instead of 2,900 and understate the block-scoped hook. Applied to
    /// both hooks; the transaction-scoped one is unaffected.
    function _nudge(uint256 v, PoolKey memory key, uint256 priceWad) internal {
        if (v != NO_HOOK) oracle.setPrice(PoolId.unwrap(key.toId()), priceWad);
    }

    function _firstEver(uint256 v) internal {
        _assertIsolated();
        _record(v, "first_ever", _one(uncharged[v], SMALL));
    }

    function _newBlock(uint256 v) internal {
        _assertIsolated();
        _one(uncharged[v], SMALL);
        vm.roll(block.number + 1);
        _nudge(v, uncharged[v], 1.0001e18);
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
        _nudge(v, uncharged[v], 1.0001e18);
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
        _nudge(v, charged[v], 1.2001e18);
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

    function _chargedVaultEmpty(uint256 v) internal {
        _assertIsolated();
        bytes32 id = PoolId.unwrap(charged[v].toId());
        if (v != NO_HOOK) oracle.setPrice(id, 1e18); // the earlier block is uncharged
        _one(charged[v], CHARGED);
        if (v != NO_HOOK) oracle.setPrice(id, 1.2e18);
        vm.roll(block.number + 1);
        require(_vault(currency1) == 0, "vault must start empty");
        uint256 used = _one(charged[v], CHARGED);
        if (v != NO_HOOK) require(_vault(currency1) > 0, "the measured swap must be charged");
        _record(v, "charged_vault_empty", used);
    }

    function _chargedSecondTx(uint256 v) internal {
        _assertIsolated();
        _one(charged[v], CHARGED);
        uint256 before = _vault(currency1);
        uint256 used = _one(charged[v], CHARGED);
        if (v != NO_HOOK) require(_vault(currency1) > before, "the measured swap must be charged");
        _record(v, "charged_second_tx", used);
    }

    function _chargedFirstEver(uint256 v) internal {
        _assertIsolated();
        uint256 used = _one(charged[v], CHARGED);
        if (v != NO_HOOK) require(_vault(currency1) > 0, "the measured swap must be charged");
        _record(v, "charged_first_ever", used);
    }

    function _invalidSecondInTx(uint256 v) internal {
        _assertIsolated();
        _one(uncharged[v], SMALL);
        if (v != NO_HOOK) oracle.setMode(PoolId.unwrap(uncharged[v].toId()), MockOracle.Mode.ForceStale);
        vm.roll(block.number + 1);
        IPoolManager.SwapParams[] memory ps = new IPoolManager.SwapParams[](2);
        ps[0] = _exactIn(true, SMALL);
        ps[1] = _exactIn(true, SMALL);
        uint256[] memory used = probe.swapMany(uncharged[v], ps);
        _record(v, "invalid_first_in_multi_tx", used[0]);
        _record(v, "invalid_second_in_tx", used[1]);
    }

    function _chargedAfterOtherTx(uint256 v) internal {
        _assertIsolated();
        _one(charged[v], CHARGED); // an earlier block, so the vault holds the token
        vm.roll(block.number + 1);
        _nudge(v, charged[v], 1.2001e18);
        uint256 v0 = _vault(currency1);
        other.swap(charged[v], _exactIn(true, SMALL)); // another sender's uncharged transaction opens the block
        if (v != NO_HOOK) require(_vault(currency1) == v0, "the first transaction must be uncharged");
        uint256 used = _one(charged[v], CHARGED);
        if (v != NO_HOOK) require(_vault(currency1) > v0, "the measured swap must be charged");
        _record(v, "charged_after_other_tx", used);
    }

    function test_block_scoped_charged_after_other_tx() public { _chargedAfterOtherTx(0); }
    function test_tx_scoped_charged_after_other_tx() public { _chargedAfterOtherTx(1); }
    function test_no_hook_charged_after_other_tx() public { _chargedAfterOtherTx(2); }

    function test_block_scoped_charged_vault_empty() public { _chargedVaultEmpty(0); }
    function test_block_scoped_charged_second_tx() public { _chargedSecondTx(0); }
    function test_block_scoped_charged_first_ever() public { _chargedFirstEver(0); }
    function test_block_scoped_invalid_second_in_tx() public { _invalidSecondInTx(0); }
    function test_tx_scoped_charged_vault_empty() public { _chargedVaultEmpty(1); }
    function test_tx_scoped_charged_second_tx() public { _chargedSecondTx(1); }
    function test_tx_scoped_charged_first_ever() public { _chargedFirstEver(1); }
    function test_tx_scoped_invalid_second_in_tx() public { _invalidSecondInTx(1); }
    function test_no_hook_charged_vault_empty() public { _chargedVaultEmpty(2); }
    function test_no_hook_charged_second_tx() public { _chargedSecondTx(2); }
    function test_no_hook_charged_first_ever() public { _chargedFirstEver(2); }
    function test_no_hook_invalid_second_in_tx() public { _invalidSecondInTx(2); }

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
