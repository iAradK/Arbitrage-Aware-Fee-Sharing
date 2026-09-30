"""Old controlled experiment: 44.1% trade quality at gamma=0 and 55.6% recapture at gamma=0.005."""
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from common import cpmm, mechanism as mech

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "experiments" / "synthetic_frontier" / "run_synthetic_recovery_incentive_frontier.py"


def test_legacy_script_reproduces_paper_numbers(tmp_path):
    subprocess.run([sys.executable, str(SCRIPT), "--output-dir", str(tmp_path)], check=True, capture_output=True, cwd=SCRIPT.parent)
    df = pd.read_csv(tmp_path / "synthetic_recovery_incentive_frontier.csv").set_index("gamma")
    assert round(df.loc[0.0, "trade_quality"], 3) == 0.441
    assert round(df.loc[0.005, "lp_recovery_rate"], 3) == 0.556


def test_new_library_reproduces_legacy_setup():
    """Same setup through common.cpmm / common.mechanism (fee 0, C=50, R=1000, lam=0.75, shock 1.10).

    The legacy CSV uses a 5001-point grid, so its smallest correction is 0.4410 of S(q0); the exact smallest
    correction has S = K = 1050, trade quality 0.4407. That grid effect is the only difference."""
    x, y, f, lam, C, R = 1000.0, 1e6, 0.0, 0.75, 50.0, 1000.0
    pi = 1000.0 * 1.10
    d, n0 = cpmm.full_correction_net(x, y, f, pi)
    S0, _ = cpmm.surplus_of(x, y, f, d, n0, pi)
    K = C + R
    r0 = mech.transfer_ideal(S0, K, lam, 0.0)
    assert np.isclose(S0 - C - r0, R)
    q = cpmm.smallest_net_for_surplus(x, y, f, d, n0, pi, K)
    Sbr, _ = cpmm.surplus_of(x, y, f, d, q, pi)
    assert abs(float(Sbr) / float(S0) - 0.441) < 1e-3
    assert abs(q / n0 - 0.2478) < 1e-3
    r = mech.transfer_ideal(S0, K, lam, 0.005)
    assert abs(float(r) / float(S0) - 0.5565) < 5e-4
