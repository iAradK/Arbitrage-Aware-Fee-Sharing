"""Q5 diagnostic (python experiments/eQ5_bucket_diagnostic.py). Diagnostic for Q5's deviation-conditioned eps (ETH/USDC): how candidate error relates to deviation, and where the
observed feasible swaps fall. Read-only; reuses the committed script's functions."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

W = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(W))
sys.path.insert(0, str(W / "experiments"))
import eQ5_buffer_calibration as q5  # noqa: E402
from common import minutegrid as mg, reporting  # noqa: E402
import e2_sequential_replay as e2  # noqa: E402
import eC_same_block_replay as eC  # noqa: E402

key, var = "eth_usdc_005", "raw"
cfg2 = reporting.load_config(W / "experiments" / "configs" / "e2.yml")
edges = pd.read_csv(W / "results" / "eQ5" / "eQ5_bucket_edges_train.csv").query("pool == @key and variant == @var")[["edge_q0.5", "edge_q0.8", "edge_q0.95"]].to_numpy()[0]
R = mg.r_regimes_usd(key, var, cfg2["gas_units"], cfg2["r_quantiles"])["median"]
seed = q5.seed_candidates(key, var, R, 1, 1, "train", cfg2)
g = mg.build_grid(key, "valid", var, lags=(0, 1))
C0 = e2.make_configs(cfg2).iloc[[0]].reset_index(drop=True)
r, act, pp = q5.recorded_simulate(g, key, C0, cfg2, R, 1, 1, pd.DataFrame({"t": seed["t_utc"], "err_usd": seed["err_usd"], "err_signed_usd": seed["err_signed_usd"]}))
c = q5.candidates(g, r, pp, 1)
c = c[c["bf"]]
c["bucket"] = np.digitize(c["dev"], edges)
print("validation candidates: corr(dev, err_rel) =", round(float(np.corrcoef(c["dev"], c["err_rel"])[0, 1]), 3))
print(c.groupby("bucket").agg(n=("err_rel", "size"), dev_median_bp=("dev", lambda v: 1e4 * v.median()),
                              err_median_bp=("err_rel", lambda v: 1e4 * v.median()), err_p95_bp=("err_rel", lambda v: 1e4 * v.quantile(0.95))).round(2))
al = eC.load_swaps(key, var, "valid")
sw = pd.read_csv(W / "results" / "eC" / "eC_swaps_valid.csv.gz", float_precision="round_trip")
df = sw[(sw.pool == key) & (sw.variant == var) & (sw.setting == "buffered_eps_d1")].reset_index(drop=True)
feas = df["margin_usd"].to_numpy() >= 0
dev = np.abs(np.log(al["price_pre"].to_numpy() / al["pi_hook"].to_numpy()))
err = np.abs(al["pi_hook"].to_numpy() / al["pi_bench"].to_numpy() - 1)
b = np.digitize(np.nan_to_num(dev, nan=0.0), edges)
x = pd.DataFrame({"bucket": b[feas], "dev": dev[feas], "err": err[feas]})
print("observed feasible swaps: corr(dev, err) =", round(float(np.corrcoef(np.nan_to_num(x["dev"]), x["err"])[0, 1]), 3))
print(x.groupby("bucket").agg(n=("err", "size"), dev_median_bp=("dev", lambda v: 1e4 * v.median()),
                              err_median_bp=("err", lambda v: 1e4 * v.median()), err_p95_bp=("err", lambda v: 1e4 * v.quantile(0.95))).round(2))

