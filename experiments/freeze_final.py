#!/usr/bin/env python3
"""Freeze the final rule's configs: write config/frozen/final/<exp>.sha256, which reporting.guard_final checks before any
test-month run (--split test --confirm-frozen). E8's hash covers its config, the E2 config it reads and the dynamic-fee
betas calibrated on the validation months (read from results/<cal_root>/e8/dynfee_calibration.json), as hashed_cfg does.

  python experiments/freeze_final.py --cal-root final_valid_eps
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common import reporting  # noqa: E402
from common.pools import RESULTS  # noqa: E402

CONFIGS = {"e2": "e2_final.yml", "e4": "e4_final.yml", "e6": "e6_final.yml", "e7": "e7_final.yml"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cal-root", required=True, help="run root holding the validation dynfee calibration (e8/dynfee_calibration.json)")
    a = ap.parse_args()
    out = reporting.FINAL_FROZEN_DIR
    out.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for exp, f in CONFIGS.items():
        hashes[exp] = reporting.config_hash(reporting.load_config(ROOT / "experiments" / "configs" / f))
    cfg8 = reporting.load_config(ROOT / "experiments" / "configs" / "e8_final.yml")
    cfg2 = reporting.load_config(ROOT / cfg8["e2_config"])
    cal = json.loads((RESULTS / a.cal_root / "e8" / "dynfee_calibration.json").read_text())
    hashes["e8"] = reporting.config_hash({"e8": cfg8, "e2_hash": reporting.config_hash(cfg2),
                                          "beta_cal": {k: v["beta"] for k, v in cal["pools"].items()}})
    for exp, h in hashes.items():
        (out / f"{exp}.sha256").write_text(h)
    print(json.dumps({"frozen_dir": out.relative_to(ROOT).as_posix(), "hashes": hashes,
                      "beta_cal": {k: v["beta"] for k, v in cal["pools"].items()}}, indent=1))


if __name__ == "__main__":
    main()
