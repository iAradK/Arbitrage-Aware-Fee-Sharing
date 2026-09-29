#!/usr/bin/env python3
"""Redraw every experiment figure from saved outputs, without recomputing any experiment.

Reads results/e1 tables and depth files, results/e2 summaries (for E3), results/e4 summaries and results/e5 summary, and
calls each experiment's own plotting function. Use it after a style change (for example the x-tick rule in
common/plotstyle.ensure_xticks). It never re-evaluates the test split.

    python experiments/redraw_figures.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

from common import reporting  # noqa: E402
from common.pools import CACHE, POOLS, TEST_START, VALID_START  # noqa: E402

import e1_cpmm_calibration as e1  # noqa: E402
import e4_oracle_robustness as e4  # noqa: E402
import e5_size_dependent_costs as e5  # noqa: E402

RES = ROOT / "results"


def redraw_e1():
    cfg = reporting.load_config(ROOT / "experiments" / "configs" / "e1.yml")
    out = reporting.out_dir("e1")
    chosen = json.load(open(out / "selected_window.json"))
    for split, end in (("valid", TEST_START), ("test", None)):
        tab = out / "tables" / f"e1_error_by_quartile_{split}.csv"
        if not tab.exists():
            continue
        rows = pd.read_csv(tab)
        res = {}
        for key in cfg["pools"]:
            df = pd.read_parquet(CACHE / "aligned" / f"{key}.parquet")
            df = df[df["split"].isin(["train", "valid"] if split == "valid" else ["train", "valid", "test"])]
            d = e1.prepare(df, POOLS[key].fee, cfg)
            days = pd.date_range(d["date"].min(), d["date"].max(), freq="D", tz="UTC").as_unit("ns")
            dm = pd.read_parquet(out / f"depth_{key}.parquet")
            if end is not None:
                dm = dm[dm["timestamp"] < end]
            daily = pd.DataFrame({"L_trailing": dm.set_index("timestamp")["L_trailing"].resample("D").median()})
            daily = daily.join(d.groupby("date")[["price", "usd_per_num"]].median())
            daily["L_same_day"] = e1.same_day_depth(d, days).reindex(daily.index)
            for c in ("L_trailing", "L_same_day"):
                daily["usd_" + c] = 2 * daily[c] * np.sqrt(daily["price"]) * daily["usd_per_num"]
            res[key] = {"rows": rows[rows["pool"] == key].to_dict("records"), "depth": daily, "chosen": chosen[key]}
        e1.make_figures(res, out, split)
        print("e1", split)


def redraw_e3():
    for f in sorted((RES / "e2").glob("e2_summary_*.parquet")):
        tag = f.stem.removeprefix("e2_summary_")
        subprocess.run([sys.executable, str(ROOT / "experiments" / "e3_pareto_frontier.py"), "--tag", tag], check=True,
                       capture_output=True)
        print("e3", tag)


def redraw_e4():
    cfg = reporting.load_config(ROOT / "experiments" / "configs" / "e4.yml")
    out = reporting.out_dir("e4")
    for split in ("valid", "test"):
        f = out / f"e4_summary_{split}.csv"
        if f.exists():
            e4.figs(pd.read_csv(f), out, split, cfg)
            print("e4", split)


def redraw_e5():
    cfg = reporting.load_config(ROOT / "experiments" / "configs" / "e5.yml")
    out = reporting.out_dir("e5")
    e5.figs(pd.read_parquet(out / "e5_summary.parquet"), out, cfg)
    print("e5")


if __name__ == "__main__":
    redraw_e1()
    redraw_e3()
    redraw_e4()
    redraw_e5()
