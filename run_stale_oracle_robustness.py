from pathlib import Path
import json
import math
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt


# ============================================================
# Configuration
# ============================================================

ROOT = Path(__file__).resolve().parent

EXTERNAL_PRICE_CSV = (
    ROOT
    / "pyth_eth_usd_tradingview_2026_01_01_to_2026_04_01_1min"
    / "external_price_data"
    / "eth_usd_pyth_oracle_vs_binance_actual_2026_01_to_04.csv"
)

# Add one or more pool JSON files here.
# The uploaded file appears to be Uniswap-style poolDayDatas JSON.
POOL_JSONS = [
    ROOT / "exp_data" / "pool_data_2026_01_to_04" / "0x4e68ccd3e89f51c3074ca5072bbac773960dfa36.json",
    ROOT / "exp_data" / "pool_data_2026_01_to_04" / "0x11b815efb8f581194ae79006d24e0d814b7697f6.json",
]

OUT_DIR = ROOT / "results_stale_oracle_robustness_poolday"
OUT_DIR.mkdir(exist_ok=True)

EVENTS_CSV = OUT_DIR / "stale_oracle_poolday_events.csv"
SUMMARY_CSV = OUT_DIR / "stale_oracle_poolday_summary.csv"
EPSILON_CSV = OUT_DIR / "stale_oracle_poolday_epsilon.csv"

FIG_PDF = OUT_DIR / "fig_stale_oracle_poolday_robustness.pdf"
FIG_PNG = OUT_DIR / "fig_stale_oracle_poolday_robustness.png"


# ============================================================
# Mechanism parameters
# ============================================================

LAMBDA = 0.75
GAMMA = 0.05

# Same as previous experiments unless you have a dynamic gas model.
EXECUTION_COST_USD = 5.0

# Reservation profit model.
RESERVATION_USD = 0.0
RESERVATION_FRAC_OF_SURPLUS = 0.00

DELAYS_MINUTES = [0, 1, 5, 10, 30, 60]

EPSILON_QUANTILE = 0.95

MIN_TRUE_SURPLUS_USD = 1e-9


# ============================================================
# Loading
# ============================================================

def load_external_prices(path: Path) -> pd.DataFrame:
    print(f"Loading external prices: {path}", flush=True)

    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)

    required = [
        "datetime",
        "binance_eth_usd_price",
        "pyth_oracle_price",
    ]

    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing required external-price column: {col}")

    df = df.sort_values("datetime")
    df = df.drop_duplicates(subset=["datetime"], keep="last")

    for delay in DELAYS_MINUTES:
        if delay == 0:
            continue

        col = f"pyth_oracle_delay_{delay}m"
        if col not in df.columns:
            df[col] = df["pyth_oracle_price"].shift(delay)

    print(f"External rows: {len(df)}", flush=True)
    print(f"External range: {df['datetime'].min()} to {df['datetime'].max()}", flush=True)

    return df


