// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Pure accounting core for participation-aware surplus sharing.
/// All monetary inputs use the same fixed-point scale (recommended: 1e18 USD).
contract SurplusSharingAccounting {
    uint256 public constant WAD = 1e18;

    error InvalidLambda();
    error InvalidGamma();

    function computeTransfer(
        uint256 surplusHat,
        uint256 executionMarginHat,
        uint256 lambdaWad,
        uint256 gammaWad,
        uint256 delta
    ) public pure returns (uint256 transferAmount, bool capBinds) {
        if (lambdaWad > WAD) revert InvalidLambda();
        if (gammaWad >= WAD) revert InvalidGamma();

        uint256 target = mulWadDown(lambdaWad, surplusHat);
        uint256 bufferedMargin;

        // Evaluate [S_hat - K_hat - delta]^+ without adding K_hat and
        // delta first.  This avoids an otherwise unnecessary uint256
        // overflow at the boundary and implements the paper's order of
        // operations exactly.
        if (surplusHat > executionMarginHat) {
            uint256 afterMargin = surplusHat - executionMarginHat;
            if (afterMargin > delta) {
                bufferedMargin = afterMargin - delta;
            }
        }

        uint256 cap = mulWadDown(WAD - gammaWad, bufferedMargin);

        transferAmount = target < cap ? target : cap;
        capBinds = cap <= target;
    }

    function mulWadDown(uint256 x, uint256 y) internal pure returns (uint256) {
        // Every current caller supplies y <= WAD. Splitting x into quotient
        // and remainder therefore computes floor(x*y/WAD) without allowing
        // the intermediate product to overflow for large x.
        return (x / WAD) * y + ((x % WAD) * y) / WAD;
    }
}
