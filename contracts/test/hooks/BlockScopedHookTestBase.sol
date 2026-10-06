// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Deployers} from "v4-core/test/utils/Deployers.sol";
import {MockERC20} from "solmate/src/test/utils/mocks/MockERC20.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {Hooks} from "v4-core/src/libraries/Hooks.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId} from "v4-core/src/types/PoolId.sol";
import {Currency} from "v4-core/src/types/Currency.sol";
import {BalanceDelta} from "v4-core/src/types/BalanceDelta.sol";
import {PoolSwapTest} from "v4-core/src/test/PoolSwapTest.sol";

import {HookMiner} from "../utils/HookMiner.sol";
import {ParticipationAwareHook} from "../../src/hooks/ParticipationAwareHook.sol";
import {TxScopedParticipationAwareHook} from "../../src/hooks/TxScopedParticipationAwareHook.sol";
import {MockOracle} from "../../src/mocks/MockOracle.sol";

/// @notice Swaps from inside one call, so that several swaps share one transaction under
/// `forge test --isolate`, and measures their gas from inside the transaction (no
/// intrinsic or calldata gas, so hooked and unhooked pools compare directly).
contract SwapProbe {
    PoolSwapTest internal immutable router;

    constructor(PoolSwapTest _router, Currency c0, Currency c1) {
        router = _router;
        MockERC20(Currency.unwrap(c0)).approve(address(_router), type(uint256).max);
        MockERC20(Currency.unwrap(c1)).approve(address(_router), type(uint256).max);
    }

    function swap(PoolKey calldata key, IPoolManager.SwapParams calldata params) external returns (uint256 used) {
        PoolSwapTest.TestSettings memory settings = PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false});
        uint256 g0 = gasleft();
        router.swap(key, params, settings, "");
        used = g0 - gasleft();
    }

    function swapMany(PoolKey calldata key, IPoolManager.SwapParams[] calldata params)
        external
        returns (uint256[] memory used)
    {
        PoolSwapTest.TestSettings memory settings = PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false});
        used = new uint256[](params.length);
        for (uint256 i; i < params.length; ++i) {
            uint256 g0 = gasleft();
            router.swap(key, params[i], settings, "");
            used[i] = g0 - gasleft();
        }
    }
}

