// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Minimal reference-price oracle interface consumed by ParticipationAwareHook.
/// @dev priceWad is the reference price in WAD common-numeraire units. updatedAt is the
/// timestamp of the last completed round (0 signals an incomplete round). valid is the
/// oracles own report of data integrity; a value of true does not by itself guarantee
/// freshness or a nonzero price, so callers must independently check updatedAt and
/// priceWad before trusting the result.
interface IReferenceOracle {
    function latestPrice(bytes32 poolId)
        external
        view
        returns (uint256 priceWad, uint256 updatedAt, bool valid);
}