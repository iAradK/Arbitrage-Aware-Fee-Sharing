"""Shared CPMM replay utilities for trace-driven and gas-aware experiments.

Gamma-aware version.

The experiment treats each consecutive price movement as one independent CPMM
arbitrage opportunity. At each step, the pool is initialized at the observed
price p_t and calibrated to a chosen TVL. The external reference price then
moves to p_{t+1}.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd

RULES = ("baseline", "unconstrained", "participation_aware")


def pool_price(x: float, y: float) -> float:
    return y / x


def target_reserves_after_arbitrage(k: float, p_ref: float) -> tuple[float, float]:
    return math.sqrt(k / p_ref), math.sqrt(k * p_ref)


def tracking_error(p_pool: float, p_ref: float) -> float:
    if p_pool <= 0 or p_ref <= 0:
        return float("nan")
    return abs(math.log(p_pool / p_ref))


def reserves_from_tvl(price: float, tvl_usd: float) -> tuple[float, float]:
    """Return x and y reserves for a two-sided ETH/stable CPMM."""
    if price <= 0 or tvl_usd <= 0:
        raise ValueError("price and tvl_usd must be positive")
    x0 = (tvl_usd / 2.0) / price
    y0 = tvl_usd / 2.0
    return x0, y0


def arbitrage_surplus_y_units(x0: float, y0: float, p_ref: float) -> dict[str, float | str]:
    """Return optimal arbitrage surplus and LP arbitrage loss in token-Y units."""
    p0 = pool_price(x0, y0)
    k = x0 * y0

    if math.isclose(p_ref, p0, rel_tol=1e-12, abs_tol=1e-12):
        return {
            "direction": "none",
            "x1": x0,
            "y1": y0,
            "surplus": 0.0,
            "lp_loss": 0.0,
        }

    x1, y1 = target_reserves_after_arbitrage(k, p_ref)

    if p_ref > p0:
        # ETH is cheap in the pool. Arbitrageur pays stablecoin and receives ETH.
        delta_y_in = y1 - y0
        delta_x_out = x0 - x1
        surplus = p_ref * delta_x_out - delta_y_in
        direction = "buy_x_from_pool"
    else:
        # ETH is expensive in the pool. Arbitrageur pays ETH and receives stablecoin.
        delta_x_in = x1 - x0
        delta_y_out = y0 - y1
        surplus = delta_y_out - p_ref * delta_x_in
        direction = "sell_x_to_pool"

    value_before = x0 * p_ref + y0
    value_after = x1 * p_ref + y1
    lp_loss = max(0.0, value_before - value_after)

    return {
        "direction": direction,
        "x1": x1,
        "y1": y1,
        "surplus": max(0.0, surplus),
        "lp_loss": lp_loss,
    }


def sharing_amount(
    rule: str,
    surplus: float,
    lam: float,
    execution_cost: float,
    reservation_profit: float,
    gamma: float = 0.0,
) -> float:
    """Return transfer to LPs under the selected sharing rule.

    participation_aware implements

        r_{lambda,gamma}(q)
        = min{lambda*S(q), [(1-gamma)*(S(q)-C(q)-R(q))]^+}.

    gamma=0 recovers the old maximal capped transfer.
    gamma>0 leaves a gamma fraction of the baseline participation margin to the arbitrageur.
    """
    if not 0.0 <= gamma <= 1.0:
        raise ValueError(f"gamma must be in [0,1], got {gamma}")

    if rule == "baseline":
        return 0.0
    if rule == "unconstrained":
        return lam * surplus
    if rule == "participation_aware":
        participation_margin = surplus - execution_cost - reservation_profit
        capped_transfer = max(0.0, (1.0 - gamma) * participation_margin)
        target_transfer = lam * surplus
        return min(target_transfer, capped_transfer)
    raise ValueError(f"unknown rule: {rule}")


def evaluate_rule(
    *,
    rule: str,
    x0: float,
    y0: float,
    p_ref: float,
    surplus: float,
    lp_loss: float,
    lam: float,
    execution_cost: float,
    reservation_profit: float,
    gamma: float = 0.0,
) -> dict[str, float | bool | str]:
    p0 = pool_price(x0, y0)
    shared = sharing_amount(
        rule=rule,
        surplus=surplus,
        lam=lam,
        execution_cost=execution_cost,
        reservation_profit=reservation_profit,
        gamma=gamma,
    )

    tol = 1e-9 * max(1.0, abs(reservation_profit), abs(surplus))
    arb_profit_before_reservation = surplus - shared - execution_cost
    participation_margin_after_sharing = arb_profit_before_reservation - reservation_profit
    participates = participation_margin_after_sharing >= -tol

    if participates:
        lp_recovery = min(lp_loss, shared)
        arb_profit = arb_profit_before_reservation
        final_price = p_ref
    else:
        lp_recovery = 0.0
        arb_profit = 0.0
        final_price = p0

    return {
        "rule": rule,
        "shared_amount": shared,
        "participates": participates,
        "lp_recovery": lp_recovery,
        "lp_recovery_rate": lp_recovery / lp_loss if lp_loss > 0 else float("nan"),
        "arb_profit": arb_profit,
        "participation_margin_after_sharing": participation_margin_after_sharing if participates else float("nan"),
        "tracking_error": tracking_error(final_price, p_ref),
    }


def load_eth_price_trace(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    candidates = ["eth_usd", "ETH", "close", "Close", "price", "Price"]
    price_col = next((c for c in candidates if c in df.columns), None)
    if price_col is None:
        raise ValueError(f"Could not find an ETH price column in {path}. Columns are {list(df.columns)}")

    date_col = "timestamp" if "timestamp" in df.columns else "date"
    if date_col not in df.columns:
        raise ValueError(f"Could not find timestamp/date column in {path}. Columns are {list(df.columns)}")

    out = df[[date_col, price_col]].copy()
    out.columns = ["date", "price"]
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out = out.dropna().sort_values("date").drop_duplicates("date")
    out = out[out["price"] > 0]
    return out.reset_index(drop=True)


def load_gas_trace(path: str | Path, gas_column: str = "median_gas_price_gwei") -> pd.DataFrame:
    df = pd.read_csv(path)
    if "date" not in df.columns:
        raise ValueError(f"Could not find date column in {path}. Columns are {list(df.columns)}")
    if gas_column not in df.columns:
        raise ValueError(f"Could not find {gas_column} in {path}. Columns are {list(df.columns)}")

    out = df[["date", gas_column]].copy()
    out.columns = ["date", "gas_price_gwei"]
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out["gas_price_gwei"] = pd.to_numeric(out["gas_price_gwei"], errors="coerce")
    out = out.dropna().sort_values("date").drop_duplicates("date")
    return out.reset_index(drop=True)


def load_pool_day_data(path: str | Path) -> pd.DataFrame:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    rows = raw.get("data", {}).get("poolDayDatas", [])
    if not rows:
        raise ValueError(f"No data.poolDayDatas rows found in {path}")
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(pd.to_numeric(df["date"]), unit="s").dt.normalize()
    for col in ["tvlUSD", "volumeUSD", "token0Price", "token1Price"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def median_tvl_from_pool_json(path: str | Path) -> float:
    df = load_pool_day_data(path)
    tvl = df["tvlUSD"].dropna()
    if tvl.empty:
        raise ValueError(f"No tvlUSD values found in {path}")
    return float(tvl.median())


def build_price_steps(price_df: pd.DataFrame) -> pd.DataFrame:
    df = price_df.copy().sort_values("date")
    df["next_date"] = df["date"].shift(-1)
    df["next_price"] = df["price"].shift(-1)
    df = df.dropna(subset=["next_price"]).copy()
    df["shock_factor"] = df["next_price"] / df["price"]
    df = df[(df["price"] > 0) & (df["next_price"] > 0)]
    return df.reset_index(drop=True)


def read_table_auto(path: Path) -> pd.DataFrame:
    """Read CSV, TSV, or whitespace-separated table."""
    return pd.read_csv(path, sep=None, engine="python")


def assign_tvl(
    steps: pd.DataFrame,
    fixed_tvl: float | None = None,
    pool_json: Path | None = None,
    use_daily_tvl: bool = False,
    monthly_tvl: Path | None = None,
    tvl_columns: list[str] | None = None,
    monthly_start: str = "2021-05-01",
    tvl_aggregation: str = "median",
    fill_missing_tvl: bool = False,
) -> pd.DataFrame:
    """Assign TVL to each replay step.

    Priority:
        1. fixed_tvl
        2. monthly_tvl
        3. pool_json
    """
    steps = steps.copy()

    if fixed_tvl is not None:
        steps["tvl_usd"] = float(fixed_tvl)
        return steps

    if monthly_tvl is not None:
        monthly_path = Path(monthly_tvl)
        if not monthly_path.exists():
            raise FileNotFoundError(f"Monthly TVL file not found: {monthly_path}")

        tvl = read_table_auto(monthly_path)

        if "month" not in tvl.columns:
            raise ValueError(f"Monthly TVL file must contain a 'month' column. Columns: {list(tvl.columns)}")

        if tvl_columns is None or len(tvl_columns) == 0:
            tvl_columns = [c for c in tvl.columns if c.startswith("tvl_")]

        missing = [c for c in tvl_columns if c not in tvl.columns]
        if missing:
            raise ValueError(f"Missing TVL columns in {monthly_path}: {missing}. Available columns: {list(tvl.columns)}")

        tvl = tvl.copy()
        tvl["month"] = pd.to_numeric(tvl["month"], errors="coerce")
        tvl = tvl.dropna(subset=["month"])
        tvl["month"] = tvl["month"].astype(int)

        for col in tvl_columns:
            tvl[col] = pd.to_numeric(tvl[col], errors="coerce")

        if tvl_aggregation == "median":
            tvl["tvl_usd"] = tvl[tvl_columns].median(axis=1, skipna=True)
        elif tvl_aggregation == "mean":
            tvl["tvl_usd"] = tvl[tvl_columns].mean(axis=1, skipna=True)
        elif tvl_aggregation == "sum":
            tvl["tvl_usd"] = tvl[tvl_columns].sum(axis=1, skipna=True)
        else:
            raise ValueError(f"Unsupported tvl_aggregation: {tvl_aggregation}")

        start = pd.Timestamp(monthly_start).to_period("M")
        tvl["month_period"] = tvl["month"].apply(lambda m: start + int(m) - 1)

        steps["month_period"] = pd.to_datetime(steps["date"]).dt.to_period("M")
        steps = steps.merge(
            tvl[["month_period", "tvl_usd"]],
            on="month_period",
            how="left",
        )

        if fill_missing_tvl:
            steps["tvl_usd"] = steps["tvl_usd"].ffill().bfill()
        else:
            before = len(steps)
            steps = steps.dropna(subset=["tvl_usd"])
            dropped = before - len(steps)
            if dropped > 0:
                print(f"dropped {dropped} replay steps outside monthly TVL coverage")

        steps = steps.drop(columns=["month_period"])
        return steps

    if pool_json is not None:
        pool_path = Path(pool_json)
        if not pool_path.exists():
            raise FileNotFoundError(f"Pool JSON file not found: {pool_path}")

        pool = load_pool_day_data(pool_path)

        if use_daily_tvl:
            pool = pool[["date", "tvlUSD"]].rename(columns={"tvlUSD": "tvl_usd"})
            pool["date"] = pd.to_datetime(pool["date"]).dt.date
            steps["date_only"] = pd.to_datetime(steps["date"]).dt.date
            steps = steps.merge(pool, left_on="date_only", right_on="date", how="left")
            steps = steps.drop(columns=["date_y", "date_only"]).rename(columns={"date_x": "date"})
            steps["tvl_usd"] = steps["tvl_usd"].ffill().bfill()
        else:
            median_tvl = float(pd.to_numeric(pool["tvlUSD"], errors="coerce").median())
            steps["tvl_usd"] = median_tvl

        return steps

    raise ValueError(
        "No TVL calibration was provided. Use --fixed-tvl, --monthly-tvl, or --pool-json."
    )


def replay_steps(
    steps: pd.DataFrame,
    *,
    lambdas: Iterable[float],
    reservation_profit: float,
    fixed_execution_cost: float | None = None,
    gas_units: float | None = None,
    gammas: Iterable[float] | None = None,
    gamma_values: Iterable[float] | None = None,
    gamma: float | None = None,
) -> pd.DataFrame:
    """Replay independent CPMM arbitrage events.

    Supports both old gamma=0 behavior and retained-surplus sharing with gamma>0.
    Accepted gamma APIs are gammas=[...], gamma_values=[...], or gamma=<float>.
    """
    if gammas is not None and gamma_values is not None:
        raise ValueError("Pass either gammas or gamma_values, not both")
    if gammas is not None and gamma is not None:
        raise ValueError("Pass either gammas or gamma, not both")
    if gamma_values is not None and gamma is not None:
        raise ValueError("Pass either gamma_values or gamma, not both")

    if gammas is not None:
        gamma_list = [float(g) for g in gammas]
    elif gamma_values is not None:
        gamma_list = [float(g) for g in gamma_values]
    elif gamma is not None:
        gamma_list = [float(gamma)]
    else:
        gamma_list = [0.0]

    rows: list[dict] = []
    for event_id, row in steps.iterrows():
        p0 = float(row["price"])
        p_ref = float(row["next_price"])
        if "tvl_usd" in row:
            tvl = float(row["tvl_usd"])
        elif "tvlUSD" in row:
            tvl = float(row["tvlUSD"])
        else:
            raise KeyError("Expected TVL column 'tvl_usd' or 'tvlUSD'")
        x0, y0 = reserves_from_tvl(p0, tvl)
        arb = arbitrage_surplus_y_units(x0, y0, p_ref)
        surplus = float(arb["surplus"])
        lp_loss = float(arb["lp_loss"])

        if fixed_execution_cost is not None:
            execution_cost = float(fixed_execution_cost)
        else:
            if gas_units is None or "gas_price_gwei" not in row:
                raise ValueError("gas-aware replay requires gas_units and gas_price_gwei")
            execution_cost = float(gas_units) * float(row["gas_price_gwei"]) * 1e-9 * p0

        tol = 1e-9 * max(1.0, abs(reservation_profit), abs(surplus))
        baseline_feasible = (surplus - execution_cost) >= reservation_profit - tol
        baseline_participation_margin = surplus - execution_cost - reservation_profit

        for lam in lambdas:
            for gam in gamma_list:
                for rule in RULES:
                    out = evaluate_rule(
                        rule=rule,
                        x0=x0,
                        y0=y0,
                        p_ref=p_ref,
                        surplus=surplus,
                        lp_loss=lp_loss,
                        lam=float(lam),
                        execution_cost=execution_cost,
                        reservation_profit=reservation_profit,
                        gamma=float(gam),
                    )
                    out.update({
                        "event_id": event_id,
                        "date": row["date"],
                        "next_date": row["next_date"],
                        "lambda": float(lam),
                        "gamma": float(gam),
                        "initial_price": p0,
                        "reference_price": p_ref,
                        "shock_factor": float(row["shock_factor"]),
                        "tvlUSD": tvl,
                        "execution_cost": execution_cost,
                        "reservation_profit": reservation_profit,
                        "gross_surplus": surplus,
                        "lp_loss_without_sharing": lp_loss,
                        "direction": arb["direction"],
                        "baseline_feasible": baseline_feasible,
                        "baseline_participation_margin": baseline_participation_margin,
                    })
                    if "gas_price_gwei" in row:
                        out["gas_price_gwei"] = float(row["gas_price_gwei"])
                    rows.append(out)
    return pd.DataFrame(rows)


def summarize_replay(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    group_cols = ["rule", "lambda"]
    if "gamma" in events.columns:
        group_cols.append("gamma")

    for keys, d in events.groupby(group_cols, sort=True):
        if len(group_cols) == 2:
            rule, lam = keys
            gam = 0.0
        else:
            rule, lam, gam = keys

        d_cond = d[d["baseline_feasible"]].copy()
        total_loss = d_cond["lp_loss_without_sharing"].sum()
        total_recovery = d_cond["lp_recovery"].sum()
        rows.append({
            "rule": rule,
            "lambda": lam,
            "gamma": gam,
            "n_events": int(len(d)),
            "n_baseline_feasible": int(len(d_cond)),
            "conditional_participation": float(d_cond["participates"].mean()) if len(d_cond) else float("nan"),
            "lp_recovery_rate": float(total_recovery / total_loss) if total_loss > 0 else float("nan"),
            "total_lp_loss": float(total_loss),
            "total_lp_recovery": float(total_recovery),
            "mean_tracking_error": float(d_cond["tracking_error"].mean()) if len(d_cond) else float("nan"),
            "p95_tracking_error": float(d_cond["tracking_error"].quantile(0.95)) if len(d_cond) else float("nan"),
            "mean_execution_cost": float(d_cond["execution_cost"].mean()) if len(d_cond) else float("nan"),
            "mean_participation_margin_after_sharing": float(d_cond["participation_margin_after_sharing"].mean()) if len(d_cond) and "participation_margin_after_sharing" in d_cond else float("nan"),
        })

    return pd.DataFrame(rows).sort_values(group_cols).reset_index(drop=True)


def plot_lambda_tradeoff(summary: pd.DataFrame, output_path: str | Path, title_prefix: str) -> None:
    labels = {
        "baseline": "Baseline AMM",
        "unconstrained": "Unconstrained",
        "participation_aware": "Participation-aware",
    }
    metrics = [
        ("lp_recovery_rate", "LP recovery rate"),
        ("conditional_participation", "Conditional participation rate"),
        ("mean_tracking_error", "Mean final log error"),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(12.0, 3.2))
    for ax, (metric, ylabel) in zip(axes, metrics):
        for rule in ["baseline", "unconstrained", "participation_aware"]:
            d = summary[summary["rule"] == rule]
            if metric == "lp_recovery_rate" and rule == "baseline":
                continue
            ax.plot(d["lambda"], d[metric], marker="o", linewidth=1.8, markersize=3.5, label=labels[rule])
        ax.set_xlabel(r"$\lambda$")
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.25)
    axes[0].set_title("(a) LP recovery")
    axes[1].set_title("(b) Arbitrage participation")
    axes[2].set_title("(c) Price tracking")
    axes[2].legend(frameon=False, fontsize=8)
    fig.suptitle(title_prefix, y=1.04, fontsize=11)
    fig.tight_layout()
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)