"""
Section IV-B: Synthetic Participation Trade-off.

This experiment shows the basic failure mode of unconstrained proportional
surplus sharing in a controlled CPMM price-shock setting.

Question:
    Among arbitrage opportunities that are feasible under the baseline AMM,
    does a surplus-sharing rule preserve arbitrage participation?

Mechanisms:
    1. Baseline AMM:
       No surplus sharing.

    2. Unconstrained proportional sharing:
       r(q) = lambda * S(q)

    3. Participation-aware retained-surplus sharing:
       r_{lambda,gamma}(q)
       =
       min{
           lambda * S(q),
           [(1-gamma) * (S(q) - C(q) - R(q))]^+
       }

Main outputs:
    synthetic_ivb_participation_tradeoff/
      single_shock_table.csv
      lambda_sweep_summary.csv
      paper_lambda_table.csv
      fig_ivb_participation_tradeoff.png

Run:
    python synthetic_ivb_participation_tradeoff.py
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math

import pandas as pd
import matplotlib.pyplot as plt


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class ExperimentConfig:
    # CPMM reserves. Initial price is y0 / x0 = 1000 token-Y per token-X.
    x0: float = 1000.0
    y0: float = 1_000_000.0

    # Price shocks around the initial reference price.
    # Dense near 1.0 so marginal arbitrage opportunities appear.
    shock_factors: tuple[float, ...] = (
        0.970, 0.975, 0.980, 0.985, 0.990, 0.995,
        1.005, 1.010, 1.015, 1.020, 1.025, 1.030,
        1.050, 1.100, 1.200, 1.500, 2.000,
    )

    # Lambda sweep for the participation trade-off.
    lambda_values: tuple[float, ...] = tuple(i / 20 for i in range(0, 20))  # 0.00 ... 0.95

    # Retained-surplus buffer.
    # gamma=0 gives the maximal feasible capped transfer.
    # gamma>0 leaves part of the baseline participation margin to the arbitrageur.
    gamma: float = 0.05

    # High-cost setting used to make the participation constraint bind.
    execution_cost: float = 50.0
    reservation_profit: float = 1_000.0

    # Single-shock example used as a sanity-check table.
    example_shock_factor: float = 1.20
    example_lambda: float = 0.75

    # Selected lambda values for the compact table.
    # Include 0.60 because this is where suppression often begins in this setup.
    selected_lambdas: tuple[float, ...] = (0.00, 0.25, 0.50, 0.60, 0.75, 0.95)

    output_dir: str = "synthetic_ivb_participation_tradeoff"


# -----------------------------------------------------------------------------
# CPMM mechanics
# -----------------------------------------------------------------------------

def pool_price(x: float, y: float) -> float:
    return y / x


def target_reserves_after_arbitrage(k: float, p_ref: float) -> tuple[float, float]:
    """
    Reserves after full price correction in a zero-fee CPMM.

    The post-arbitrage pool price satisfies y1 / x1 = p_ref,
    while the invariant x1 * y1 = k is preserved.
    """
    x1 = math.sqrt(k / p_ref)
    y1 = math.sqrt(k * p_ref)
    return x1, y1


def tracking_error(p_pool: float, p_ref: float) -> float:
    return abs(math.log(p_pool / p_ref))


def arbitrage_surplus_y_units(x0: float, y0: float, p_ref: float) -> dict:
    """
    Compute the full-correction arbitrage surplus and LP loss.

    All values are expressed in token-Y units.
    """
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
        # X is cheap in the pool.
        # Arbitrageur pays Y into the pool and receives X.
        delta_y_in = y1 - y0
        delta_x_out = x0 - x1
        surplus = p_ref * delta_x_out - delta_y_in
        direction = "buy_X_from_pool"
    else:
        # X is expensive in the pool.
        # Arbitrageur pays X into the pool and receives Y.
        delta_x_in = x1 - x0
        delta_y_out = y0 - y1
        surplus = delta_y_out - p_ref * delta_x_in
        direction = "sell_X_to_pool"

    # Event-level LP loss from full price-correcting arbitrage.
    # This is the reduction in pool mark-to-market value at the reference price.
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


# -----------------------------------------------------------------------------
# Sharing rules
# -----------------------------------------------------------------------------

def unconstrained_proportional_sharing(S: float, lam: float) -> float:
    return lam * S


def participation_aware_sharing(
    S: float,
    lam: float,
    C: float,
    R: float,
    gamma: float,
) -> float:
    """
    Participation-aware retained-surplus rule.

    r_{lambda,gamma}(q)
    =
    min{
        lambda * S(q),
        [(1-gamma) * S(q) - C(q) - R(q)]^+
    }.
    """
    feasible_cap = max(0.0, (1.0 - gamma) * S - C - R)
    target_transfer = lam * S
    return min(target_transfer, feasible_cap)


# -----------------------------------------------------------------------------
# Event evaluation
# -----------------------------------------------------------------------------

def evaluate_rule(
    *,
    rule_name: str,
    x0: float,
    y0: float,
    p_ref: float,
    S: float,
    L: float,
    lam: float,
    C: float,
    R: float,
    gamma: float,
) -> dict:
    p0 = pool_price(x0, y0)

    if rule_name == "baseline":
        shared = 0.0
    elif rule_name == "unconstrained":
        shared = unconstrained_proportional_sharing(S, lam)
    elif rule_name == "participation_aware":
        shared = participation_aware_sharing(S, lam, C, R, gamma)
    else:
        raise ValueError(f"unknown rule: {rule_name}")

    arb_profit = S - shared - C
    margin_after_sharing = arb_profit - R
    participates = arb_profit >= R

    if participates:
        lp_recovery = min(L, shared)
        lp_residual_loss = max(0.0, L - shared)
        final_price = p_ref
    else:
        # If arbitrage is suppressed, no surplus is redirected and the pool
        # remains at its stale pre-arbitrage price.
        lp_recovery = 0.0
        lp_residual_loss = 0.0
        arb_profit = 0.0
        margin_after_sharing = float("nan")
        final_price = p0

    return {
        "rule": rule_name,
        "shared_amount": shared,
        "participates": participates,
        "lp_recovery": lp_recovery,
        "lp_residual_loss": lp_residual_loss,
        "lp_recovery_rate": (lp_recovery / L) if L > 0 else float("nan"),
        "arb_profit": arb_profit,
        "margin_after_sharing": margin_after_sharing,
        "tracking_error": tracking_error(final_price, p_ref),
    }


def run_single_event(
    *,
    config: ExperimentConfig,
    shock_factor: float,
    lam: float,
) -> list[dict]:
    x0 = config.x0
    y0 = config.y0
    C = config.execution_cost
    R = config.reservation_profit
    gamma = config.gamma

    p0 = pool_price(x0, y0)
    p_ref = p0 * shock_factor

    arb = arbitrage_surplus_y_units(x0, y0, p_ref)
    S = arb["surplus"]
    L = arb["lp_loss"]

    rows = []

    for rule in ("baseline", "unconstrained", "participation_aware"):
        out = evaluate_rule(
            rule_name=rule,
            x0=x0,
            y0=y0,
            p_ref=p_ref,
            S=S,
            L=L,
            lam=lam,
            C=C,
            R=R,
            gamma=gamma,
        )

        out.update({
            "shock_factor": shock_factor,
            "lambda": lam,
            "gamma": gamma,
            "execution_cost": C,
            "reservation_profit": R,
            "initial_price": p0,
            "reference_price": p_ref,
            "direction": arb["direction"],
            "gross_surplus": S,
            "lp_loss_without_sharing": L,
            "baseline_margin": S - C - R,
        })

        rows.append(out)

    return rows


def add_baseline_feasible_flag(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add event-level baseline feasibility.

    Conditional metrics are computed only over arbitrage opportunities that
    execute under the baseline AMM.
    """
    baseline = df[df["rule"] == "baseline"][[
        "lambda",
        "gamma",
        "shock_factor",
        "participates",
    ]].rename(columns={"participates": "baseline_feasible"})

    return df.merge(
        baseline,
        on=["lambda", "gamma", "shock_factor"],
        how="left",
    )


