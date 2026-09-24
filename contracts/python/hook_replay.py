#!/usr/bin/env python3
"""Build atomic accounting vectors and merge Solidity gas measurements.

Input CSV columns may use an unadorned name, a ``*_usd`` floating-point
column, or a ``*_wad`` integer column. Required economic inputs are::

    event_id, surplus_hat, execution_margin_hat, lp_loss,
    gas_price_gwei, eth_usd

``true_surplus`` and descriptive market columns are optional.

The canonical rule is

    F(S_hat) = min(lambda*S_hat,
                   (1-gamma)*max(0, S_hat-K_hat-delta)).

``K_hat`` is named ``execution_margin_hat`` in the input schema for backward
compatibility. It should contain the estimated baseline execution cost plus
the reservation payoff. Fragment-dependent costs are evaluated separately in
``fragmentation_replay.py``.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

WAD = 10**18


def parse_floats(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def validate_parameters(lam: float, gamma: float, delta: float) -> None:
    if not 0.0 <= lam <= 1.0:
        raise ValueError("lambda must lie in [0, 1]")
    if not 0.0 <= gamma < 1.0:
        raise ValueError("gamma must lie in [0, 1)")
    if delta < 0.0 or not math.isfinite(delta):
        raise ValueError("delta must be finite and nonnegative")


def get_numeric(
    frame: pd.DataFrame,
    base: str,
    default: float | None = None,
) -> pd.Series:
    if f"{base}_wad" in frame:
        return pd.to_numeric(frame[f"{base}_wad"], errors="raise") / WAD
    if f"{base}_usd" in frame:
        return pd.to_numeric(frame[f"{base}_usd"], errors="raise")
    if base in frame:
        return pd.to_numeric(frame[base], errors="raise")
    if default is None:
        raise ValueError(
            f"Missing required column: {base}, {base}_usd, or {base}_wad"
        )
    return pd.Series(default, index=frame.index, dtype=float)


def transfer_scalar(
    surplus_hat: float,
    execution_margin_hat: float,
    lam: float,
    gamma: float,
    delta: float,
) -> tuple[float, bool]:
    """Evaluate the canonical rule in floating-point common-numeraire units."""
    validate_parameters(lam, gamma, delta)
    if surplus_hat < 0.0 or execution_margin_hat < 0.0:
        raise ValueError("surplus and execution margin must be nonnegative")
    target = lam * surplus_hat
    cap = (1.0 - gamma) * max(
        0.0,
        surplus_hat - execution_margin_hat - delta,
    )
    return min(target, cap), cap <= target


def transfer_float(
    surplus_hat: pd.Series,
    execution_margin_hat: pd.Series,
    lam: float | pd.Series,
    gamma: float | pd.Series,
    delta: float | pd.Series,
) -> pd.Series:
    """Vectorized floating-point implementation of the canonical rule."""
    buffered_margin = np.maximum(
        0.0,
        surplus_hat - execution_margin_hat - delta,
    )
    return np.minimum(lam * surplus_hat, (1.0 - gamma) * buffered_margin)


def to_wad_floor(value: float) -> int:
    if value < 0.0 or not math.isfinite(value):
        raise ValueError(f"Cannot encode value as uint256 WAD: {value}")
    return int(math.floor(value * WAD + 1e-9))


def mul_wad_down(value: int, fraction_wad: int) -> int:
    if value < 0:
        raise ValueError("value must be nonnegative")
    if not 0 <= fraction_wad <= WAD:
        raise ValueError("WAD fraction must lie in [0, WAD]")
    return (value // WAD) * fraction_wad + (
        (value % WAD) * fraction_wad
    ) // WAD


def transfer_wad(
    surplus_hat: int,
    execution_margin_hat: int,
    lambda_wad: int,
    gamma_wad: int,
    delta: int,
) -> tuple[int, bool]:
    """Exact integer counterpart of ``SurplusSharingAccounting.sol``."""
    if surplus_hat < 0 or execution_margin_hat < 0 or delta < 0:
        raise ValueError("monetary WAD inputs must be nonnegative")
    if not 0 <= lambda_wad <= WAD:
        raise ValueError("lambda_wad must lie in [0, WAD]")
    if not 0 <= gamma_wad < WAD:
        raise ValueError("gamma_wad must lie in [0, WAD)")

    target = mul_wad_down(surplus_hat, lambda_wad)
    buffered_margin = max(0, surplus_hat - execution_margin_hat - delta)
    cap = mul_wad_down(buffered_margin, WAD - gamma_wad)
    return min(target, cap), cap <= target


def cumulative_charges_wad(
    cumulative_surplus_wad: Iterable[int],
    execution_margin_hat_wad: int,
    lambda_wad: int,
    gamma_wad: int,
    delta_wad: int,
) -> list[dict[str, int | bool]]:
    """Apply the transaction-scoped watermark to cumulative surplus prefixes.

    The input is the sequence ``A_1, ..., A_n`` rather than fragment-level
    surplus. This permits reversal experiments as well as monotone partitions.
    """
    watermark = 0
    rows: list[dict[str, int | bool]] = []
    for index, cumulative_surplus in enumerate(cumulative_surplus_wad, start=1):
        if cumulative_surplus < 0:
            raise ValueError("cumulative surplus must be nonnegative")
        target, cap_binds = transfer_wad(
            cumulative_surplus,
            execution_margin_hat_wad,
            lambda_wad,
            gamma_wad,
            delta_wad,
        )
        next_watermark = max(watermark, target)
        marginal_charge = next_watermark - watermark
        rows.append(
            {
                "fragment": index,
                "cumulative_surplus_wad": cumulative_surplus,
                "target_wad": target,
                "watermark_wad": next_watermark,
                "marginal_charge_wad": marginal_charge,
                "cap_binds": cap_binds,
            }
        )
        watermark = next_watermark
    return rows


def build_vectors(
    frame: pd.DataFrame,
    lambdas: list[float],
    gamma: float,
    delta: float,
) -> tuple[pd.DataFrame, dict[str, object]]:
    for lam in lambdas:
        validate_parameters(lam, gamma, delta)

    work = frame.copy()
    if "event_id" not in work:
        work["event_id"] = [str(index) for index in range(len(work))]
    work["event_id"] = work["event_id"].astype(str)

    surplus = get_numeric(work, "surplus_hat")
    margin = get_numeric(work, "execution_margin_hat")
    loss = get_numeric(work, "lp_loss", 0.0)
    true_surplus = get_numeric(work, "true_surplus", np.nan)

    rows: list[dict[str, object]] = []
    vectors: list[dict[str, str]] = []
    for index, row in work.iterrows():
        for lam in lambdas:
            surplus_wad = to_wad_floor(float(surplus.loc[index]))
            margin_wad = to_wad_floor(float(margin.loc[index]))
            lambda_wad = to_wad_floor(lam)
            gamma_wad = to_wad_floor(gamma)
            delta_wad = to_wad_floor(delta)
            transfer, cap_binds = transfer_wad(
                surplus_wad,
                margin_wad,
                lambda_wad,
                gamma_wad,
                delta_wad,
            )
            event_id = f"{row['event_id']}|l={lam:g}"
            vectors.append(
                {
                    "event_id": event_id,
                    "surplus_hat_wad": str(surplus_wad),
                    "execution_margin_hat_wad": str(margin_wad),
                    "lambda_wad": str(lambda_wad),
                    "gamma_wad": str(gamma_wad),
                    "delta_wad": str(delta_wad),
                    "expected_transfer_wad": str(transfer),
                }
            )
            rows.append(
                {
                    **row.to_dict(),
                    "vector_event_id": event_id,
                    "lambda": lam,
                    "gamma": gamma,
                    "delta_usd": delta,
                    "surplus_hat_usd": float(surplus.loc[index]),
                    "execution_margin_hat_usd": float(margin.loc[index]),
                    "lp_loss_usd": float(loss.loc[index]),
                    "true_surplus_usd": (
                        float(true_surplus.loc[index])
                        if pd.notna(true_surplus.loc[index])
                        else np.nan
                    ),
                    "expected_transfer_wad": transfer,
                    "expected_transfer_usd": transfer / WAD,
                    "cap_binds_python": cap_binds,
                }
            )
    return pd.DataFrame(rows), {"count": len(vectors), "vectors": vectors}


def summarize(
    events: pd.DataFrame,
    gas_units_fallback: float | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    detailed = events.copy()
    if "gas_used" not in detailed:
        if gas_units_fallback is None:
            raise ValueError(
                "No gas_used measurements. Run Forge or pass "
                "--fallback-gas-units."
            )
        detailed["gas_used"] = gas_units_fallback

    gas_price = get_numeric(detailed, "gas_price_gwei")
    eth_usd = get_numeric(detailed, "eth_usd")
    detailed["hook_cost_usd"] = (
        pd.to_numeric(detailed["gas_used"]) * gas_price * 1e-9 * eth_usd
    )

    variants = (("ignoring_hook_overhead", False), ("including_hook_overhead", True))
    for variant, add_hook in variants:
        margin = detailed["execution_margin_hat_usd"] + (
            detailed["hook_cost_usd"] if add_hook else 0.0
        )
        detailed[f"transfer_{variant}_usd"] = transfer_float(
            detailed["surplus_hat_usd"],
            margin,
            detailed["lambda"],
            detailed["gamma"],
            detailed["delta_usd"],
        )
        actual_surplus = detailed["true_surplus_usd"].fillna(
            detailed["surplus_hat_usd"]
        )
        detailed[f"participates_{variant}"] = (
            actual_surplus - margin - detailed[f"transfer_{variant}_usd"]
            >= -1e-9
        )
        detailed[f"recovery_{variant}_usd"] = np.minimum(
            detailed["lp_loss_usd"],
            detailed[f"transfer_{variant}_usd"],
        ) * detailed[f"participates_{variant}"]
        detailed[f"violation_{variant}"] = (
            ~detailed[f"participates_{variant}"]
        ) & (detailed[f"transfer_{variant}_usd"] > 0.0)

    summary_rows: list[dict[str, object]] = []
    for lam, group in detailed.groupby("lambda"):
        total_loss = group["lp_loss_usd"].sum()
        for variant, _ in variants:
            summary_rows.append(
                {
                    "lambda": lam,
                    "variant": variant,
                    "events": len(group),
                    "lp_recovery_rate": (
                        group[f"recovery_{variant}_usd"].sum() / total_loss
                        if total_loss > 0.0
                        else np.nan
                    ),
                    "participation_rate": group[
                        f"participates_{variant}"
                    ].mean(),
                    "participation_violations": int(
                        group[f"violation_{variant}"].sum()
                    ),
                    "positive_transfer_rate": (
                        group[f"transfer_{variant}_usd"] > 0.0
                    ).mean(),
                    "cap_bind_rate": group["cap_binds_python"].mean(),
                    "median_hook_gas": group["gas_used"].median(),
                    "p95_hook_gas": group["gas_used"].quantile(0.95),
                    "max_hook_gas": group["gas_used"].max(),
                    "mean_hook_cost_usd": group["hook_cost_usd"].mean(),
                    "mean_hook_cost_fraction_surplus": np.mean(
                        np.divide(
                            group["hook_cost_usd"],
                            group["surplus_hat_usd"],
                            out=np.zeros(len(group)),
                            where=group["surplus_hat_usd"] > 0.0,
                        )
                    ),
                }
            )
    return detailed, pd.DataFrame(summary_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True)
    parser.add_argument("--output-dir", default="results")
    parser.add_argument(
        "--lambda-values",
        type=parse_floats,
        default=[0.25, 0.5, 0.75, 0.95],
    )
    parser.add_argument("--gamma", type=float, default=0.05)
    parser.add_argument("--delta-usd", type=float, default=0.0)
    parser.add_argument("--solidity-gas-csv")
    parser.add_argument("--fallback-gas-units", type=float)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = pd.read_csv(args.events)
    events, payload = build_vectors(
        raw,
        args.lambda_values,
        args.gamma,
        args.delta_usd,
    )
    (output_dir / "test_vectors.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    events.to_csv(output_dir / "python_vectors.csv", index=False)

    if args.solidity_gas_csv:
        gas = pd.read_csv(args.solidity_gas_csv).rename(
            columns={"event_id": "vector_event_id"}
        )
        events = events.merge(
            gas,
            on="vector_event_id",
            how="left",
            validate="one_to_one",
        )
        if events["gas_used"].isna().any():
            raise ValueError("Solidity gas CSV is missing vectors")
        events["transfer_solidity_wad"] = events[
            "transfer_solidity_wad"
        ].astype(object).map(int)
        events["transfer_deviation_wad"] = (
            events["transfer_solidity_wad"] - events["expected_transfer_wad"]
        ).abs()
        if events["transfer_deviation_wad"].max() != 0:
            raise AssertionError("Python/Solidity transfer mismatch")

    detailed, summary = summarize(events, args.fallback_gas_units)
    detailed.to_csv(output_dir / "hook_replay_events.csv", index=False)
    summary.to_csv(output_dir / "hook_replay_summary.csv", index=False)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
