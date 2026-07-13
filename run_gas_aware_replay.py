"""Run the gas-aware CPMM robustness experiment.

This experiment replays historical ETH/USD price movements through a calibrated
CPMM while using historical gas prices to estimate arbitrage execution costs.

It sweeps over gas-cost multipliers:

    C_t^(m) = m * gasPrice_t * gasUnits * ETHUSD_t

The goal is to test whether retained-surplus, participation-aware sharing
$r_{lambda,gamma}$ preserves arbitrage incentives under increasingly expensive
execution conditions.

Default run:
    python run_gas_aware_replay.py

Example with explicit arguments:
    python run_gas_aware_replay.py \
        --data-dir exp_data \
        --price-file eth_usd_5y.csv \
        --gas-file ether_gas_4y.csv \
        --pool-json ./pool_data/0x4e68ccd3e89f51c3074ca5072bbac773960dfa36.json \
        --gas-units 150000 \
        --reservation-profit 1000 \
        --lambda-values 0.75 \
        --gamma-values 0.05 \
        --gas-multipliers 0.5,1,2,5,10,25,50,100
"""

from __future__ import annotations

import argparse
import inspect
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from cpmm_replay_common import (
    assign_tvl,
    build_price_steps,
    load_eth_price_trace,
    load_gas_trace,
    replay_steps,
    summarize_replay,
)


def parse_float_list(value: str) -> list[float]:
    """Parse a comma-separated list of floats."""
    try:
        return [float(x.strip()) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected comma-separated floats, got: {value}"
        ) from exc


def resolve_input_path(data_dir: Path, maybe_path: str | None) -> Path | None:
    """Resolve an input path.

    If maybe_path is absolute or already exists relative to the current working
    directory, use it as-is. Otherwise, interpret it relative to data_dir.

    This avoids accidentally turning:
        ./pool_data/file.json
    into:
        exp_data/pool_data/file.json
    when the file is actually in ./pool_data.
    """
    if maybe_path is None:
        return None

    path = Path(maybe_path)

    if path.is_absolute():
        return path

    if path.exists():
        return path

    return data_dir / path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gas-aware CPMM robustness replay"
    )

    parser.add_argument(
        "--data-dir",
        default="exp_data",
        help="Directory containing price and gas input data",
    )
    parser.add_argument(
        "--price-file",
        default="eth_usd_5y.csv",
        help="ETH/USD price CSV",
    )
    parser.add_argument(
        "--gas-file",
        default="ether_gas_4y.csv",
        help="Ethereum gas CSV",
    )
    parser.add_argument(
        "--gas-column",
        default="median_gas_price_gwei",
        help="Gas column to use from gas CSV",
    )
    parser.add_argument(
        "--gas-units",
        type=float,
        default=150_000.0,
        help="Baseline gas units per arbitrage transaction",
    )
    parser.add_argument(
        "--gas-multipliers",
        type=parse_float_list,
        default=[0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0],
        help="Comma-separated gas-cost multipliers, e.g. 0.5,1,2,5,10,25,50,100",
    )
    parser.add_argument(
        "--lambda-values",
        type=parse_float_list,
        default=[0.75],
        help="Comma-separated lambda values, e.g. 0.5,0.75,0.95",
    )
    parser.add_argument(
        "--gamma-values",
        type=parse_float_list,
        default=[0.05],
        help=(
            "Comma-separated retained-surplus buffer values for "
            "r_{lambda,gamma}, e.g. 0,0.02,0.05"
        ),
    )
    parser.add_argument(
        "--pool-json",
        default="./pool_data/0x4e68ccd3e89f51c3074ca5072bbac773960dfa36.json",
        help="Optional Uniswap poolDayDatas JSON for TVL calibration",
    )
    parser.add_argument(
        "--fixed-tvl",
        type=float,
        default=None,
        help="Fixed TVL in USD. Overrides median TVL from pool-json",
    )
    parser.add_argument(
        "--use-daily-tvl",
        action="store_true",
        help="Use daily tvlUSD from pool-json instead of fixed median TVL",
    )
    parser.add_argument(
        "--reservation-profit",
        type=float,
        default=1000.0,
        help="Reservation profit in USD",
    )
    parser.add_argument(
        "--output-dir",
        default="results_gas_robustness",
        help="Directory for CSVs and figures",
    )

    return parser.parse_args()



