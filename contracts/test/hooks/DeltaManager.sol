// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {toBalanceDelta} from "v4-core/src/types/BalanceDelta.sol";

import {ParticipationAwareHook} from "../../src/hooks/ParticipationAwareHook.sol";

/// @notice Stands in for the PoolManager of a ParticipationAwareHook deployed against it, so
/// afterSwap can be driven with exact core deltas (E7 vectors, int128 boundaries) that no
/// real pool would produce. The hook's code path is the deployed one; only the caller
/// differs. `mint` (the hook's settlement into the vault) is a no-op that counts the amount.
/// One call into this contract is one transaction under `forge test --isolate`.
contract DeltaManager {
    uint256 public minted;

    function mint(address, uint256, uint256 amount) external {
        minted += amount;
    }

    /// @dev Swap parameters that make the unspecified (settlement) currency token0 or token1:
    /// exact input oneForZero settles in token0, exact input zeroForOne in token1.
    function _params(bool settleToken0) internal pure returns (IPoolManager.SwapParams memory p) {
        p.zeroForOne = !settleToken0;
        p.amountSpecified = -1;
    }

    function swap(ParticipationAwareHook hook, PoolKey calldata key, int128 d0, int128 d1, bool settleToken0)
        public
        returns (int128 charge)
    {
        (, charge) = hook.afterSwap(address(this), key, _params(settleToken0), toBalanceDelta(d0, d1), "");
    }

    /// @dev All swaps inside one call: one transaction.
    function swapMany(
        ParticipationAwareHook hook,
        PoolKey calldata key,
        int128[] calldata d0,
        int128[] calldata d1,
        bool settleToken0
    ) external returns (int128[] memory charges) {
        charges = new int128[](d0.length);
        for (uint256 j; j < d0.length; ++j) {
            charges[j] = swap(hook, key, d0[j], d1[j], settleToken0);
        }
    }

    /// @dev As swapMany, and before each swap reads quote() of that swap's delta inside the same
    /// transaction (the hook keeps the transaction's running state in transient storage).
    function quoteAndSwapMany(
        ParticipationAwareHook hook,
        PoolKey calldata key,
        int128[] calldata d0,
        int128[] calldata d1,
        bool settleToken0
    ) external returns (int128[] memory charges, uint256[] memory quotes) {
        charges = new int128[](d0.length);
        quotes = new uint256[](d0.length);
        for (uint256 j; j < d0.length; ++j) {
            quotes[j] = hook.quote(key, d0[j], d1[j]);
            charges[j] = swap(hook, key, d0[j], d1[j], settleToken0);
        }
    }
}
