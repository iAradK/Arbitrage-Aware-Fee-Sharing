"""Q5 criteria table (python experiments/eQ5_criteria_table.py): joins the base, grid and dev outputs of
eQ5_buffer_calibration.py into one row per pool and eps variant with the user's criteria. ETH/USDC: tipped
violations (a) < 104, ideally about 40; five-block survival (SW, N = 5) >= 75%; unsplit transfer within 5% of p95.
Read-only on the stored outputs; writes results/eQ5/eQ5_criteria_valid.csv."""
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "results" / "eQ5"
TAGS = ["", "_grid", "_dev"]


def load(stem):
    fs = [OUT / f"eQ5_{stem}{t}_valid.csv" for t in TAGS if (OUT / f"eQ5_{stem}{t}_valid.csv").exists()]
    x = pd.concat([pd.read_csv(f).assign(source=f.name) for f in fs], ignore_index=True)
    return x.drop_duplicates(["pool", "variant", "eps_variant"] + (["N"] if "N" in x else []), keep="first")


obs, cb = load("observed_swaps"), load("crossblock")
w = cb.pivot_table(index=["pool", "variant", "eps_variant"], columns="N", values="surviving_sum_weighted")
w.columns = [f"surv_SW_N{c}" for c in w.columns]
u = cb[cb.N == 1].set_index(["pool", "variant", "eps_variant"])[["unsplit_transfer_usd", "n_charged_1"]]
t = obs.set_index(["pool", "variant", "eps_variant"])[
    ["a_tipped_V1", "b_spillover_n_V1", "b_spillover_usd_V1", "coverage_overestimation_feasible", "transfer_V1_usd_all",
     "transfer_V1_usd_ex_event", "eps_median", "buffer_median_usd_feasible"]].join(u).join(w.drop(columns="surv_SW_N1"))
ref = t.xs("p95", level="eps_variant")
t["unsplit_vs_p95"] = t["unsplit_transfer_usd"] / ref["unsplit_transfer_usd"].reindex(t.index.droplevel(2)).to_numpy() - 1
t["observed_ex_event_vs_p95"] = t["transfer_V1_usd_ex_event"] / ref["transfer_V1_usd_ex_event"].reindex(t.index.droplevel(2)).to_numpy() - 1
t = t.reset_index()
usdc = (t.pool == "eth_usdc_005")
t["meets_all_usdc"] = usdc & (t.a_tipped_V1 < 104) & (t.surv_SW_N5 >= 0.75) & (t.unsplit_vs_p95 >= -0.05)
t.to_csv(OUT / "eQ5_criteria_valid.csv", index=False)
cols = ["pool", "variant", "eps_variant", "a_tipped_V1", "b_spillover_n_V1", "coverage_overestimation_feasible",
        "unsplit_transfer_usd", "unsplit_vs_p95", "surv_SW_N2", "surv_SW_N5", "surv_SW_N75", "transfer_V1_usd_ex_event",
        "observed_ex_event_vs_p95", "meets_all_usdc"]
with pd.option_context("display.width", 300, "display.max_rows", 200, "display.max_columns", 30):
    print(t[cols].round(3).to_string(index=False))