# -----------------------------------------------------------------------------
# Experiments
# -----------------------------------------------------------------------------

def single_shock_table(config: ExperimentConfig) -> pd.DataFrame:
    rows = run_single_event(
        config=config,
        shock_factor=config.example_shock_factor,
        lam=config.example_lambda,
    )

    df = pd.DataFrame(rows)

    cols = [
        "rule",
        "shock_factor",
        "lambda",
        "gamma",
        "gross_surplus",
        "baseline_margin",
        "lp_loss_without_sharing",
        "shared_amount",
        "participates",
        "lp_recovery",
        "lp_recovery_rate",
        "arb_profit",
        "margin_after_sharing",
        "tracking_error",
    ]

    return df[cols]


def summarize_lambda_sweep(config: ExperimentConfig) -> pd.DataFrame:
    rows = []

    for lam in config.lambda_values:
        event_rows = []

        for shock in config.shock_factors:
            event_rows.extend(run_single_event(
                config=config,
                shock_factor=shock,
                lam=lam,
            ))

        df = pd.DataFrame(event_rows)
        df = add_baseline_feasible_flag(df)

        # Keep only nonzero arbitrage opportunities.
        df = df[df["gross_surplus"] > 0].copy()

        num_baseline_feasible = int(
            df[df["rule"] == "baseline"]["baseline_feasible"].sum()
        )

        for rule in ("baseline", "unconstrained", "participation_aware"):
            d = df[df["rule"] == rule].copy()
            d_cond = d[d["baseline_feasible"]].copy()

            total_loss_cond = d_cond["lp_loss_without_sharing"].sum()
            total_recovery_cond = d_cond["lp_recovery"].sum()

            participates_cond = (
                d_cond["participates"].mean()
                if len(d_cond) > 0 else float("nan")
            )

            recovery_rate_cond = (
                total_recovery_cond / total_loss_cond
                if total_loss_cond > 0 else float("nan")
            )

            mean_tracking_error_cond = (
                d_cond["tracking_error"].mean()
                if len(d_cond) > 0 else float("nan")
            )

            max_tracking_error_cond = (
                d_cond["tracking_error"].max()
                if len(d_cond) > 0 else float("nan")
            )

            mean_profit_executed = (
                d.loc[d["participates"], "arb_profit"].mean()
                if d["participates"].any() else float("nan")
            )

            mean_margin_executed = (
                d.loc[d["participates"], "margin_after_sharing"].mean()
                if d["participates"].any() else float("nan")
            )

            rows.append({
                "lambda": lam,
                "gamma": config.gamma,
                "rule": rule,

                "num_opportunities": len(d),
                "num_baseline_feasible": num_baseline_feasible,
                "num_executed_all": int(d["participates"].sum()),
                "num_executed_conditional": int(d_cond["participates"].sum()),

                "conditional_participation_rate": participates_cond,

                "total_lp_recovery_conditional": total_recovery_cond,
                "total_lp_loss_conditional": total_loss_cond,
                "aggregate_lp_recovery_rate_conditional": recovery_rate_cond,

                "mean_tracking_error_conditional": mean_tracking_error_cond,
                "max_tracking_error_conditional": max_tracking_error_cond,

                "mean_arb_profit_executed": mean_profit_executed,
                "mean_margin_after_sharing_executed": mean_margin_executed,
            })

    return pd.DataFrame(rows)