def replay_steps_with_gamma(
    steps: pd.DataFrame,
    lambdas: np.ndarray,
    gammas: np.ndarray,
    gas_units: float,
    reservation_profit: float,
) -> pd.DataFrame:
    """Call replay_steps while supporting both old and gamma-aware helpers.

    The preferred helper API is one of:
        replay_steps(..., gammas=gammas, ...)
        replay_steps(..., gamma_values=gammas, ...)
        replay_steps(..., gamma=<single gamma>, ...)

    If the project helper still has the old API, this function permits only
    gamma=0.0, because otherwise the reported rule would not match the model.
    """

    signature = inspect.signature(replay_steps)
    parameters = set(signature.parameters)

    base_kwargs = {
        "lambdas": lambdas,
        "gas_units": gas_units,
        "reservation_profit": reservation_profit,
    }

    if "gammas" in parameters:
        events = replay_steps(steps, gammas=gammas, **base_kwargs)
    elif "gamma_values" in parameters:
        events = replay_steps(steps, gamma_values=gammas, **base_kwargs)
    elif "gamma" in parameters:
        frames = []
        for gamma in gammas:
            events_g = replay_steps(steps, gamma=float(gamma), **base_kwargs)
            if "gamma" not in events_g.columns:
                events_g["gamma"] = float(gamma)
            frames.append(events_g)
        events = pd.concat(frames, ignore_index=True)
    else:
        if len(gammas) != 1 or float(gammas[0]) != 0.0:
            raise TypeError(
                "cpmm_replay_common.replay_steps does not expose a gamma "
                "argument. Update replay_steps before running retained-surplus "
                "sharing with gamma > 0."
            )
        events = replay_steps(steps, **base_kwargs)
        events["gamma"] = 0.0

    if "gamma" not in events.columns:
        if len(gammas) == 1:
            events["gamma"] = float(gammas[0])
        else:
            raise ValueError(
                "replay_steps accepted multiple gamma values but did not return "
                "a gamma column, so results cannot be grouped correctly."
            )

    return events

def summarize_gas_robustness(events: pd.DataFrame) -> pd.DataFrame:
    """Summarize replay outcomes by gas multiplier, lambda, and rule."""

    # First try the existing project summarizer.
    # If it already preserves gas_multiplier and key metrics, use it.
    try:
        summary = summarize_replay(events)
        needed = {
            "gas_multiplier",
            "lambda",
            "gamma",
            "rule",
            "conditional_participation",
            "lp_recovery_rate",
            "mean_tracking_error",
        }
        if needed.issubset(set(summary.columns)):
            return summary
    except Exception:
        pass

    group_cols = ["gas_multiplier", "lambda", "gamma", "rule"]
    missing_group_cols = [c for c in group_cols if c not in events.columns]
    if missing_group_cols:
        raise ValueError(
            f"Events are missing required grouping columns: {missing_group_cols}"
        )

    def first_existing(cols: list[str]) -> str | None:
        for col in cols:
            if col in events.columns:
                return col
        return None

    baseline_feasible_col = first_existing(
        [
            "baseline_feasible",
            "is_baseline_feasible",
            "baseline_participates",
            "baseline_arbitrage",
        ]
    )
    participates_col = first_existing(
        [
            "participates",
            "arbitrage_participates",
            "did_arbitrage",
            "trade_executes",
        ]
    )
    lp_loss_col = first_existing(
        [
            "lp_loss_without_sharing",
            "lp_loss",
            "loss",
            "arbitrage_loss",
            "lp_arbitrage_loss",
        ]
    )
    recovered_col = first_existing(
        [
            "lp_recovery",
            "shared_amount",
            "recovered",
            "redistribution",
            "shared_surplus",
            "fee_to_lps",
        ]
    )
    tracking_error_col = first_existing(
        [
            "tracking_error",
            "final_tracking_error",
            "price_error",
            "final_price_error",
        ]
    )
    gas_cost_col = first_existing(
        [
            "gas_cost",
            "gas_cost_usd",
            "execution_cost",
            "execution_cost_usd",
        ]
    )

    rows = []

    for keys, group in events.groupby(group_cols, dropna=False):
        gas_multiplier, lambda_value, gamma_value, rule = keys

        row: dict[str, float | str] = {
            "gas_multiplier": float(gas_multiplier),
            "lambda": float(lambda_value),
            "gamma": float(gamma_value),
            "rule": str(rule),
            "n_events": float(len(group)),
        }

        if baseline_feasible_col is not None:
            feasible = group[baseline_feasible_col].astype(bool)
            row["baseline_feasible_rate"] = float(feasible.mean())
            feasible_group = group[feasible]
        else:
            feasible_group = group
            row["baseline_feasible_rate"] = np.nan

        if participates_col is not None:
            if len(feasible_group) > 0:
                row["conditional_participation"] = float(
                    feasible_group[participates_col].astype(bool).mean()
                )
            else:
                row["conditional_participation"] = np.nan
        else:
            row["conditional_participation"] = np.nan

        if lp_loss_col is not None and recovered_col is not None:
            total_loss = float(feasible_group[lp_loss_col].sum())
            total_recovered = float(feasible_group[recovered_col].sum())
            row["total_lp_loss"] = total_loss
            row["total_lp_recovered"] = total_recovered
            row["lp_recovery_rate"] = (
                total_recovered / total_loss if total_loss > 0 else np.nan
            )
        else:
            row["total_lp_loss"] = np.nan
            row["total_lp_recovered"] = np.nan
            row["lp_recovery_rate"] = np.nan

        if tracking_error_col is not None:
            row["mean_tracking_error"] = float(group[tracking_error_col].mean())
        else:
            row["mean_tracking_error"] = np.nan

        if gas_cost_col is not None:
            row["mean_gas_cost"] = float(group[gas_cost_col].mean())
            row["median_gas_cost"] = float(group[gas_cost_col].median())
            row["max_gas_cost"] = float(group[gas_cost_col].max())

        rows.append(row)

    return pd.DataFrame(rows).sort_values(group_cols).reset_index(drop=True)


