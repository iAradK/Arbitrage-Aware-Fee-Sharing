#!/usr/bin/env python3
"""Cold gas table for the block-scoped and the transaction-scoped hook (no simulation).

Runs contracts/test/hooks/BlockScopedHookGas.t.sol with `forge test --isolate` (Foundry in WSL, as in E7): every
measured swap is its own transaction, so accounts and storage start cold, and storage written by earlier transactions is
nonzero. Gas is measured inside the transaction around the router call; overhead = gas with the hook - gas of the
identical swap sequence on a pool without a hook.

The cost of an additional arbitrage transaction is 21,000 + the swap without a hook + the hook overhead of the
scenario that starts that transaction, priced at the study window's median USD per gas: the P50 of
C_gas_actual_usd in results/e7/tables/e7_smin_gas_conditions.csv (g_hat gas at each block's actual gas price and ETH
price, over all blocks) divided by g_hat from results/e7/smin_formula.txt. The paper prices its 185k-gas extra
transaction the same way.

Writes results/gas/gas_cold.csv. Does not touch results/e7/gas_profile.json or any config.

  python experiments/gas_cold_table.py
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "gas"
INTRINSIC_GAS = 21_000

SCENARIOS = {  # name: (description, starts a transaction)
    "first_ever": ("first swap the pool ever sees (hook slots and pool fee growth zero -> nonzero), no charge", True),
    "new_block": ("first swap of a block, earlier block had a scope, oracle price moved, no charge", True),
    "second_tx": ("first swap of a 2nd transaction in the same block, no charge", True),
    "first_in_multi_tx": ("1st swap of a two-swap transaction in a new block, no charge", True),
    "second_in_tx": ("2nd swap of that transaction, no charge", False),
    "charged": ("new_block with a positive settled charge, vault already holds the token", True),
    "charged_vault_empty": ("new_block with a positive settled charge, vault holds none of the token (balance 0 -> nonzero)", True),
    "charged_second_tx": ("charged first swap of a 2nd transaction in the same block, vault holds the token", True),
    "charged_first_ever": ("first swap the pool ever sees, charged, vault empty (conditions of HookGasSettled)", True),
    "invalid": ("new_block with a stale oracle: no scope, charge 0", True),
    "invalid_first_in_multi_tx": ("1st swap of a two-swap transaction, stale oracle", True),
    "invalid_second_in_tx": ("2nd swap of that transaction, stale oracle", False),
    "charged_after_other_tx": ("charged transaction after another sender's uncharged transaction in the same block "
                               "(V1: folds the earlier transaction), vault holds the token", True),
}


def _wsl_path(p: Path) -> str:
    s = str(p.resolve()).replace("\\", "/")
    return "/mnt/" + s[0].lower() + s[2:]


def run_forge() -> str:
    cmd = (f"cd '{_wsl_path(ROOT / 'contracts')}' && forge test --offline --isolate "
           "--match-contract '^BlockScopedHookGasTest$' -vv 2>&1")
    r = subprocess.run(["wsl", "-e", "bash", "-ic", cmd], capture_output=True, text=True, timeout=1800)
    log = r.stdout + r.stderr
    if "Suite result: ok" not in log or "FAIL" in log:
        raise SystemExit("forge run failed:\n" + log[-3000:])
    return log


def usd_per_gas_median() -> tuple[float, int, float]:
    g_hat = int(re.search(r"=\s*(\d+) gas", (ROOT / "results" / "e7" / "smin_formula.txt").read_text()).group(1))
    t = pd.read_csv(ROOT / "results" / "e7" / "tables" / "e7_smin_gas_conditions.csv")
    c50 = float(t[(t.pool == "eth_usdc_005") & (t.gas_quantile == "P50")].C_gas_actual_usd.iloc[0])
    return c50 / g_hat, g_hat, c50


def commit() -> str:
    h = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "contracts/src", "contracts/test"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    return h + ("+dirty" if dirty else "")


def main():
    log = run_forge()
    rows = [ln.strip().split(",")[1:] for ln in log.splitlines() if ln.strip().startswith("BSGAS,")]
    g = pd.DataFrame(rows, columns=["hook", "scenario", "gas"]).astype({"gas": int})
    g = g.pivot_table(index="scenario", columns="hook", values="gas")
    upg, g_hat, c50 = usd_per_gas_median()
    src = commit()
    out = []
    for hook in ("block_scoped", "tx_scoped", "no_hook"):
        for sc, (desc, starts_tx) in SCENARIOS.items():
            with_hook, base = int(g.loc[sc, hook]), int(g.loc[sc, "no_hook"])
            extra = INTRINSIC_GAS + with_hook if starts_tx else None
            out.append({"hook": hook, "scenario": sc, "description": desc, "gas_with_hook": with_hook, "gas_no_hook": base,
                        "overhead_gas": with_hook - base, "extra_tx_gas": extra,
                        "extra_tx_usd_median": round(extra * upg, 6) if extra else None,
                        "usd_per_gas_median": upg, "contracts_commit": src})
    df = pd.DataFrame(out)
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / "gas_cold.csv", index=False)
    print(f"median USD per gas {upg:.4e} (P50 C_gas_actual_usd {c50:.6f} / g_hat {g_hat}); contracts {src}")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(df[df.hook != "no_hook"][["hook", "scenario", "gas_with_hook", "gas_no_hook", "overhead_gas", "extra_tx_gas",
                                        "extra_tx_usd_median"]].to_string(index=False))


if __name__ == "__main__":
    main()
