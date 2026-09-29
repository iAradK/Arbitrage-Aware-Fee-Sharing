#!/usr/bin/env python3
"""--legacy regression: rerun the old controlled experiment (paper 5.1) and assert the paper's numbers.

The old script is self-contained (no data/ inputs). Output goes to a temporary directory, nothing is overwritten.
    python experiments/legacy_check.py --legacy
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "experiments" / "synthetic_frontier" / "run_synthetic_recovery_incentive_frontier.py"

ap = argparse.ArgumentParser()
ap.add_argument("--legacy", action="store_true", required=True)
ap.parse_args()
with tempfile.TemporaryDirectory() as tmp:
    subprocess.run([sys.executable, str(SCRIPT), "--output-dir", tmp], check=True, capture_output=True, cwd=SCRIPT.parent)
    df = pd.read_csv(Path(tmp) / "synthetic_recovery_incentive_frontier.csv").set_index("gamma")
q0, rec = float(df.loc[0.0, "trade_quality"]), float(df.loc[0.005, "lp_recovery_rate"])
print(f"trade quality at gamma=0: {q0:.4f} (paper 44.1%), recapture at gamma=0.005: {rec:.4f} (paper 55.6%)")
assert round(q0, 3) == 0.441 and round(rec, 3) == 0.556, "legacy numbers changed"
print("legacy regression OK")