def _plot_rules_for_metric(
    ax,
    subset: pd.DataFrame,
    metric_col: str,
    metric_label: str,
    include_baseline: bool,
) -> None:
    """Plot one metric panel."""

    plot_subset = subset.copy()

    if not include_baseline:
        plot_subset = plot_subset[plot_subset["rule"] != "baseline"]

    preferred_order = [
        "baseline",
        "retained_surplus",
        "participation_aware_gamma",
        "participation_aware",
        "maximal_capped",
        "capped",
        "unconstrained",
    ]

    for rule in preferred_order:
        rule_group = plot_subset[plot_subset["rule"] == rule].copy()
        if rule_group.empty:
            continue

        rule_group = rule_group.sort_values("gas_multiplier")

        if rule == "baseline":
            ax.plot(
                rule_group["gas_multiplier"],
                rule_group[metric_col],
                marker="o",
                linestyle="--",
                label="baseline",
            )
        else:
            ax.plot(
                rule_group["gas_multiplier"],
                rule_group[metric_col],
                marker="o",
                label=rule,
            )

    # Plot any unexpected rule names too.
    for rule, rule_group in plot_subset.groupby("rule"):
        if rule in preferred_order:
            continue

        rule_group = rule_group.sort_values("gas_multiplier")
        ax.plot(
            rule_group["gas_multiplier"],
            rule_group[metric_col],
            marker="o",
            label=str(rule),
        )

    ax.set_xlabel("Gas-cost multiplier")
    ax.set_ylabel(metric_label)
    ax.grid(True, alpha=0.3)

    if metric_col in {
        "baseline_feasible_rate",
        "lp_recovery_rate",
        "conditional_participation",
    }:
        ax.set_ylim(-0.02, 1.02)


