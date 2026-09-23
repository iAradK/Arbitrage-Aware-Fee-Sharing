#!/usr/bin/env python3
"""Evaluate independent-callback and cumulative split-swap accounting.

For every historical event and fragment count, the script compares:

1. the canonical unsplit transfer;
2. the old rule applied independently to every fragment; and
3. the transaction-scoped cumulative watermark rule.

Both equal partitions and an optimized adversarial partition are evaluated.
The adversarial optimizer is exact for the script's scalar, piecewise-linear
transfer model: all but at most one fragment lie at a domain boundary or a
breakpoint of F. When ``--minimum-fragment-usd`` is zero, the result should be
interpreted as the infimum attainable with arbitrarily small fragments.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

try:
    from hook_replay import (
        get_numeric,
        parse_floats,
        transfer_scalar,
        validate_parameters,
    )
except ModuleNotFoundError:
    # ChatGPT downloads may append "(1)" to a filename that already exists.
    # The fallback keeps the individually downloaded files runnable while the
    # canonical project layout continues to use python/hook_replay.py.
    candidates = (
        Path(__file__).with_name("hook_replay.py"),
        Path(__file__).with_name("hook_replay(1).py"),
        Path(__file__).resolve().with_name("hook_replay.py"),
        Path(__file__).resolve().with_name("hook_replay(1).py"),
    )
    fallback = next((path for path in candidates if path.exists()), None)
    if fallback is None:
        raise
    spec = importlib.util.spec_from_file_location("hook_replay", fallback)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load reference model: {fallback}")
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    get_numeric = reference.get_numeric
    parse_floats = reference.parse_floats
    transfer_scalar = reference.transfer_scalar
    validate_parameters = reference.validate_parameters


def parse_ints(value: str) -> list[int]:
    counts = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not counts or any(count < 1 for count in counts):
        raise argparse.ArgumentTypeError("fragment counts must be positive integers")
    return sorted(set(counts))


def independent_transfer(
    parts: Iterable[float],
    execution_margin_hat: float,
    lam: float,
    gamma: float,
    delta: float,
) -> float:
    return sum(
        transfer_scalar(part, execution_margin_hat, lam, gamma, delta)[0]
        for part in parts
    )


def equal_partition(total: float, count: int) -> list[float]:
    if count == 1:
        return [total]
    part = total / count
    return [part] * (count - 1) + [total - part * (count - 1)]


def transfer_breakpoints(
    execution_margin_hat: float,
    lam: float,
    gamma: float,
    delta: float,
    minimum_fragment: float,
) -> list[float]:
    """Return the feasible breakpoints of the scalar transfer function."""
    threshold = execution_margin_hat + delta
    points = {minimum_fragment}
    if threshold >= minimum_fragment:
        points.add(threshold)

    retained_slope = 1.0 - gamma
    if retained_slope > lam:
        crossover = retained_slope * threshold / (retained_slope - lam)
        if crossover >= minimum_fragment and math.isfinite(crossover):
            points.add(crossover)
    return sorted(points)


def adversarial_partition(
    total: float,
    count: int,
    execution_margin_hat: float,
    lam: float,
    gamma: float,
    delta: float,
    minimum_fragment: float,
) -> tuple[list[float], float]:
    """Minimize the old independent-callback transfer over scalar partitions."""
    if total < count * minimum_fragment - 1e-12:
        raise ValueError(
            "total surplus is smaller than count * minimum fragment"
        )
    if count == 1:
        parts = [total]
        return parts, independent_transfer(
            parts,
            execution_margin_hat,
            lam,
            gamma,
            delta,
        )

    best_parts = equal_partition(total, count)
    best_value = independent_transfer(
        best_parts,
        execution_margin_hat,
        lam,
        gamma,
        delta,
    )
    breakpoints = transfer_breakpoints(
        execution_margin_hat,
        lam,
        gamma,
        delta,
        minimum_fragment,
    )

    # A minimum of a separable piecewise-linear objective over the partition
    # simplex has a representative with all but at most one coordinate at a
    # breakpoint. Enumerate the counts assigned to each breakpoint.
    def search(
        point_index: int,
        remaining_slots: int,
        fixed_parts: list[float],
    ) -> None:
        nonlocal best_parts, best_value
        if point_index == len(breakpoints) - 1:
            point = breakpoints[point_index]
            for assigned in range(remaining_slots + 1):
                prefix = fixed_parts + [point] * assigned
                unassigned = count - 1 - len(prefix)
                if unassigned < 0:
                    continue
                prefix += [minimum_fragment] * unassigned
                residual = total - sum(prefix)
                if residual < minimum_fragment - 1e-12:
                    continue
                candidate = prefix + [max(minimum_fragment, residual)]
                value = independent_transfer(
                    candidate,
                    execution_margin_hat,
                    lam,
                    gamma,
                    delta,
                )
                if value < best_value - 1e-12:
                    best_parts = candidate
                    best_value = value
            return

        point = breakpoints[point_index]
        for assigned in range(remaining_slots + 1):
            search(
                point_index + 1,
                remaining_slots - assigned,
                fixed_parts + [point] * assigned,
            )

    search(0, count - 1, [])
    best_parts.sort()
    return best_parts, best_value


def cumulative_transfer(
    parts: Iterable[float],
    execution_margin_hat: float,
    lam: float,
    gamma: float,
    delta: float,
) -> tuple[float, list[float]]:
    cumulative = 0.0
    watermark = 0.0
    charges: list[float] = []
    for part in parts:
        cumulative += part
        target = transfer_scalar(
            cumulative,
            execution_margin_hat,
            lam,
            gamma,
            delta,
        )[0]
        next_watermark = max(watermark, target)
        charges.append(next_watermark - watermark)
        watermark = next_watermark
    return watermark, charges


def run_experiment(
    raw: pd.DataFrame,
    lambdas: list[float],
    gamma: float,
    delta: float,
    fragment_counts: list[int],
    incremental_fragment_gas: float,
    minimum_fragment: float,
) -> pd.DataFrame:
    for lam in lambdas:
        validate_parameters(lam, gamma, delta)
    if incremental_fragment_gas < 0.0:
        raise ValueError("incremental fragment gas must be nonnegative")
    if minimum_fragment < 0.0:
        raise ValueError("minimum fragment must be nonnegative")

    work = raw.copy()
    if "event_id" not in work:
        work["event_id"] = [str(index) for index in range(len(work))]
    work["event_id"] = work["event_id"].astype(str)

    estimated_surplus = get_numeric(work, "surplus_hat")
    true_surplus = get_numeric(work, "true_surplus", np.nan).fillna(
        estimated_surplus
    )
    margin = get_numeric(work, "execution_margin_hat")
    lp_loss = get_numeric(work, "lp_loss", 0.0)
    gas_price = get_numeric(work, "gas_price_gwei")
    eth_usd = get_numeric(work, "eth_usd")

    rows: list[dict[str, object]] = []
    for index, event in work.iterrows():
        surplus_hat = float(estimated_surplus.loc[index])
        actual_surplus = float(true_surplus.loc[index])
        required_margin = float(margin.loc[index])
        loss = float(lp_loss.loc[index])
        incremental_gas_usd = (
            incremental_fragment_gas
            * float(gas_price.loc[index])
            * 1e-9
            * float(eth_usd.loc[index])
        )

        for lam in lambdas:
            unsplit_transfer = transfer_scalar(
                surplus_hat,
                required_margin,
                lam,
                gamma,
                delta,
            )[0]
            for count in fragment_counts:
                if surplus_hat < count * minimum_fragment - 1e-12:
                    continue
                partitions: list[tuple[str, list[float], float]] = []
                equal = equal_partition(surplus_hat, count)
                partitions.append(
                    (
                        "equal",
                        equal,
                        independent_transfer(
                            equal,
                            required_margin,
                            lam,
                            gamma,
                            delta,
                        ),
                    )
                )
                adversarial, adversarial_transfer = adversarial_partition(
                    surplus_hat,
                    count,
                    required_margin,
                    lam,
                    gamma,
                    delta,
                    minimum_fragment,
                )
                partitions.append(
                    ("adversarial", adversarial, adversarial_transfer)
                )

                for mode, parts, old_transfer in partitions:
                    new_transfer, marginal_charges = cumulative_transfer(
                        parts,
                        required_margin,
                        lam,
                        gamma,
                        delta,
                    )
                    fragmentation_gas_cost = max(0, count - 1) * incremental_gas_usd
                    old_remaining_margin = (
                        actual_surplus
                        - required_margin
                        - old_transfer
                        - fragmentation_gas_cost
                    )
                    new_remaining_margin = (
                        actual_surplus
                        - required_margin
                        - new_transfer
                        - fragmentation_gas_cost
                    )
                    old_participates = old_remaining_margin >= -1e-9
                    new_participates = new_remaining_margin >= -1e-9

                    rows.append(
                        {
                            **event.to_dict(),
                            "lambda": lam,
                            "gamma": gamma,
                            "delta_usd": delta,
                            "fragment_count": count,
                            "partition_mode": mode,
                            "partition_usd": json.dumps(parts),
                            "marginal_cumulative_charges_usd": json.dumps(
                                marginal_charges
                            ),
                            "surplus_hat_usd": surplus_hat,
                            "true_surplus_usd": actual_surplus,
                            "execution_margin_hat_usd": required_margin,
                            "lp_loss_usd": loss,
                            "fragmentation_gas_cost_usd": fragmentation_gas_cost,
                            "unsplit_transfer_usd": unsplit_transfer,
                            "old_independent_transfer_usd": old_transfer,
                            "new_cumulative_transfer_usd": new_transfer,
                            "old_transfer_avoided_usd": max(
                                0.0,
                                unsplit_transfer - old_transfer,
                            ),
                            "new_transfer_deviation_usd": (
                                new_transfer - unsplit_transfer
                            ),
                            "old_remaining_margin_usd": old_remaining_margin,
                            "new_remaining_margin_usd": new_remaining_margin,
                            "old_participates": old_participates,
                            "new_participates": new_participates,
                            "old_lp_recovery_usd": (
                                min(loss, old_transfer)
                                if old_participates
                                else 0.0
                            ),
                            "new_lp_recovery_usd": (
                                min(loss, new_transfer)
                                if new_participates
                                else 0.0
                            ),
                        }
                    )
    return pd.DataFrame(rows)


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    group_columns = ["lambda", "fragment_count", "partition_mode"]
    for keys, group in events.groupby(group_columns):
        lam, count, mode = keys
        total_loss = pd.to_numeric(group["lp_loss_usd"]).sum()
        rows.append(
            {
                "lambda": lam,
                "fragment_count": count,
                "partition_mode": mode,
                "events": len(group),
                "mean_unsplit_transfer_usd": group[
                    "unsplit_transfer_usd"
                ].mean(),
                "mean_old_independent_transfer_usd": group[
                    "old_independent_transfer_usd"
                ].mean(),
                "mean_new_cumulative_transfer_usd": group[
                    "new_cumulative_transfer_usd"
                ].mean(),
                "mean_old_transfer_avoided_usd": group[
                    "old_transfer_avoided_usd"
                ].mean(),
                "max_abs_new_transfer_deviation_usd": group[
                    "new_transfer_deviation_usd"
                ].abs().max(),
                "old_participation_rate": group["old_participates"].mean(),
                "new_participation_rate": group["new_participates"].mean(),
                "old_lp_recovery_rate": (
                    group["old_lp_recovery_usd"].sum() / total_loss
                    if total_loss > 0.0
                    else np.nan
                ),
                "new_lp_recovery_rate": (
                    group["new_lp_recovery_usd"].sum() / total_loss
                    if total_loss > 0.0
                    else np.nan
                ),
                "mean_fragmentation_gas_cost_usd": group[
                    "fragmentation_gas_cost_usd"
                ].mean(),
            }
        )
    return pd.DataFrame(rows)


def optimal_counts(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    group_columns = ["event_id", "lambda", "partition_mode"]
    for keys, group in events.groupby(group_columns):
        event_id, lam, mode = keys
        old_index = group["old_remaining_margin_usd"].idxmax()
        new_index = group["new_remaining_margin_usd"].idxmax()
        rows.append(
            {
                "event_id": event_id,
                "lambda": lam,
                "partition_mode": mode,
                "old_optimal_fragment_count": int(
                    events.loc[old_index, "fragment_count"]
                ),
                "old_optimal_remaining_margin_usd": events.loc[
                    old_index,
                    "old_remaining_margin_usd",
                ],
                "new_optimal_fragment_count": int(
                    events.loc[new_index, "fragment_count"]
                ),
                "new_optimal_remaining_margin_usd": events.loc[
                    new_index,
                    "new_remaining_margin_usd",
                ],
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True)
    parser.add_argument("--output-dir", default="results/fragmentation")
    parser.add_argument(
        "--lambda-values",
        type=parse_floats,
        default=[0.95],
    )
    parser.add_argument("--gamma", type=float, default=0.05)
    parser.add_argument("--delta-usd", type=float, default=0.0)
    parser.add_argument(
        "--fragment-counts",
        type=parse_ints,
        default=[1, 2, 4, 8, 16],
    )
    parser.add_argument(
        "--incremental-fragment-gas-units",
        type=float,
        default=0.0,
        help="Measured additional gas for every fragment after the first.",
    )
    parser.add_argument(
        "--minimum-fragment-usd",
        type=float,
        default=0.0,
        help="Lower bound imposed on every adversarial fragment.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = pd.read_csv(args.events)
    events = run_experiment(
        raw,
        args.lambda_values,
        args.gamma,
        args.delta_usd,
        args.fragment_counts,
        args.incremental_fragment_gas_units,
        args.minimum_fragment_usd,
    )
    summary = summarize(events)
    optima = optimal_counts(events)

    events.to_csv(output_dir / "fragmentation_events.csv", index=False)
    summary.to_csv(output_dir / "fragmentation_summary.csv", index=False)
    optima.to_csv(output_dir / "fragmentation_optima.csv", index=False)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