def make_paper_lambda_table(
    summary: pd.DataFrame,
    selected_lambdas: tuple[float, ...],
) -> pd.DataFrame:
    table = summary[
        (summary["lambda"].isin(selected_lambdas)) &
        (summary["rule"].isin(["unconstrained", "participation_aware"]))
    ].copy()

    cols = [
        "lambda",
        "gamma",
        "rule",
        "aggregate_lp_recovery_rate_conditional",
        "conditional_participation_rate",
        "mean_tracking_error_conditional",
        "num_executed_conditional",
        "num_baseline_feasible",
        "mean_margin_after_sharing_executed",
    ]

    return table[cols].sort_values(["lambda", "rule"])


# -----------------------------------------------------------------------------
# Plot helpers
# -----------------------------------------------------------------------------

def rule_label(rule: str, gamma: float) -> str:
    labels = {
        "baseline": "Baseline AMM",
        "unconstrained": r"Unconstrained $\lambda S(q)$",
        "participation_aware": rf"Participation-aware $r_{{\lambda,\gamma}}$, $\gamma={gamma:g}$",
    }
    return labels[rule]


def first_unconstrained_failure_lambda(summary: pd.DataFrame) -> float | None:
    d = summary[
        (summary["rule"] == "unconstrained") &
        (summary["conditional_participation_rate"] < 1.0)
    ].sort_values("lambda")

    if d.empty:
        return None

    return float(d.iloc[0]["lambda"])


