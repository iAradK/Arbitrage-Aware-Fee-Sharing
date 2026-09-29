// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IReferenceOracle} from "../interfaces/IReferenceOracle.sol";

/// @notice Test oracle with a per-pool settable failure mode, used to fuzz a hooks
/// defensive checks against invalid oracle states.
/// @dev ForceStale, ZeroPrice, and IncompleteRound all report valid=true so that a
/// consuming hooks own staleness/zero-price/round-completeness checks are what catch
/// the bad data, not this mocks own valid flag. SelfReportedInvalid instead reports
/// valid=false, exercising the branch where the hook trusts the oracles own signal.
/// Revert makes latestPrice revert, exercising the hooks try/catch around the read.
contract MockOracle is IReferenceOracle {
    /// @dev Comfortably larger than any staleness threshold a hook is expected to
    /// configure, so ForceStale reads as stale regardless of block.timestamp.
    uint256 internal constant STALE_OFFSET = 365 days;

    enum Mode {
        Normal,
        ForceStale,
        ZeroPrice,
        IncompleteRound,
        SelfReportedInvalid,
        Revert
    }

    error OracleUnavailable();

    struct PoolConfig {
        uint256 priceWad;
        uint256 updatedAt;
        Mode mode;
    }

    mapping(bytes32 => PoolConfig) internal _configs;

    /// @notice Set the reference price for a pool and stamp updatedAt to now.
    function setPrice(bytes32 poolId, uint256 priceWad) external {
        _configs[poolId].priceWad = priceWad;
        _configs[poolId].updatedAt = block.timestamp;
    }

    /// @notice Set the failure mode a pool should report on the next latestPrice call.
    function setMode(bytes32 poolId, Mode mode) external {
        _configs[poolId].mode = mode;
    }

    function latestPrice(bytes32 poolId)
        external
        view
        override
        returns (uint256 priceWad, uint256 updatedAt, bool valid)
    {
        PoolConfig memory cfg = _configs[poolId];

        if (cfg.mode == Mode.Revert) revert OracleUnavailable();
        if (cfg.mode == Mode.ForceStale) {
            // Always at least STALE_OFFSET seconds in the past (relative to "now",
            // not a fixed absolute timestamp): a hardcoded absolute value like 1
            // would collide with Foundry's default block.timestamp of 1, making
            // the pool look fresh instead of stale in tests that don't warp time.
            uint256 staleUpdatedAt = block.timestamp > STALE_OFFSET ? block.timestamp - STALE_OFFSET : 0;
            return (cfg.priceWad, staleUpdatedAt, true);
        }
        if (cfg.mode == Mode.ZeroPrice) {
            return (0, block.timestamp, true);
        }
        if (cfg.mode == Mode.IncompleteRound) {
            return (cfg.priceWad, 0, true);
        }
        if (cfg.mode == Mode.SelfReportedInvalid) {
            return (cfg.priceWad, cfg.updatedAt, false);
        }
        return (cfg.priceWad, cfg.updatedAt, true);
    }
}