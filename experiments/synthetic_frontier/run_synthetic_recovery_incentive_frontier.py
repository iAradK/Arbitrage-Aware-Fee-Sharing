"""Run the synthetic recovery--incentive frontier experiment.

This experiment is intended for Section V-C.

Purpose:
    Show the role of the retained-surplus parameter gamma.

Main idea:
    The capped rule with gamma=0 can preserve participation but may flatten the
    arbitrageur's payoff over a range of trades. A positive gamma leaves a small
    share of surplus to the arbitrageur and restores a stronger incentive to
    choose the full price-correcting trade.

Default usage:
    python run_synthetic_recovery_incentive_frontier.py

Example:
    python run_synthetic_recovery_incentive_frontier.py \
        --lambda-value 0.75 \
        --gammas 0,0.005,0.01,0.02,0.05,0.10 \
        --shock-factor 1.10 \
        --cost 50 \
        --reservation-profit 1000 \
        --tie-breaker smallest
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synthetic recovery--incentive frontier for CPMM arbitrage"
    )

    # Synthetic pool setup
    parser.add_argument(
        "--x0",
        type=float,
        default=1000.0,
        help="Initial reserve of token X, e.g., WETH",
    )
    parser.add_argument(
        "--y0",
        type=float,
        default=1_000_000.0,
        help="Initial reserve of token Y, e.g., USDT",
    )
    parser.add_argument(
        "--shock-factor",
        type=float,
        default=1.10,
        help="External reference price multiplier p_ref / p0",
    )

    # Mechanism parameters
    parser.add_argument(
        "--lambda-value",
        type=float,
        default=0.75,
        help="Surplus-sharing intensity lambda",
    )
    parser.add_argument(
        "--gammas",
        default="0,0.005,0.01,0.02,0.05,0.10",
        help="Comma-separated gamma values to evaluate",
    )
    parser.add_argument(
        "--cost",
        type=float,
        default=50.0,
        help="Fixed execution cost C(q), in token-Y/USD units",
    )
    parser.add_argument(
        "--reservation-profit",
        type=float,
        default=1000.0,
        help="Reservation profit R(q), in token-Y/USD units",
    )

    # Numerical search
    parser.add_argument(
        "--grid-size",
        type=int,
        default=5001,
        help="Number of candidate trade sizes between no trade and full correction",
    )
    parser.add_argument(
        "--tie-breaker",
        default="smallest",
        choices=["smallest", "largest"],
        help=(
            "Tie-breaking among payoff-equivalent best responses. "
            "'smallest' exposes the weak-incentive problem at gamma=0. "
            "'largest' assumes the arbitrageur chooses the most price-correcting trade."
        ),
    )

    # Output
    parser.add_argument(
        "--output-dir",
        default="results",
        help="Directory for CSVs and figures",
    )

    return parser.parse_args()


def parse_float_list(raw: str) -> list[float]:
    return [float(x.strip()) for x in raw.split(",") if x.strip()]


def pool_price(x: float, y: float) -> float:
    return y / x


def tracking_error(p_pool: float, p_ref: float) -> float:
    if p_pool <= 0 or p_ref <= 0:
        return float("nan")
    return abs(math.log(p_pool / p_ref))


def target_reserves_after_arbitrage(k: float, p_ref: float) -> tuple[float, float]:
    """Return reserves at the CPMM state whose marginal price equals p_ref."""
    return math.sqrt(k / p_ref), math.sqrt(k * p_ref)


def surplus_for_trade(
    *,
    x0: float,
    y0: float,
    p_ref: float,
    trade_size: float,
    direction: str,
) -> tuple[float, float, float]:
    """Return gross surplus and final reserves for a candidate CPMM arbitrage trade.

    trade_size is measured in the input token:
        - if direction == "buy_x_from_pool", trade_size is delta_y_in
        - if direction == "sell_x_to_pool", trade_size is delta_x_in
    """
    k = x0 * y0

    if direction == "buy_x_from_pool":
        delta_y_in = trade_size
        y1 = y0 + delta_y_in
        x1 = k / y1
        delta_x_out = x0 - x1
        surplus = p_ref * delta_x_out - delta_y_in
        return max(0.0, surplus), x1, y1

    if direction == "sell_x_to_pool":
        delta_x_in = trade_size
        x1 = x0 + delta_x_in
        y1 = k / x1
        delta_y_out = y0 - y1
        surplus = delta_y_out - p_ref * delta_x_in
        return max(0.0, surplus), x1, y1

    raise ValueError(f"unknown direction: {direction}")


def full_correction_trade(
    *,
    x0: float,
    y0: float,
    p_ref: float,
) -> dict[str, float | str]:
    """Compute the full price-correcting arbitrage trade q*."""
    p0 = pool_price(x0, y0)
    k = x0 * y0
    x_star, y_star = target_reserves_after_arbitrage(k, p_ref)

    if math.isclose(p0, p_ref, rel_tol=1e-12, abs_tol=1e-12):
        return {
            "direction": "none",
            "q_star": 0.0,
            "x_star": x0,
            "y_star": y0,
            "s_star": 0.0,
            "lp_loss": 0.0,
        }

    if p_ref > p0:
        direction = "buy_x_from_pool"
        q_star = y_star - y0
    else:
        direction = "sell_x_to_pool"
        q_star = x_star - x0

    s_star, _, _ = surplus_for_trade(
        x0=x0,
        y0=y0,
        p_ref=p_ref,
        trade_size=q_star,
        direction=direction,
    )

    value_before = x0 * p_ref + y0
    value_after = x_star * p_ref + y_star
    lp_loss = max(0.0, value_before - value_after)

    return {
        "direction": direction,
        "q_star": q_star,
        "x_star": x_star,
        "y_star": y_star,
        "s_star": s_star,
        "lp_loss": lp_loss,
    }


def retained_surplus_transfer(
    *,
    surplus: float,
    lam: float,
    gamma: float,
    execution_cost: float,
    reservation_profit: float,
) -> float:
    """Compute r_{lambda,gamma}(q).

    r(q) = min{
        lambda * S(q),
        [(1 - gamma) * (S(q) - C(q) - R(q))]^+
    }

    With gamma=0, this is the maximal participation-preserving cap.
    With gamma>0, the arbitrageur keeps a positive fraction of surplus above
    execution cost and reservation profit.
    """
    proportional = lam * surplus
    capped = max(
        0.0,
        (1.0 - gamma) * (surplus - execution_cost - reservation_profit),
    )
    return min(proportional, capped)


def is_better_best_response(
    *,
    candidate: dict[str, float],
    best: dict[str, float],
    tie_breaker: str,
) -> bool:
    """Return True if candidate should replace current best response."""
    payoff_tol = 1e-9 * max(
        1.0,
        abs(candidate["payoff"]),
        abs(best["payoff"]),
    )

    if candidate["payoff"] > best["payoff"] + payoff_tol:
        return True

    if abs(candidate["payoff"] - best["payoff"]) <= payoff_tol:
        if tie_breaker == "smallest":
            return candidate["q"] < best["q"]
        if tie_breaker == "largest":
            return candidate["q"] > best["q"]
        raise ValueError(f"unknown tie_breaker: {tie_breaker}")

    return False


def evaluate_gamma(
    *,
    x0: float,
    y0: float,
    p_ref: float,
    lam: float,
    gamma: float,
    execution_cost: float,
    reservation_profit: float,
    grid_size: int,
    tie_breaker: str,
) -> dict[str, float | str | bool]:
    """Evaluate one gamma value by solving the arbitrageur best response."""
    full = full_correction_trade(x0=x0, y0=y0, p_ref=p_ref)

    direction = str(full["direction"])
    q_star = float(full["q_star"])
    s_star = float(full["s_star"])
    lp_loss = float(full["lp_loss"])

    if direction == "none" or q_star <= 0 or s_star <= 0:
        final_price = pool_price(x0, y0)
        return {
            "lambda": lam,
            "gamma": gamma,
            "direction": direction,
            "q_star": q_star,
            "q_br": 0.0,
            "correction_fraction": 0.0,
            "s_star": s_star,
            "s_br": 0.0,
            "trade_quality": float("nan"),
            "shared_amount": 0.0,
            "lp_recovery": 0.0,
            "lp_recovery_rate": float("nan"),
            "arb_payoff": 0.0,
            "participates": False,
            "final_price": final_price,
            "tracking_error": tracking_error(final_price, p_ref),
            "lp_loss_without_sharing": lp_loss,
            "tie_breaker": tie_breaker,
        }

    candidate_q = np.linspace(0.0, q_star, grid_size)

    best: dict[str, float] | None = None

    for q in candidate_q:
        surplus, x1, y1 = surplus_for_trade(
            x0=x0,
            y0=y0,
            p_ref=p_ref,
            trade_size=float(q),
            direction=direction,
        )

        shared = retained_surplus_transfer(
            surplus=surplus,
            lam=lam,
            gamma=gamma,
            execution_cost=execution_cost,
            reservation_profit=reservation_profit,
        )

        payoff = surplus - shared - execution_cost

        participates = payoff >= reservation_profit - 1e-9 * max(
            1.0,
            abs(reservation_profit),
            abs(surplus),
        )

        if not participates:
            continue

        candidate = {
            "q": float(q),
            "surplus": float(surplus),
            "shared": float(shared),
            "payoff": float(payoff),
            "x1": float(x1),
            "y1": float(y1),
        }

        if best is None or is_better_best_response(
            candidate=candidate,
            best=best,
            tie_breaker=tie_breaker,
        ):
            best = candidate

    if best is None:
        final_price = pool_price(x0, y0)
        return {
            "lambda": lam,
            "gamma": gamma,
            "direction": direction,
            "q_star": q_star,
            "q_br": 0.0,
            "correction_fraction": 0.0,
            "s_star": s_star,
            "s_br": 0.0,
            "trade_quality": 0.0,
            "shared_amount": 0.0,
            "lp_recovery": 0.0,
            "lp_recovery_rate": 0.0,
            "arb_payoff": 0.0,
            "participates": False,
            "final_price": final_price,
            "tracking_error": tracking_error(final_price, p_ref),
            "lp_loss_without_sharing": lp_loss,
            "tie_breaker": tie_breaker,
        }

    q_br = float(best["q"])
    s_br = float(best["surplus"])
    shared = float(best["shared"])
    final_price = pool_price(float(best["x1"]), float(best["y1"]))

    lp_recovery = min(lp_loss, shared)

    return {
        "lambda": lam,
        "gamma": gamma,
        "direction": direction,
        "q_star": q_star,
        "q_br": q_br,
        "correction_fraction": q_br / q_star if q_star > 0 else float("nan"),
        "s_star": s_star,
        "s_br": s_br,
        "trade_quality": s_br / s_star if s_star > 0 else float("nan"),
        "shared_amount": shared,
        "lp_recovery": lp_recovery,
        "lp_recovery_rate": lp_recovery / lp_loss if lp_loss > 0 else float("nan"),
        "arb_payoff": float(best["payoff"]),
        "participates": True,
        "final_price": final_price,
        "tracking_error": tracking_error(final_price, p_ref),
        "lp_loss_without_sharing": lp_loss,
        "tie_breaker": tie_breaker,
    }


def plot_frontier(summary: pd.DataFrame, output_path: str | Path) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(12.0, 3.2))

    axes[0].plot(
        summary["gamma"],
        summary["lp_recovery_rate"],
        marker="o",
        linewidth=1.8,
        markersize=4,
    )
    axes[0].set_xlabel(r"$\gamma$")
    axes[0].set_ylabel("LP recovery rate")
    axes[0].set_title("(a) LP recovery")
    axes[0].grid(True, alpha=0.25)

    axes[1].plot(
        summary["gamma"],
        summary["trade_quality"],
        marker="o",
        linewidth=1.8,
        markersize=4,
    )
    axes[1].set_xlabel(r"$\gamma$")
    axes[1].set_ylabel(r"$S(q^{BR})/S(q^*)$")
    axes[1].set_title("(b) Trade quality")
    axes[1].grid(True, alpha=0.25)

    axes[2].plot(
        summary["gamma"],
        summary["tracking_error"],
        marker="o",
        linewidth=1.8,
        markersize=4,
    )
    axes[2].set_xlabel(r"$\gamma$")
    axes[2].set_ylabel("Final log price error")
    axes[2].set_title("(c) Price tracking")
    axes[2].grid(True, alpha=0.25)

    fig.tight_layout()
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_payoff_curve(
    *,
    x0: float,
    y0: float,
    p_ref: float,
    lam: float,
    gammas: list[float],
    execution_cost: float,
    reservation_profit: float,
    grid_size: int,
    output_path: str | Path,
) -> None:
    """Optional diagnostic plot of arbitrageur payoff as a function of trade size."""
    full = full_correction_trade(x0=x0, y0=y0, p_ref=p_ref)
    direction = str(full["direction"])
    q_star = float(full["q_star"])

    if direction == "none" or q_star <= 0:
        return

    candidate_q = np.linspace(0.0, q_star, grid_size)

    fig, ax = plt.subplots(figsize=(5.0, 3.2))

    for gamma in gammas:
        payoffs = []
        for q in candidate_q:
            surplus, _, _ = surplus_for_trade(
                x0=x0,
                y0=y0,
                p_ref=p_ref,
                trade_size=float(q),
                direction=direction,
            )
            shared = retained_surplus_transfer(
                surplus=surplus,
                lam=lam,
                gamma=gamma,
                execution_cost=execution_cost,
                reservation_profit=reservation_profit,
            )
            payoffs.append(surplus - shared - execution_cost)

        ax.plot(
            candidate_q / q_star,
            payoffs,
            linewidth=1.8,
            label=rf"$\gamma={gamma:g}$",
        )

    ax.axhline(
        reservation_profit,
        linestyle="--",
        linewidth=1.2,
        label="reservation profit",
    )
    ax.set_xlabel(r"Correction fraction $q/q^*$")
    ax.set_ylabel("Arbitrageur payoff")
    ax.set_title("Best-response payoff curve")
    ax.grid(True, alpha=0.25)
    ax.legend(frameon=False, fontsize=8)

    fig.tight_layout()
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    args = parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    x0 = float(args.x0)
    y0 = float(args.y0)
    p0 = pool_price(x0, y0)
    p_ref = p0 * float(args.shock_factor)

    lam = float(args.lambda_value)
    gammas = parse_float_list(args.gammas)

    rows = []
    for gamma in gammas:
        row = evaluate_gamma(
            x0=x0,
            y0=y0,
            p_ref=p_ref,
            lam=lam,
            gamma=float(gamma),
            execution_cost=float(args.cost),
            reservation_profit=float(args.reservation_profit),
            grid_size=int(args.grid_size),
            tie_breaker=str(args.tie_breaker),
        )
        rows.append(row)

    summary = pd.DataFrame(rows)

    summary_path = output_dir / "synthetic_recovery_incentive_frontier.csv"
    fig_path = output_dir / "fig_synthetic_recovery_incentive_frontier.png"
    payoff_fig_path = output_dir / "fig_synthetic_payoff_curve.png"

    summary.to_csv(summary_path, index=False)
    plot_frontier(summary, fig_path)
    plot_payoff_curve(
        x0=x0,
        y0=y0,
        p_ref=p_ref,
        lam=lam,
        gammas=gammas,
        execution_cost=float(args.cost),
        reservation_profit=float(args.reservation_profit),
        grid_size=int(args.grid_size),
        output_path=payoff_fig_path,
    )

    print(f"Initial price: {p0:.6f}")
    print(f"Reference price: {p_ref:.6f}")
    print(f"Shock factor: {args.shock_factor:.6f}")
    print(f"lambda: {lam:.4f}")
    print(f"tie_breaker: {args.tie_breaker}")
    print()
    print(f"wrote {summary_path}")
    print(f"wrote {fig_path}")
    print(f"wrote {payoff_fig_path}")
    print()
    print(
        summary[
            [
                "gamma",
                "lp_recovery_rate",
                "trade_quality",
                "tracking_error",
                "correction_fraction",
                "arb_payoff",
                "shared_amount",
                "participates",
            ]
        ].to_string(index=False)
    )


if __name__ == "__main__":
    main()