def add_failure_marker(ax, lambda_fail: float | None) -> None:
    if lambda_fail is None:
        return

    ax.axvline(lambda_fail, linestyle="--", linewidth=1)

    ymin, ymax = ax.get_ylim()
    ax.text(
        lambda_fail,
        ymax - 0.06 * (ymax - ymin),
        "first suppression",
        rotation=90,
        va="top",
        ha="right",
        fontsize=8,
    )


def plot_ivb_combined(
    *,
    summary: pd.DataFrame,
    output_path: Path,
    gamma: float,
) -> None:
    """
    Main paper figure for IV-B.

    All metrics are conditional on baseline-feasible arbitrage opportunities.
    """
    fig, axes = plt.subplots(1, 3, figsize=(14, 3.6))
    lambda_fail = first_unconstrained_failure_lambda(summary)

    specs = [
        {
            "metric": "aggregate_lp_recovery_rate_conditional",
            "ylabel": "Conditional LP recovery rate",
            "title": "(a) LP recovery",
            "rules": ("unconstrained", "participation_aware"),
        },
        {
            "metric": "conditional_participation_rate",
            "ylabel": "Conditional participation rate",
            "title": "(b) Arbitrage participation",
            "rules": ("baseline", "unconstrained", "participation_aware"),
        },
        {
            "metric": "mean_tracking_error_conditional",
            "ylabel": "Mean final log error",
            "title": "(c) Price tracking",
            "rules": ("baseline", "unconstrained", "participation_aware"),
        },
    ]

    for ax, spec in zip(axes, specs):
        for rule in spec["rules"]:
            d = summary[summary["rule"] == rule].sort_values("lambda")

            ax.plot(
                d["lambda"],
                d[spec["metric"]],
                marker="o",
                label=rule_label(rule, gamma),
            )

        add_failure_marker(ax, lambda_fail)
        ax.set_xlabel(r"Surplus-sharing parameter $\lambda$")
        ax.set_ylabel(spec["ylabel"])
        ax.set_title(spec["title"])

    axes[0].set_ylim(-0.05, 1.05)
    axes[1].set_ylim(-0.05, 1.05)

    # One shared legend saves space.
    handles, labels = axes[1].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        loc="lower center",
        ncol=3,
        fontsize=8,
        frameon=True,
        bbox_to_anchor=(0.5, -0.08),
    )

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.24)
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> None:
    config = ExperimentConfig()

    out = Path(config.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Sanity-check table for one representative shock.
    single_table = single_shock_table(config)
    single_table.to_csv(out / "single_shock_table.csv", index=False)

    # Main lambda sweep.
    summary = summarize_lambda_sweep(config)
    summary.to_csv(out / "lambda_sweep_summary.csv", index=False)

    # Compact table for paper text.
    paper_table = make_paper_lambda_table(summary, config.selected_lambdas)
    paper_table.to_csv(out / "paper_lambda_table.csv", index=False)

    # Main IV-B figure.
    plot_ivb_combined(
        summary=summary,
        output_path=out / "fig_ivb_participation_tradeoff.png",
        gamma=config.gamma,
    )

    lambda_fail = first_unconstrained_failure_lambda(summary)

    print("\nSection IV-B: Synthetic Participation Trade-off")
    print("-----------------------------------------------")
    print(f"Initial reserves: x0={config.x0}, y0={config.y0}")
    print(f"Initial price: {pool_price(config.x0, config.y0):.4f}")
    print(f"Execution cost C: {config.execution_cost}")
    print(f"Reservation profit R: {config.reservation_profit}")
    print(f"Gamma: {config.gamma}")
    print(f"Outputs saved to: {out}")

    if lambda_fail is not None:
        print(
            "First lambda where unconstrained sharing suppresses "
            f"baseline-feasible arbitrage: {lambda_fail:.2f}"
        )
    else:
        print(
            "Unconstrained sharing never suppresses baseline-feasible "
            "arbitrage in this sweep."
        )

    print("\nSingle-shock sanity table:")
    print(single_table.round(6).to_string(index=False))

    print("\nPaper-ready lambda table:")
    print(paper_table.round(6).to_string(index=False))


if __name__ == "__main__":
    main()