def plot_gas_robustness(summary: pd.DataFrame, output_path: Path) -> None:
    """Plot gas robustness with gas multiplier on the x-axis.

    Panels:
      1. Baseline-feasible arbitrage rate
      2. LP-loss recovery
      3. Conditional participation
      4. Mean final tracking error
    """

    required_cols = {"gas_multiplier", "lambda", "gamma", "rule"}
    missing = required_cols - set(summary.columns)
    if missing:
        raise ValueError(f"Summary is missing required columns: {sorted(missing)}")

    metric_candidates = [
        ("baseline_feasible_rate", "Baseline-feasible rate", True),
        ("lp_recovery_rate", "LP-loss recovery", True),
        ("conditional_participation", "Conditional participation", False),
        ("mean_tracking_error", "Mean final tracking error", False),
    ]

    available_metrics = [
        item for item in metric_candidates if item[0] in summary.columns
    ]

    if not available_metrics:
        raise ValueError("Could not find any plottable metrics in summary.")

    lambda_values = sorted(summary["lambda"].dropna().unique())
    gamma_values = sorted(summary["gamma"].dropna().unique())

    for lambda_value in lambda_values:
        for gamma_value in gamma_values:
            subset = summary[
                (summary["lambda"] == lambda_value)
                & (summary["gamma"] == gamma_value)
            ].copy()

            if subset.empty:
                continue

            fig, axes = plt.subplots(
                nrows=1,
                ncols=len(available_metrics),
                figsize=(4.4 * len(available_metrics), 3.6),
            )

            if len(available_metrics) == 1:
                axes = [axes]

            for ax, (metric_col, metric_label, include_baseline) in zip(
                axes, available_metrics
            ):
                if metric_col == "baseline_feasible_rate":
                    base = (
                        subset[["gas_multiplier", metric_col]]
                        .drop_duplicates()
                        .sort_values("gas_multiplier")
                    )
                    ax.plot(
                        base["gas_multiplier"],
                        base[metric_col],
                        marker="o",
                        linestyle="--",
                        label="baseline feasible",
                    )
                    ax.set_xlabel("Gas-cost multiplier")
                    ax.set_ylabel(metric_label)
                    ax.grid(True, alpha=0.3)
                    ax.set_ylim(-0.02, 1.02)
                else:
                    _plot_rules_for_metric(
                        ax=ax,
                        subset=subset,
                        metric_col=metric_col,
                        metric_label=metric_label,
                        include_baseline=include_baseline,
                    )

            handles, labels = axes[0].get_legend_handles_labels()
            fig.legend(
                handles,
                labels,
                loc="upper center",
                ncol=min(len(labels), 3),
                frameon=True,
                bbox_to_anchor=(0.5, 1.03),
            )

            fig.suptitle(
                f"Gas-aware robustness, lambda={lambda_value:g}, "
                f"gamma={gamma_value:g}",
                y=1.12,
            )
            fig.tight_layout()

            single_panel = len(lambda_values) == 1 and len(gamma_values) == 1
            if single_panel:
                fig.savefig(output_path, dpi=300, bbox_inches="tight")
            else:
                stem = output_path.stem
                suffix = output_path.suffix
                panel_path = output_path.with_name(
                    f"{stem}_lambda_{lambda_value:g}_gamma_{gamma_value:g}{suffix}"
                )
                fig.savefig(panel_path, dpi=300, bbox_inches="tight")

            plt.close(fig)

def main() -> None:
    args = parse_args()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    price_path = resolve_input_path(data_dir, args.price_file)
    gas_path = resolve_input_path(data_dir, args.gas_file)
    pool_json = resolve_input_path(data_dir, args.pool_json)

    if not price_path.exists():
        raise FileNotFoundError(f"Price file not found: {price_path}")

    if not gas_path.exists():
        raise FileNotFoundError(f"Gas file not found: {gas_path}")

    if args.fixed_tvl is None and pool_json is not None and not pool_json.exists():
        raise FileNotFoundError(
            f"Pool JSON not found: {pool_json}. "
            "Either fix --pool-json or use --fixed-tvl."
        )

    prices = load_eth_price_trace(price_path)
    gas = load_gas_trace(gas_path, gas_column=args.gas_column)

    steps = build_price_steps(prices)
    steps = steps.merge(gas, on="date", how="inner")

    if steps.empty:
        raise ValueError(
            "No overlapping dates between price steps and gas data after merge."
        )

    steps = assign_tvl(
        steps,
        fixed_tvl=args.fixed_tvl,
        pool_json=pool_json,
        use_daily_tvl=args.use_daily_tvl,
    )

    lambdas = np.array(args.lambda_values, dtype=float)
    gammas = np.array(args.gamma_values, dtype=float)

    all_events: list[pd.DataFrame] = []

    for gas_multiplier in args.gas_multipliers:
        effective_gas_units = args.gas_units * gas_multiplier

        events_m = replay_steps_with_gamma(
            steps,
            lambdas=lambdas,
            gammas=gammas,
            gas_units=effective_gas_units,
            reservation_profit=args.reservation_profit,
        )

        events_m["gas_multiplier"] = gas_multiplier
        events_m["baseline_gas_units"] = args.gas_units
        events_m["effective_gas_units"] = effective_gas_units

        all_events.append(events_m)

    events = pd.concat(all_events, ignore_index=True)
    summary = summarize_gas_robustness(events)

    events_path = output_dir / "gas_robustness_events.csv"
    summary_path = output_dir / "gas_robustness_summary.csv"
    figure_path = output_dir / "fig_gas_robustness_extended.png"

    events.to_csv(events_path, index=False)
    summary.to_csv(summary_path, index=False)
    plot_gas_robustness(summary, figure_path)

    print(f"wrote {events_path}")
    print(f"wrote {summary_path}")
    print(f"wrote {figure_path}")

    display_cols = [
        col
        for col in [
            "gas_multiplier",
            "lambda",
            "gamma",
            "rule",
            "baseline_feasible_rate",
            "conditional_participation",
            "lp_recovery_rate",
            "mean_tracking_error",
            "mean_gas_cost",
            "median_gas_cost",
            "max_gas_cost",
        ]
        if col in summary.columns
    ]

    print()
    print(summary[display_cols].to_string(index=False))


if __name__ == "__main__":
    main()