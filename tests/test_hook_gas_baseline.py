"""E2 no-hook baseline (DECISIONS W2) on a synthetic minute grid."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))

from common.pools import POOLS  # noqa: E402
from e2_sequential_replay import make_configs, simulate  # noqa: E402

CFG = {"static_fee_rates": [0.0005], "lambdas": [0.75], "gammas": [0.02], "gas_units": 150000, "hook_overhead_gas": 30032,
       "nohook_baseline": True, "grid_points": 50, "eps_quantile": 0.95}


def grid(T=600, seed=0):
    rng = np.random.default_rng(seed)
    p = 3000 * np.exp(np.cumsum(rng.normal(0, 8e-4, T)))
    return pd.DataFrame({"t": pd.date_range("2026-05-01", periods=T, freq="min", tz="UTC"), "p_ref_0": p, "L": 2e6,
                         "gas_wei": rng.lognormal(np.log(2e9), 1.0, T), "eth_in_num": p, "usd_per_num": 1.0})


def test_nohook_equals_baseline_without_overhead():
    cfg = CFG | {"hook_overhead_gas": 0}
    t = simulate(grid(), POOLS["eth_usdc_005"], make_configs(cfg), cfg, 0.5, 0.0, 0)["table"].set_index("mech")
    cols = ["n_baseline_feasible", "n_executed", "etw_mean", "searcher_net_usd", "mean_lp_vs_hodl_bp"]
    pd.testing.assert_series_equal(t.loc["baseline", cols], t.loc["baseline_nohook", cols], check_names=False)


def test_state_matched_sets_are_nested():
    r = simulate(grid(), POOLS["eth_usdc_005"], make_configs(CFG), CFG, 0.5, 0.0, 0)
    s, t = r["nohook"], r["table"].set_index("mech")
    assert s["act"].sum() > 0
    assert not (s["f180"] & ~s["f150"]).any()                  # F180 is a subset of F150 on every state
    assert (s["f150"] & ~s["f180"]).any()                      # the synthetic gas is high enough to create marginal steps
    assert s["f150"].sum() == t.loc["baseline_nohook", "n_baseline_feasible"]
    assert (make_configs(CFG).set_index("mech")["gas"].drop("baseline_nohook") == 180032).all()
