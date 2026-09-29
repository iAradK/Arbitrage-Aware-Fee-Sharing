#!/usr/bin/env python3
"""E1: CPMM depth calibration and validation.

Fits virtual depth L (constant-product approximation of the local concentrated liquidity) per pool from
consecutive swaps, using the exact v3/v4 relations (see README): net input / d(sqrtP) or d(1/sqrtP).
Depth at time t is the median over clean swaps in a trailing window of W hours, using only swaps of strictly
earlier blocks. W is chosen on the validation months. The same-day fit and a constant train depth are reported as comparisons.

  python experiments/e1_cpmm_calibration.py --split valid          # calibrate + validate (train, valid)
  python experiments/e1_cpmm_calibration.py --freeze               # freeze the config after validation
  python experiments/e1_cpmm_calibration.py --split test --confirm-frozen
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import plotstyle, reporting  # noqa: E402
from common.plotstyle import POOL_LABEL  # noqa: E402
from common.pools import CACHE, POOLS  # noqa: E402

CFG = ROOT / "experiments" / "configs" / "e1.yml"


def prepare(df: pd.DataFrame, fee: float, cfg: dict) -> pd.DataFrame:
    """Per-swap depth from the input leg and from the output leg (they agree inside one tick range)."""
    d = df[["timestamp", "block", "price", "price_pre", "amount0", "amount1", "amountUSD", "usd_per_num", "split"]].copy()
    d["date"] = d["timestamp"].dt.floor("D")
    d = d[d["price_pre"].notna()].copy()
    sp1, sp0 = np.sqrt(d["price"].to_numpy()), np.sqrt(d["price_pre"].to_numpy())
    a0, a1 = d["amount0"].to_numpy(), d["amount1"].to_numpy()
    t1_in = a1 > 0                                       # token1 flows into the pool, price rises
    with np.errstate(divide="ignore", invalid="ignore"):
        L_in = np.where(t1_in, a1 * (1 - fee) / (sp1 - sp0), a0 * (1 - fee) / (1 / sp1 - 1 / sp0))
        L_out = np.where(t1_in, -a0 / (1 / sp0 - 1 / sp1), -a1 / (sp0 - sp1))
    move = np.abs(np.log(sp1 / sp0))
    ok = np.isfinite(L_in) & np.isfinite(L_out) & (L_in > 0) & (L_out > 0) & (move >= cfg["min_rel_move"])
    d["sp_pre"], d["sp_post"], d["t1_in"], d["move"] = sp0, sp1, t1_in, move
    d["L_in"], d["L_out"] = np.where(ok, L_in, np.nan), np.where(ok, L_out, np.nan)
    d["ratio"] = d["L_in"] / d["L_out"]
    d["clean"] = ok & (np.abs(d["ratio"] - 1) <= cfg["consistency_tol"])
    d["size_usd"] = d["amountUSD"].astype(float)
    return d.reset_index(drop=True)


def same_day_depth(d: pd.DataFrame, days: pd.DatetimeIndex) -> pd.Series:
    """In-sample comparison only: median depth of the swap's own UTC day (uses that day's future swaps)."""
    c = d[d["clean"]]
    return c.groupby("date")["L_in"].median().reindex(days).ffill()


def rolling_depth(d: pd.DataFrame, hours: int, min_swaps: int) -> pd.Series:
    """Median depth over clean swaps in (t - W, t], indexed by swap time. Read it with depth_at()."""
    c = d[d["clean"]]
    s = pd.Series(c["L_in"].to_numpy(), index=pd.DatetimeIndex(c["timestamp"]))
    return s.rolling(pd.Timedelta(hours=hours), min_periods=min_swaps).median().dropna()


def depth_at(series: pd.Series, t) -> np.ndarray:
    """Depth known strictly before each time t (last rolling value stamped earlier than t; earlier blocks only)."""
    idx = series.index.searchsorted(pd.DatetimeIndex(t), side="left") - 1
    out = np.where(idx >= 0, series.to_numpy()[np.clip(idx, 0, None)], np.nan)
    return out


def predict_log_impact(d: pd.DataFrame, L: np.ndarray, fee: float) -> np.ndarray:
    """Predicted ln(p_post/p_pre) for the observed input under depth L (net of the pool fee)."""
    a0, a1 = d["amount0"].to_numpy(), d["amount1"].to_numpy()
    sp0 = d["sp_pre"].to_numpy()
    t1 = d["t1_in"].to_numpy()
    sp_up = sp0 + np.maximum(a1, 0) * (1 - fee) / L
    inv_dn = 1 / sp0 + np.maximum(a0, 0) * (1 - fee) / L
    sp_pred = np.where(t1, sp_up, 1 / inv_dn)
    return 2 * np.log(sp_pred / sp0)


def errors(d: pd.DataFrame, L: np.ndarray, fee: float) -> pd.DataFrame:
    pred = predict_log_impact(d, L, fee)
    obs = np.log(d["price"].to_numpy() / d["price_pre"].to_numpy())
    e = pd.DataFrame({"size_usd": d["size_usd"].to_numpy(), "obs": obs, "pred": pred})
    e["abs_err_bp"] = np.abs(e["pred"] - e["obs"]) * 1e4
    big = np.abs(e["obs"]) >= 2 * 1e-8
    e["rel_err"] = np.where(big, np.abs(e["pred"] - e["obs"]) / np.abs(e["obs"]), np.nan)
    return e


def summarize(e: pd.DataFrame, edges: np.ndarray, model: str, pool: str) -> list[dict]:
    q = np.clip(np.searchsorted(edges, e["size_usd"].to_numpy(), side="right") - 1, 0, 3)
    rows = []
    for name, m in [("all", np.ones(len(e), bool))] + [(f"Q{i+1}", q == i) for i in range(4)]:
        s = e[m]
        rows.append({"pool": pool, "model": model, "size_quartile": name, "n": int(m.sum()),
                     "median_abs_err_bp": float(s["abs_err_bp"].median()), "p95_abs_err_bp": float(s["abs_err_bp"].quantile(.95)),
                     "median_rel_err": float(s["rel_err"].median()), "p95_rel_err": float(s["rel_err"].quantile(.95)),
                     "median_obs_impact_bp": float((s["obs"].abs() * 1e4).median())})
    return rows


def run_pool(key: str, cfg: dict, split: str, out: Path, selected: dict) -> dict:
    pool = POOLS[key]
    df = pd.read_parquet(CACHE / "aligned" / f"{key}.parquet")
    allowed = ["train", "valid"] if split == "valid" else ["train", "valid", "test"]
    df = df[df["split"].isin(allowed)]
    d = prepare(df, pool.fee, cfg)
    days = pd.date_range(d["date"].min(), d["date"].max(), freq="D", tz="UTC").as_unit("ns")

    train = d[d["split"] == "train"]
    edges = np.quantile(train["size_usd"], [0, .25, .5, .75, 1.0])
    L_null = float(train.loc[train["clean"], "L_in"].median())
    ev = d[d["split"] == split].copy()
    ts_ev = pd.DatetimeIndex(ev["timestamp"])
    roll = {h: rolling_depth(d, h, cfg["min_swaps_in_window"]) for h in cfg["window_hours_grid"]}
    L_h = {h: depth_at(roll[h], ts_ev) for h in roll}

    # window selection on validation, reused unchanged for test. Swaps without a defined depth are skipped
    # for selection and counted; the constant train depth is never substituted silently.
    sel_rows, chosen = [], selected.get(key)
    if split == "valid":
        for h in cfg["window_hours_grid"]:
            m = np.isfinite(L_h[h])
            e = errors(ev[m], L_h[h][m], pool.fee)
            sel_rows.append({"pool": key, "window_hours": h, "share_defined": float(m.mean()),
                             "median_abs_err_bp": float(e["abs_err_bp"].median()), "median_rel_err": float(e["rel_err"].median())})
        chosen = int(min(sel_rows, key=lambda r: r["median_rel_err"])["window_hours"])
        selected[key] = chosen
    elif chosen is None:
        raise SystemExit(f"no validated window for {key}; run --split valid first")

    m = np.isfinite(L_h[chosen])
    day_idx = ev["date"].map(lambda t: days.get_loc(t)).to_numpy()
    L_sd = same_day_depth(d, days).to_numpy()[day_idx]
    rows = []
    rows += summarize(errors(ev[m], L_h[chosen][m], pool.fee), edges, f"trailing_{chosen}h", key)
    rows += summarize(errors(ev[m], L_sd[m], pool.fee), edges, "same_day (in-sample)", key)
    rows += summarize(errors(ev[m], np.full(int(m.sum()), L_null), pool.fee), edges, "constant_train", key)

    chk = d[d["split"].isin(["train", "valid"])]
    integ = {"pool": key, "share_valid": float(chk["L_in"].notna().mean()), "share_clean": float(chk["clean"].mean()),
             "median_L_in_over_L_out": float(chk["ratio"].median()), "share_eval_depth_defined": float(m.mean())}
    integ["min_month_clean_share"] = float(chk.assign(mo=chk["date"].dt.strftime("%Y-%m")).groupby("mo")["clean"].mean().min())

    # depth on a minute grid for E2 (evaluated splits only): depth known strictly before each minute
    grid = pd.date_range(d["timestamp"].min().ceil("min"), ev["timestamp"].max().ceil("D") - pd.Timedelta(minutes=1), freq="min").as_unit("ns")
    dm = pd.DataFrame({"timestamp": grid, "L_trailing": depth_at(roll[chosen], grid)})
    dm.to_parquet(out / f"depth_{key}.parquet", index=False)

    # daily view for the figure / README
    daily = pd.DataFrame({"L_trailing": dm.set_index("timestamp")["L_trailing"].resample("D").median()})
    px = d.groupby("date")[["price", "usd_per_num"]].median()
    daily = daily.join(px)
    daily["L_same_day"] = same_day_depth(d, days).reindex(daily.index)
    for c in ("L_trailing", "L_same_day"):
        daily["usd_" + c] = 2 * daily[c] * np.sqrt(daily["price"]) * daily["usd_per_num"]
    return {"rows": rows, "sel": sel_rows, "integ": integ, "depth": daily, "L_null": L_null, "chosen": chosen}


def make_figures(res: dict, out: Path, split: str):
    import matplotlib.pyplot as plt
    plotstyle.apply()
    fig, axs = plt.subplots(2, 2, figsize=(7.2, 5.0), sharex=False)
    for ax, (k, r) in zip(axs.ravel(), res.items()):
        dd = r["depth"]
        ax.plot(dd.index, dd["usd_L_trailing"], label=f"trailing {r['chosen']}h", lw=1.2)
        ax.plot(dd.index, dd["usd_L_same_day"], label="same day (in-sample)", lw=0.7, alpha=.7)
        ax.set_yscale("log"); ax.set_title(POOL_LABEL[k]); ax.set_ylabel("virtual depth 2$y_0$ (USD)")
        ax.tick_params(axis="x", rotation=30)
    axs[0, 0].legend(frameon=False)
    fig.tight_layout()
    reporting.savefig(fig, out / "figures" / f"e1_depth_series_{split}")
    plt.close(fig)

    fig, axs = plt.subplots(1, 4, figsize=(9.5, 2.8), sharey=False)
    for ax, (k, r) in zip(axs, res.items()):
        t = pd.DataFrame(r["rows"])
        t = t[t["size_quartile"] != "all"]
        for i, (m, g) in enumerate(t.groupby("model")):
            ax.plot(g["size_quartile"], g["median_abs_err_bp"], marker="o", label=m)
        ax.set_yscale("log"); ax.set_title(POOL_LABEL[k]); ax.set_xlabel("trade-size quartile")
    axs[0].set_ylabel("median |error| of log impact (bp)")
    axs[0].legend(frameon=False, fontsize=6)
    fig.tight_layout()
    reporting.savefig(fig, out / "figures" / f"e1_error_by_quartile_{split}")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CFG))
    ap.add_argument("--split", choices=["valid", "test"], default="valid")
    ap.add_argument("--confirm-frozen", action="store_true")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--pools", default=None)
    a = ap.parse_args()
    cfg = reporting.load_config(a.config)
    out = reporting.out_dir("e1")
    if a.freeze:
        reporting.freeze("e1", cfg)
        print("config frozen:", reporting.config_hash(cfg)[:12])
        return
    reporting.guard_split("e1", a.split, a.confirm_frozen, cfg)
    sel_path = out / "selected_window.json"
    selected = json.load(open(sel_path)) if sel_path.exists() else {}
    keys = a.pools.split(",") if a.pools else cfg["pools"]
    res = {k: run_pool(k, cfg, a.split, out, selected) for k in keys}
    if a.split == "valid":
        sel_path.write_text(json.dumps(selected))
        reporting.write_table(pd.DataFrame([r for v in res.values() for r in v["sel"]]), out / "tables" / "e1_window_selection")
    tag = a.split
    tab = pd.DataFrame([r for v in res.values() for r in v["rows"]])
    reporting.write_table(tab, out / "tables" / f"e1_error_by_quartile_{tag}", {"median_rel_err": "{:.3f}", "p95_rel_err": "{:.3f}"})
    reporting.write_table(pd.DataFrame([v["integ"] for v in res.values()]), out / "tables" / "e1_chain_integrity",
                          {c: "{:.4f}" for c in ["share_valid", "share_clean", "median_L_in_over_L_out", "min_month_clean_share"]})
    make_figures(res, out, tag)
    reporting.write_manifest("e1", cfg, [CACHE / "aligned" / f"{k}.parquet" for k in keys], a.split,
                             {"selected_window_hours": selected, "L_null": {k: v["L_null"] for k, v in res.items()}})
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(tab[tab["size_quartile"] == "all"].to_string(index=False))
        print(pd.DataFrame([v["integ"] for v in res.values()]).to_string(index=False))


if __name__ == "__main__":
    main()
