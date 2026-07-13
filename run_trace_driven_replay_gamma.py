"""Run the trace-driven CPMM replay experiment with gamma-aware sharing.

This script is intended for the historical trace-driven experiment.

Default usage:
    python run_trace_driven_replay_gamma.py

This uses:
    exp_data/eth_usd_5y.csv
    exp_data/pool_data/tvl_monthly_end.dat
    TVL columns tvl_4e,tvl_11

These correspond to the two WETH/USDT pools:
    4e  WETH/USDT  0.30%
    11  WETH/USDT  0.05%

The experiment compares:
    baseline
    unconstrained:          r(q) = lambda * S(q)
    capped_gamma0:          r_{lambda,0}(q)
    buffered_gamma:         r_{lambda,gamma}(q)

Example:
    python run_trace_driven_replay_gamma.py \
        --data-dir exp_data \
        --price-file eth_usd_5y.csv \
        --monthly-tvl pool_data/tvl_monthly_end.dat \
        --tvl-columns tvl_4e,tvl_11 \
        --monthly-start 2021-05-01 \
        --cost 50 \
        --reservation-profit 1000 \
        --gamma 0.05
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from cpmm_replay_common import (
    arbitrage_surplus_y_units,
    assign_tvl,
    build_price_steps,
    load_eth_price_trace,
    pool_price,
    reserves_from_tvl,
    tracking_error,
)


RULES = (
    "baseline",
    "unconstrained",
    "capped_gamma0",
    "buffered_gamma",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trace-driven CPMM replay with gamma-aware sharing"
    )

    # Input files
    parser.add_argument(
        "--data-dir",
        default="exp_data",
        help="Directory containing input data",
    )
    parser.add_argument(
        "--price-file",
        default="eth_usd_5y.csv",
        help="ETH/USD price CSV relative to data-dir",
    )

    # Preferred TVL calibration
    parser.add_argument(
        "--monthly-tvl",
        default="pool_data/tvl_monthly_end.dat",
        help="Monthly end-of-period TVL file relative to data-dir",
    )
    parser.add_argument(
        "--tvl-columns",
        default="tvl_4e,tvl_11",
        help="Comma-separated TVL columns used for calibration",
    )
    parser.add_argument(
        "--monthly-start",
        default="2021-05-01",
        help="Date corresponding to month 1 in the monthly TVL file",
    )
    parser.add_argument(
        "--tvl-aggregation",
        default="median",
        choices=["median", "mean", "sum"],
        help="How to aggregate selected TVL columns",
    )
    parser.add_argument(
        "--fill-missing-tvl",
        action="store_true",
        help="Forward/backward fill missing monthly TVL values",
    )

    # Optional fallback calibration
    parser.add_argument(
        "--fixed-tvl",
        type=float,
        default=None,
        help="Fixed TVL in USD. Overrides monthly TVL when provided",
    )
    parser.add_argument(
        "--pool-json",
        default=None,
        help="Optional Uniswap poolDayDatas JSON relative to data-dir",
    )
    parser.add_argument(
        "--use-daily-tvl",
        action="store_true",
        help="Use daily tvlUSD from pool-json instead of monthly/fixed TVL",
    )

    # Mechanism parameters
    parser.add_argument(
        "--cost",
        type=float,
        default=50.0,
        help="Fixed execution cost in USD",
    )
    parser.add_argument(
        "--reservation-profit",
        type=float,
        default=1000.0,
        help="Reservation profit in USD",
    )
    parser.add_argument(
        "--gamma",
        type=float,
        default=0.05,
        help="Retained-surplus cushion used for buffered_gamma",
    )

    # Lambda sweep
    parser.add_argument("--lambda-min", type=float, default=0.0)
    parser.add_argument("--lambda-max", type=float, default=0.95)
    parser.add_argument("--lambda-step", type=float, default=0.05)

    # Output
    parser.add_argument(
        "--output-dir",
        default="results_trace_replay_gamma",
        help="Directory for CSVs and figures",
    )

    return parser.parse_args()


def parse_column_list(columns: str) -> list[str]:
    return [col.strip() for col in columns.split(",") if col.strip()]


def sharing_amount(
    *,
    rule: str,
    surplus: float,
    lam: float,
    gamma: float,
    execution_cost: float,
    reservation_profit: float,
) -> float:
    """Compute the transfer to LPs for a given rule.

    baseline:
        r(q) = 0

    unconstrained:
        r(q) = lambda * S(q)

    capped_gamma0:
        r_{lambda,0}(q)
        = min{lambda S(q), [S(q) - C - R]^+}

    buffered_gamma:
        r_{lambda,gamma}(q)
        = min{lambda S(q), [(1 - gamma)(S(q) - C - R)]^+}
    """
    if rule == "baseline":
        return 0.0

    if rule == "unconstrained":
        return lam * surplus

    if rule == "capped_gamma0":
        return min(
            lam * surplus,
            max(0.0, surplus - execution_cost - reservation_profit),
        )

    if rule == "buffered_gamma":
        return min(
            lam * surplus,
            max(
                0.0,
                (1.0 - gamma) * (surplus - execution_cost - reservation_profit),
            ),
        )

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
    gamma: float,
    execution_cost: float,
    reservation_profit: float,
) -> dict[str, float | bool | str]:
    """Evaluate whether the full price-correcting arbitrage trade participates."""
    p0 = pool_price(x0, y0)

    shared = sharing_amount(
        rule=rule,
        surplus=surplus,
        lam=lam,
        gamma=gamma,
        execution_cost=execution_cost,
        reservation_profit=reservation_profit,
    )

    tol = 1e-9 * max(1.0, abs(reservation_profit), abs(surplus))
    participates = (surplus - shared - execution_cost) >= reservation_profit - tol

    if participates:
        lp_recovery = min(lp_loss, shared)
        arb_profit = surplus - shared - execution_cost
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
        "tracking_error": tracking_error(final_price, p_ref),
    }


def replay_steps_with_gamma(
    steps: pd.DataFrame,
    *,
    lambdas: Iterable[float],
    gamma: float,
    reservation_profit: float,
    fixed_execution_cost: float,
) -> pd.DataFrame:
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
        execution_cost = float(fixed_execution_cost)

        tol = 1e-9 * max(1.0, abs(reservation_profit), abs(surplus))
        baseline_feasible = (surplus - execution_cost) >= reservation_profit - tol

        for lam in lambdas:
            for rule in RULES:
                out = evaluate_rule(
                    rule=rule,
                    x0=x0,
                    y0=y0,
                    p_ref=p_ref,
                    surplus=surplus,
                    lp_loss=lp_loss,
                    lam=float(lam),
                    gamma=float(gamma),
                    execution_cost=execution_cost,
                    reservation_profit=reservation_profit,
                )

                out.update(
                    {
                        "event_id": event_id,
                        "date": row["date"],
                        "next_date": row["next_date"],
                        "lambda": float(lam),
                        "gamma": float(gamma) if rule == "buffered_gamma" else 0.0,
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
                    }
                )

                rows.append(out)

    return pd.DataFrame(rows)


def summarize_replay(events: pd.DataFrame) -> pd.DataFrame:
    rows = []

    for (rule, lam), d in events.groupby(["rule", "lambda"], sort=True):
        d_cond = d[d["baseline_feasible"]].copy()

        total_loss = d_cond["lp_loss_without_sharing"].sum()
        total_recovery = d_cond["lp_recovery"].sum()

        rows.append(
            {
                "rule": rule,
                "lambda": lam,
                "n_events": int(len(d)),
                "n_baseline_feasible": int(len(d_cond)),
                "conditional_participation": (
                    float(d_cond["participates"].mean())
                    if len(d_cond)
                    else float("nan")
                ),
                "lp_recovery_rate": (
                    float(total_recovery / total_loss)
                    if total_loss > 0
                    else float("nan")
                ),
                "total_lp_loss": float(total_loss),
                "total_lp_recovery": float(total_recovery),
                "mean_tracking_error": (
                    float(d_cond["tracking_error"].mean())
                    if len(d_cond)
                    else float("nan")
                ),
                "p95_tracking_error": (
                    float(d_cond["tracking_error"].quantile(0.95))
                    if len(d_cond)
                    else float("nan")
                ),
                "mean_execution_cost": (
                    float(d_cond["execution_cost"].mean())
                    if len(d_cond)
                    else float("nan")
                ),
            }
        )

    return pd.DataFrame(rows).sort_values(["rule", "lambda"]).reset_index(drop=True)


def plot_lambda_tradeoff(
    summary: pd.DataFrame,
    output_path: str | Path,
    gamma: float,
) -> None:
    labels = {
        "baseline": "Baseline AMM",
        "unconstrained": "Unconstrained",
        "capped_gamma0": r"$r_{\lambda,0}$",
        "buffered_gamma": rf"$r_{{\lambda,{gamma:g}}}$",
    }

    plot_order = [
        "unconstrained",
        "capped_gamma0",
        "buffered_gamma",
    ]

    metrics = [
        ("lp_recovery_rate", "LP recovery rate"),
        ("conditional_participation", "Conditional participation rate"),
        ("mean_tracking_error", "Mean final log error"),
    ]

    fig, axes = plt.subplots(1, 3, figsize=(12.0, 3.2))

    for ax, (metric, ylabel) in zip(axes, metrics):
        for rule in plot_order:
            d = summary[summary["rule"] == rule]
            ax.plot(
                d["lambda"],
                d[metric],
                marker="o",
                linewidth=1.8,
                markersize=3.5,
                label=labels[rule],
            )

        if metric in {"conditional_participation", "mean_tracking_error"}:
            d_base = summary[summary["rule"] == "baseline"]
            ax.plot(
                d_base["lambda"],
                d_base[metric],
                linestyle="--",
                linewidth=1.4,
                label=labels["baseline"],
            )

        ax.set_xlabel(r"$\lambda$")
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.25)

    axes[0].set_title("(a) LP recovery")
    axes[1].set_title("(b) Arbitrage participation")
    axes[2].set_title("(c) Price tracking")
    axes[2].legend(frameon=False, fontsize=8)

    fig.tight_layout()
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    args = parse_args()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    price_path = data_dir / args.price_file

    prices = load_eth_price_trace(price_path)
    steps = build_price_steps(prices)

    # TVL calibration priority:
    # 1. fixed TVL, if explicitly provided
    # 2. monthly TVL file, default for the paper
    # 3. legacy pool JSON, optional fallback
    if args.fixed_tvl is not None:
        steps = assign_tvl(
            steps,
            fixed_tvl=args.fixed_tvl,
            pool_json=None,
            use_daily_tvl=False,
        )

    elif args.monthly_tvl is not None:
        steps = assign_tvl(
            steps,
            monthly_tvl=data_dir / args.monthly_tvl,
            tvl_columns=parse_column_list(args.tvl_columns),
            monthly_start=args.monthly_start,
            tvl_aggregation=args.tvl_aggregation,
            fill_missing_tvl=args.fill_missing_tvl,
        )

    else:
        pool_json = data_dir / args.pool_json if args.pool_json else None
        steps = assign_tvl(
            steps,
            fixed_tvl=None,
            pool_json=pool_json,
            use_daily_tvl=args.use_daily_tvl,
        )

    lambdas = np.round(
        np.arange(args.lambda_min, args.lambda_max + 1e-9, args.lambda_step),
        10,
    )

    events = replay_steps_with_gamma(
        steps,
        lambdas=lambdas,
        gamma=float(args.gamma),
        fixed_execution_cost=float(args.cost),
        reservation_profit=float(args.reservation_profit),
    )

    summary = summarize_replay(events)

    events_path = output_dir / "trace_replay_gamma_events.csv"
    summary_path = output_dir / "trace_replay_gamma_lambda_summary.csv"
    fig_path = output_dir / "fig_trace_gamma_lambda_tradeoff.png"

    events.to_csv(events_path, index=False)
    summary.to_csv(summary_path, index=False)

    plot_lambda_tradeoff(
        summary,
        fig_path,
        gamma=float(args.gamma),
    )

    print(f"wrote {events_path}")
    print(f"wrote {summary_path}")
    print(f"wrote {fig_path}")

    selected_lambdas = [0.5, 0.75, 0.95]
    selected = summary[summary["lambda"].isin(selected_lambdas)]
    if not selected.empty:
        print()
        print(selected.to_string(index=False))


if __name__ == "__main__":
    main()