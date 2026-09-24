// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Vendored CREATE2 salt miner for Uniswap v4 hook address flags. HookMiner
/// lives in the separately-versioned v4-periphery/v4-template repos rather than
/// v4-core itself, so it is vendored here as a small, well-known, stable algorithm
/// rather than adding a fourth Forge dependency.
library HookMiner {
    // Matches Hooks.ALL_HOOK_MASK: the low 14 bits of the deployed address encode
    // which callbacks the pool manager will invoke.
    uint160 internal constant FLAG_MASK = 0x3FFF;

    // A conservative bound on how many salts to try before giving up; mining for a
    // 14-bit flag match typically succeeds within a few thousand attempts.
    uint256 internal constant MAX_LOOP = 200_000;

    /// @notice Find a salt producing a CREATE2 address whose low 14 bits equal
    /// `flags`, for a contract with the given creation code and constructor args,
    /// deployed by `deployer`.
    function find(address deployer, uint160 flags, bytes memory creationCode, bytes memory constructorArgs)
        internal
        view
        returns (address hookAddress, bytes32 salt)
    {
        bytes memory creationCodeWithArgs = abi.encodePacked(creationCode, constructorArgs);
        bytes32 initCodeHash = keccak256(creationCodeWithArgs);
        flags = flags & FLAG_MASK;

        for (uint256 i; i < MAX_LOOP; i++) {
            salt = bytes32(i);
            hookAddress = computeAddress(deployer, salt, initCodeHash);
            if (uint160(hookAddress) & FLAG_MASK == flags && hookAddress.code.length == 0) {
                return (hookAddress, salt);
            }
        }
        revert("HookMiner: could not find salt");
    }

    function computeAddress(address deployer, bytes32 salt, bytes32 initCodeHash) internal pure returns (address) {
        return address(uint160(uint256(keccak256(abi.encodePacked(bytes1(0xFF), deployer, salt, initCodeHash)))));
    }
}