/// @notice Fixture for the block-scoped hook. Tests are meant to run with
/// `forge test --isolate`, so every external call from a test function (each swap, each
/// oracle update) is its own transaction; `_assertIsolated` makes that a precondition.
abstract contract BlockScopedHookTestBase is Deployers {
    uint160 internal constant AFTER_ONLY_FLAGS = uint160(Hooks.AFTER_SWAP_FLAG | Hooks.AFTER_SWAP_RETURNS_DELTA_FLAG);
    uint160 internal constant TX_SCOPED_FLAGS =
        uint160(Hooks.BEFORE_SWAP_FLAG | Hooks.AFTER_SWAP_FLAG | Hooks.AFTER_SWAP_RETURNS_DELTA_FLAG);

    address internal constant VAULT = address(0x7A017);
    uint256 internal constant K_HAT = 1e18;
    uint16 internal constant LAMBDA_BPS = 7500;
    uint16 internal constant GAMMA_BPS = 500;
    uint256 internal constant DELTA = 0;
    uint32 internal constant STALENESS = 3600;

    MockOracle internal oracle;
    TransientProbe internal transientProbe;

    function setUpBase() internal {
        deployFreshManagerAndRouters();
        deployMintAndApprove2Currencies();
        oracle = new MockOracle();
        transientProbe = new TransientProbe();
    }

    /// @dev Under --isolate the probe's transient write is cleared before the next call.
    function _assertIsolated() internal {
        transientProbe.set();
        require(!transientProbe.isSet(), "run with forge test --isolate (each call must be its own transaction)");
    }

    function _params(uint256 kHat, uint256 delta, uint16 lambdaBps_, uint16 gammaBps_)
        internal
        pure
        returns (ParticipationAwareHook.Params memory p)
    {
        p.kHatConstant = kHat;
        p.delta = delta;
        p.lambdaBps = lambdaBps_;
        p.gammaBps = gammaBps_;
        p.stalenessThresholdSeconds = STALENESS;
    }

    function _defaultParams() internal pure returns (ParticipationAwareHook.Params memory) {
        return _params(K_HAT, DELTA, LAMBDA_BPS, GAMMA_BPS);
    }

    function _deployHook(ParticipationAwareHook.Params memory p) internal returns (ParticipationAwareHook h) {
        bytes memory args = abi.encode(manager, oracle, VAULT, p);
        (address mined, bytes32 salt) =
            HookMiner.find(address(this), AFTER_ONLY_FLAGS, type(ParticipationAwareHook).creationCode, args);
        h = new ParticipationAwareHook{salt: salt}(manager, oracle, VAULT, p);
        require(address(h) == mined, "mined address mismatch");
    }

    /// @dev The transaction-scoped contract with the same economics (kappa = kHat + delta).
    function _deployTxScopedHook(uint256 kHat, uint256 delta, uint16 lambdaBps_, uint16 gammaBps_)
        internal
        returns (TxScopedParticipationAwareHook h)
    {
        uint256 l = uint256(lambdaBps_) * 1e14;
        uint256 g = uint256(gammaBps_) * 1e14;
        bytes memory args = abi.encode(manager, oracle, VAULT, kHat, l, g, delta, uint256(STALENESS));
        (address mined, bytes32 salt) =
            HookMiner.find(address(this), TX_SCOPED_FLAGS, type(TxScopedParticipationAwareHook).creationCode, args);
        h = new TxScopedParticipationAwareHook{salt: salt}(manager, oracle, VAULT, kHat, l, g, delta, STALENESS);
        require(address(h) == mined, "mined address mismatch");
    }

    /// @dev A pool on the shared currencies with fee 0.3% and tick spacing 60, seeded with
    /// the discrete tick-range liquidity of HookTestBase, oracle price set (0 = unset).
    /// Pools that must price swaps identically differ only in their hook address.
    function _pool(IHooks hooks, uint256 priceWad) internal returns (PoolKey memory key, PoolId id) {
        return _pool(hooks, priceWad, 3000);
    }

    function _pool(IHooks hooks, uint256 priceWad, uint24 fee) internal returns (PoolKey memory key, PoolId id) {
        key = PoolKey(currency0, currency1, fee, 60, hooks);
        id = key.toId();
        manager.initialize(key, SQRT_PRICE_1_1);
        _seedDiscreteLiquidity(key);
        if (priceWad != 0) oracle.setPrice(PoolId.unwrap(id), priceWad);
    }

    function _seedDiscreteLiquidity(PoolKey memory key) internal {
        int24[6] memory lowerTicks = [int24(-1800), -1200, -600, 0, 600, 1200];
        for (uint256 i = 0; i < lowerTicks.length; i++) {
            modifyLiquidityRouter.modifyLiquidity(
                key,
                IPoolManager.ModifyLiquidityParams({
                    tickLower: lowerTicks[i],
                    tickUpper: lowerTicks[i] + 600,
                    liquidityDelta: 1e26,
                    salt: 0
                }),
                ZERO_BYTES
            );
        }
    }

    /// @dev A funded sender with router approvals, for multi-sender transaction splits.
    function _actor(uint256 i) internal returns (address actor) {
        actor = address(uint160(uint256(keccak256(abi.encode("actor", i)))));
        MockERC20(Currency.unwrap(currency0)).mint(actor, 1e30);
        MockERC20(Currency.unwrap(currency1)).mint(actor, 1e30);
        vm.startPrank(actor);
        MockERC20(Currency.unwrap(currency0)).approve(address(swapRouter), type(uint256).max);
        MockERC20(Currency.unwrap(currency1)).approve(address(swapRouter), type(uint256).max);
        vm.stopPrank();
    }

    function _probe() internal returns (SwapProbe probe) {
        probe = new SwapProbe(swapRouter, currency0, currency1);
        MockERC20(Currency.unwrap(currency0)).mint(address(probe), 1e30);
        MockERC20(Currency.unwrap(currency1)).mint(address(probe), 1e30);
    }

    function _exactIn(bool zeroForOne, uint256 amount) internal pure returns (IPoolManager.SwapParams memory) {
        return IPoolManager.SwapParams({
            zeroForOne: zeroForOne,
            amountSpecified: -int256(amount),
            sqrtPriceLimitX96: zeroForOne ? MIN_PRICE_LIMIT : MAX_PRICE_LIMIT
        });
    }

    function _exactOut(bool zeroForOne, uint256 amount) internal pure returns (IPoolManager.SwapParams memory) {
        return IPoolManager.SwapParams({
            zeroForOne: zeroForOne,
            amountSpecified: int256(amount),
            sqrtPriceLimitX96: zeroForOne ? MIN_PRICE_LIMIT : MAX_PRICE_LIMIT
        });
    }

    function _swap(PoolKey memory key, IPoolManager.SwapParams memory params) internal returns (BalanceDelta) {
        PoolSwapTest.TestSettings memory settings = PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false});
        return swapRouter.swap(key, params, settings, ZERO_BYTES);
    }

    function _swapAs(address sender, PoolKey memory key, IPoolManager.SwapParams memory params)
        internal
        returns (BalanceDelta)
    {
        PoolSwapTest.TestSettings memory settings = PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false});
        vm.prank(sender);
        return swapRouter.swap(key, params, settings, ZERO_BYTES);
    }

    function _vault(Currency currency) internal view returns (uint256) {
        return manager.balanceOf(VAULT, currency.toId());
    }

    /// @dev Surplus of a scope's cumulative delta, as the hook computes it.
    function _scopeSurplus(ParticipationAwareHook.Scope memory s) internal pure returns (uint256) {
        int256 v = int256(s.cumulativeDelta0) + (int256(s.cumulativeDelta1) * int256(uint256(s.referencePriceWad))) / 1e18;
        return v > 0 ? uint256(v) : 0;
    }

    /// @dev F(a) with the scope's locked kappa, lambda and gamma.
    function _F(ParticipationAwareHook hook, ParticipationAwareHook.Scope memory s, uint256 a) internal view returns (uint256 t) {
        (t,) = hook.computeTransfer(a, s.kappa, uint256(s.lambdaBps) * 1e14, uint256(s.gammaBps) * 1e14, 0);
    }
}

/// @dev Detects whether consecutive calls share a transaction.
contract TransientProbe {
    function set() external {
        assembly ("memory-safe") {
            tstore(0, 1)
        }
    }

    function isSet() external view returns (bool v) {
        assembly ("memory-safe") {
            v := tload(0)
        }
    }
}