def load_one_poolday_json(path: Path) -> pd.DataFrame:
    print(f"Loading pool JSON: {path}", flush=True)

    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)

    rows = obj.get("data", {}).get("poolDayDatas", [])
    if not rows:
        raise ValueError(f"No data.poolDayDatas found in {path}")

    df = pd.DataFrame(rows)

    required = ["date", "token1Price", "tvlUSD", "volumeUSD"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing poolDayDatas column {col} in {path}")

    df["date"] = pd.to_numeric(df["date"], errors="coerce").astype("Int64")
    df["datetime"] = pd.to_datetime(df["date"].astype("int64"), unit="s", utc=True)

    df["pool_price"] = pd.to_numeric(df["token1Price"], errors="coerce")
    df["tvl_usd"] = pd.to_numeric(df["tvlUSD"], errors="coerce")
    df["volume_usd"] = pd.to_numeric(df["volumeUSD"], errors="coerce")

    df = df.dropna(subset=["datetime", "pool_price", "tvl_usd"])
    df = df[(df["pool_price"] > 0) & (df["tvl_usd"] > 0)]

    df["pool_file"] = path.name

    # CPMM-equivalent virtual reserves.
    df["reserve_eth"] = df["tvl_usd"] / (2.0 * df["pool_price"])
    df["reserve_usd"] = df["tvl_usd"] / 2.0

    df = df[
        [
            "datetime",
            "pool_price",
            "tvl_usd",
            "volume_usd",
            "reserve_eth",
            "reserve_usd",
            "pool_file",
        ]
    ].copy()

    df = df.sort_values("datetime")
    df = df.drop_duplicates(subset=["datetime"], keep="last")

    print(f"Pool rows: {len(df)}", flush=True)
    print(f"Pool range: {df['datetime'].min()} to {df['datetime'].max()}", flush=True)
    print(f"Median TVL: {df['tvl_usd'].median():,.2f}", flush=True)

    return df


def load_poolday_data(paths: list[Path], external_start, external_end) -> pd.DataFrame:
    pools = []

    for path in paths:
        if not path.exists():
            print(f"WARNING: pool file does not exist, skipping: {path}", flush=True)
            continue

        df = load_one_poolday_json(path)

        overlap = df[
            (df["datetime"] >= external_start - pd.Timedelta(days=1))
            & (df["datetime"] <= external_end)
        ]

        if len(overlap) == 0:
            print(f"WARNING: no overlap with external window for {path}", flush=True)
            continue

        pools.append(df)

    if not pools:
        raise RuntimeError("No usable pool JSON files found.")

    if len(pools) == 1:
        print("Using the single available pool file.", flush=True)
        return pools[0]

    # If multiple pools are provided, use the one with the largest median TVL.
    scored = []
    for df in pools:
        scored.append((df["tvl_usd"].median(), df))

    scored.sort(key=lambda x: x[0], reverse=True)
    chosen = scored[0][1]

    print("Multiple pools found. Using the one with largest median TVL:", flush=True)
    print(chosen["pool_file"].iloc[0], flush=True)
    print(f"Chosen median TVL: {chosen['tvl_usd'].median():,.2f}", flush=True)

    return chosen


def align_daily_pool_to_minutes(pool_daily: pd.DataFrame, external: pd.DataFrame) -> pd.DataFrame:
    pool = pool_daily.copy()
    ext = external.copy()

    pool["datetime"] = pd.to_datetime(pool["datetime"], utc=True)
    ext["datetime"] = pd.to_datetime(ext["datetime"], utc=True)

    # Force identical pandas datetime precision for merge_asof.
    pool["datetime"] = pool["datetime"].astype("datetime64[ns, UTC]")
    ext["datetime"] = ext["datetime"].astype("datetime64[ns, UTC]")

    pool = pool.sort_values("datetime").reset_index(drop=True)
    ext = ext.sort_values("datetime").reset_index(drop=True)

    merged = pd.merge_asof(
        ext,
        pool,
        on="datetime",
        direction="backward",
    )

    before = len(merged)
    merged = merged.dropna(
        subset=[
            "pool_price",
            "tvl_usd",
            "reserve_eth",
            "reserve_usd",
        ]
    ).copy()

    print(
        f"Aligned rows: {len(merged)} / {before} "
        f"after forward-filling daily pool snapshots.",
        flush=True,
    )

    return merged

# ============================================================
# CPMM arbitrage logic
# ============================================================

def compute_price_correcting_trade(reserve_eth, reserve_usd, external_price):
    x = float(reserve_eth)
    y = float(reserve_usd)
    p = float(external_price)

    if x <= 0 or y <= 0 or p <= 0:
        return {
            "direction": "invalid",
            "eth_in": 0.0,
            "usd_in": 0.0,
            "eth_out": 0.0,
            "usd_out": 0.0,
            "new_reserve_eth": x,
            "new_reserve_usd": y,
            "new_pool_price": np.nan,
            "true_surplus": 0.0,
        }

    k = x * y
    p_pool = y / x

    if abs(math.log(p_pool / p)) < 1e-15:
        return {
            "direction": "none",
            "eth_in": 0.0,
            "usd_in": 0.0,
            "eth_out": 0.0,
            "usd_out": 0.0,
            "new_reserve_eth": x,
            "new_reserve_usd": y,
            "new_pool_price": p_pool,
            "true_surplus": 0.0,
        }

    x_new = math.sqrt(k / p)
    y_new = math.sqrt(k * p)

    if p_pool < p:
        # ETH is cheap in the pool. Buy ETH with USD.
        usd_in = y_new - y
        eth_out = x - x_new
        true_surplus = p * eth_out - usd_in

        return {
            "direction": "buy_eth",
            "eth_in": 0.0,
            "usd_in": max(usd_in, 0.0),
            "eth_out": max(eth_out, 0.0),
            "usd_out": 0.0,
            "new_reserve_eth": x_new,
            "new_reserve_usd": y_new,
            "new_pool_price": y_new / x_new,
            "true_surplus": max(true_surplus, 0.0),
        }

    else:
        # ETH is expensive in the pool. Sell ETH for USD.
        eth_in = x_new - x
        usd_out = y - y_new
        true_surplus = usd_out - p * eth_in

        return {
            "direction": "sell_eth",
            "eth_in": max(eth_in, 0.0),
            "usd_in": 0.0,
            "eth_out": 0.0,
            "usd_out": max(usd_out, 0.0),
            "new_reserve_eth": x_new,
            "new_reserve_usd": y_new,
            "new_pool_price": y_new / x_new,
            "true_surplus": max(true_surplus, 0.0),
        }


def estimated_surplus_for_trade(trade: dict, oracle_price: float) -> float:
    p_hat = float(oracle_price)

    if p_hat <= 0:
        return 0.0

    if trade["direction"] == "buy_eth":
        s_hat = p_hat * trade["eth_out"] - trade["usd_in"]
    elif trade["direction"] == "sell_eth":
        s_hat = trade["usd_out"] - p_hat * trade["eth_in"]
    else:
        s_hat = 0.0

    return max(s_hat, 0.0)


def reservation_profit(true_surplus: float) -> float:
    return max(
        RESERVATION_USD,
        RESERVATION_FRAC_OF_SURPLUS * true_surplus,
    )


def transfer_rule(estimated_surplus, execution_cost, reservation, delta):
    s_hat = max(float(estimated_surplus), 0.0)

    proportional_cap = LAMBDA * s_hat

    participation_cap = (
        (1.0 - GAMMA) * (s_hat - execution_cost - reservation)
        - delta
    )

    participation_cap = max(participation_cap, 0.0)

    return min(proportional_cap, participation_cap)


# ============================================================
# Event construction
# ============================================================

def build_event_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []

    for idx, row in df.iterrows():
        reserve_eth = float(row["reserve_eth"])
        reserve_usd = float(row["reserve_usd"])
        actual_price = float(row["binance_eth_usd_price"])

        trade = compute_price_correcting_trade(
            reserve_eth=reserve_eth,
            reserve_usd=reserve_usd,
            external_price=actual_price,
        )

        s_true = trade["true_surplus"]
        c = EXECUTION_COST_USD
        r_res = reservation_profit(s_true)

        baseline_feasible = (
            s_true >= MIN_TRUE_SURPLUS_USD
            and s_true - c >= r_res
        )

        event = {
            "datetime": row["datetime"],
            "pool_file": row.get("pool_file", ""),
            "pool_price_before": row["pool_price"],
            "binance_actual_price": actual_price,
            "pyth_oracle_price": row["pyth_oracle_price"],
            "tvl_usd": row["tvl_usd"],
            "reserve_eth": reserve_eth,
            "reserve_usd": reserve_usd,
            "direction": trade["direction"],
            "eth_in": trade["eth_in"],
            "usd_in": trade["usd_in"],
            "eth_out": trade["eth_out"],
            "usd_out": trade["usd_out"],
            "pool_price_after_if_executed": trade["new_pool_price"],
            "true_surplus": s_true,
            "execution_cost": c,
            "reservation_profit": r_res,
            "baseline_feasible": baseline_feasible,
        }

        for delay in DELAYS_MINUTES:
            if delay == 0:
                oracle_col = "pyth_oracle_price"
            else:
                oracle_col = f"pyth_oracle_delay_{delay}m"

            oracle_price = row.get(oracle_col, np.nan)

            if pd.isna(oracle_price):
                s_hat = np.nan
            else:
                s_hat = estimated_surplus_for_trade(trade, float(oracle_price))

            event[f"oracle_price_delay_{delay}m"] = oracle_price
            event[f"estimated_surplus_delay_{delay}m"] = s_hat
            event[f"surplus_error_abs_delay_{delay}m"] = (
                abs(s_hat - s_true) if not pd.isna(s_hat) else np.nan
            )

        rows.append(event)

        if len(rows) % 10000 == 0:
            print(f"Built {len(rows)} events...", flush=True)

    return pd.DataFrame(rows)


# ============================================================
# Evaluation
# ============================================================

def calibrate_epsilons(events: pd.DataFrame) -> dict:
    eps = {}
    feasible = events[events["baseline_feasible"]].copy()

    for delay in DELAYS_MINUTES:
        col = f"surplus_error_abs_delay_{delay}m"
        s = feasible[col].dropna()

        if len(s) == 0:
            eps[delay] = 0.0
        else:
            eps[delay] = float(s.quantile(EPSILON_QUANTILE))

    return eps


def evaluate_variant(events: pd.DataFrame, delay: int, delta_label: str, delta_value: float) -> dict:
    s_hat_col = f"estimated_surplus_delay_{delay}m"

    feasible = events[
        events["baseline_feasible"]
        & events[s_hat_col].notna()
    ].copy()

    if len(feasible) == 0:
        return {
            "delay_minutes": delay,
            "delta_label": delta_label,
            "delta_value": delta_value,
            "num_baseline_feasible": 0,
            "num_violations": 0,
            "violation_rate": np.nan,
            "lp_recovery_rate": np.nan,
            "mean_tracking_error": np.nan,
            "median_tracking_error": np.nan,
            "total_true_surplus": 0.0,
            "total_transfer": 0.0,
        }

    transfers = []
    participates = []
    tracking_errors = []

    for _, row in feasible.iterrows():
        s_true = float(row["true_surplus"])
        s_hat = float(row[s_hat_col])
        c = float(row["execution_cost"])
        r_res = float(row["reservation_profit"])

        transfer = transfer_rule(
            estimated_surplus=s_hat,
            execution_cost=c,
            reservation=r_res,
            delta=delta_value,
        )

        post_profit = s_true - c - transfer
        participates_after = post_profit >= r_res

        if participates_after:
            p_after = float(row["pool_price_after_if_executed"])
        else:
            p_after = float(row["pool_price_before"])

        p_actual = float(row["binance_actual_price"])
        tracking_error = abs(math.log(p_after / p_actual))

        transfers.append(transfer)
        participates.append(participates_after)
        tracking_errors.append(tracking_error)

    feasible["transfer"] = transfers
    feasible["participates_after"] = participates
    feasible["tracking_error"] = tracking_errors

    num_violations = int((~feasible["participates_after"]).sum())
    total_true_surplus = float(feasible["true_surplus"].sum())
    total_transfer = float(feasible["transfer"].sum())

    return {
        "delay_minutes": delay,
        "delta_label": delta_label,
        "delta_value": delta_value,
        "num_baseline_feasible": int(len(feasible)),
        "num_violations": num_violations,
        "violation_rate": num_violations / len(feasible),
        "lp_recovery_rate": (
            total_transfer / total_true_surplus
            if total_true_surplus > 0
            else np.nan
        ),
        "mean_tracking_error": float(np.mean(tracking_errors)),
        "median_tracking_error": float(np.median(tracking_errors)),
        "total_true_surplus": total_true_surplus,
        "total_transfer": total_transfer,
    }


def run_analysis(events: pd.DataFrame):
    eps = calibrate_epsilons(events)

    epsilon_df = pd.DataFrame([
        {
            "delay_minutes": delay,
            "epsilon_quantile": EPSILON_QUANTILE,
            "epsilon_surplus_usd": eps[delay],
        }
        for delay in DELAYS_MINUTES
    ])

    summary_rows = []

    for delay in DELAYS_MINUTES:
        epsilon = eps[delay]

        variants = [
            ("delta_0", 0.0),
            ("delta_epsilon", epsilon),
            ("delta_2epsilon", 2.0 * epsilon),
        ]

        for label, delta_value in variants:
            summary_rows.append(
                evaluate_variant(
                    events=events,
                    delay=delay,
                    delta_label=label,
                    delta_value=delta_value,
                )
            )

    summary = pd.DataFrame(summary_rows)

    return summary, epsilon_df


# ============================================================
# Plotting
# ============================================================

def plot_results(summary: pd.DataFrame) -> None:
    labels = ["delta_0", "delta_epsilon", "delta_2epsilon"]

    fig, axes = plt.subplots(1, 2, figsize=(9.0, 3.2))

    ax = axes[0]
    for label in labels:
        s = summary[summary["delta_label"] == label].sort_values("delay_minutes")
        ax.plot(
            s["delay_minutes"],
            100.0 * s["violation_rate"],
            marker="o",
            label=label.replace("_", " "),
        )

    ax.set_xlabel("Oracle delay (minutes)")
    ax.set_ylabel("Participation violations (%)")
    ax.set_title("(a) Participation safety")
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    for label in labels:
        s = summary[summary["delta_label"] == label].sort_values("delay_minutes")
        ax.plot(
            s["delay_minutes"],
            100.0 * s["lp_recovery_rate"],
            marker="o",
            label=label.replace("_", " "),
        )

    ax.set_xlabel("Oracle delay (minutes)")
    ax.set_ylabel("LP recovery rate (%)")
    ax.set_title("(b) Recovery trade-off")
    ax.grid(True, alpha=0.3)

    axes[0].legend(fontsize=8)
    axes[1].legend(fontsize=8)

    fig.tight_layout()
    fig.savefig(FIG_PDF, bbox_inches="tight")
    fig.savefig(FIG_PNG, dpi=300, bbox_inches="tight")

    print(f"Saved figure: {FIG_PDF}", flush=True)
    print(f"Saved figure: {FIG_PNG}", flush=True)


# ============================================================
# Main
# ============================================================

def main():
    external = load_external_prices(EXTERNAL_PRICE_CSV)

    external_start = external["datetime"].min()
    external_end = external["datetime"].max()

    pool_daily = load_poolday_data(
        POOL_JSONS,
        external_start=external_start,
        external_end=external_end,
    )

    df = align_daily_pool_to_minutes(pool_daily, external)

    if len(df) == 0:
        raise RuntimeError("No overlapping timestamps after pool/external alignment.")

    print("Building event table...", flush=True)
    events = build_event_table(df)
    events.to_csv(EVENTS_CSV, index=False)
    print(f"Saved events: {EVENTS_CSV}", flush=True)

    print("Running stale-oracle robustness analysis...", flush=True)
    summary, epsilon_df = run_analysis(events)

    summary.to_csv(SUMMARY_CSV, index=False)
    epsilon_df.to_csv(EPSILON_CSV, index=False)

    print(f"Saved summary: {SUMMARY_CSV}", flush=True)
    print(f"Saved epsilons: {EPSILON_CSV}", flush=True)

    print("")
    print("Epsilon calibration:")
    print(epsilon_df.to_string(index=False))

    print("")
    print("Summary:")
    print(summary.to_string(index=False))

    plot_results(summary)

    print("")
    print("Done.")


if __name__ == "__main__":
    main()