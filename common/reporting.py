"""Config hashing, manifests, test-split guard, CSV + LaTeX tables, figure saving."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pandas as pd
import yaml

from .data_io import sha256
from .pools import REPO, RESULTS

FROZEN_DIR = REPO / "config" / "frozen"          # tracked: config hash frozen after validation, per experiment
DATA_MANIFEST = REPO / "config" / "data_manifest.json"   # tracked: inventory of the raw inputs (make_data_manifest.py)


def frozen_path(exp: str) -> Path:
    return FROZEN_DIR / f"{exp}.sha256"


def load_config(path: str | Path) -> dict:
    return yaml.safe_load(open(path, encoding="utf-8"))


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "common", "experiments", "tests"], cwd=REPO,
                               capture_output=True, text=True).stdout.strip()
        return out + ("+dirty" if dirty else "")
    except Exception:
        return "unknown"


# RESULTS_RUN (environment): a rerun's own output root, results/<RESULTS_RUN>/<exp>, so a rerun never overwrites the
# stored results/<exp>. Unset: results/<exp>, as before.
RESULTS_RUN = os.environ.get("RESULTS_RUN", "")


def run_root() -> Path:
    return RESULTS / RESULTS_RUN if RESULTS_RUN else RESULTS


def out_dir(exp: str) -> Path:
    d = run_root() / exp
    (d / "tables").mkdir(parents=True, exist_ok=True)
    (d / "figures").mkdir(parents=True, exist_ok=True)
    return d


def guard_split(exp: str, split: str, confirm_frozen: bool, cfg: dict) -> None:
    """Test-split runs need --confirm-frozen and a config hash equal to the one frozen after validation."""
    if split != "test":
        return
    if not confirm_frozen:
        raise SystemExit("--split test requires --confirm-frozen")
    f = frozen_path(exp)
    if not f.exists():
        raise SystemExit(f"{f} missing: run the validation split and then --freeze before touching the test months")
    if f.read_text().strip() != config_hash(cfg):
        raise SystemExit("config changed since it was frozen; test split refused")


def freeze(exp: str, cfg: dict) -> None:
    FROZEN_DIR.mkdir(parents=True, exist_ok=True)
    frozen_path(exp).write_text(config_hash(cfg))


def write_manifest(exp: str, cfg: dict, inputs: list[Path], split: str, extra: dict | None = None,
                   tag: str | None = None, latest: bool = True) -> None:
    """manifest.json records the latest run of the experiment. With `tag`, the run also gets its own manifest_<tag>.json,
    which later runs of other tags do not overwrite. `latest=False` writes only the tagged file (analysis scripts)."""
    m = {"experiment": exp, "split": split, "git_commit": git_commit(), "config_hash": config_hash(cfg),
         "inputs": {str(Path(p).relative_to(REPO)): sha256(p) for p in inputs}}
    dm = DATA_MANIFEST
    if dm.exists():
        m["raw_data_sha256"] = {k: v["sha256"] for k, v in json.load(open(dm))["files"].items()}
    m.update(extra or {})
    if tag is not None:
        m["tag"] = tag
        (out_dir(exp) / f"manifest_{tag}.json").write_text(json.dumps(m, indent=1))
    if latest:
        (out_dir(exp) / "manifest.json").write_text(json.dumps(m, indent=1))


def _tex_escape(s: str) -> str:
    return str(s).replace("_", r"\_").replace("%", r"\%").replace("&", r"\&")


def write_table(df: pd.DataFrame, path_stem: Path, fmt: dict | None = None, caption: str = "") -> None:
    """CSV plus a booktabs LaTeX table (no jinja needed)."""
    path_stem = Path(path_stem)
    df.to_csv(str(path_stem) + ".csv", index=False)      # append, never with_suffix: stems may contain dots (e.g. curv0.5)
    fmt = fmt or {}
    cols = list(df.columns)
    lines = [r"\begin{tabular}{" + "l" * 1 + "r" * (len(cols) - 1) + "}", r"\toprule",
             " & ".join(_tex_escape(c) for c in cols) + r" \\", r"\midrule"]
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            if isinstance(v, float):
                cells.append(fmt.get(c, "{:.4g}").format(v))
            else:
                cells.append(_tex_escape(v))
        lines.append(" & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    tex = "\n".join(lines)
    if caption:
        tex = "% " + caption + "\n" + tex
    Path(str(path_stem) + ".tex").write_text(tex, encoding="utf-8")


def savefig(fig, path_stem: Path) -> None:
    from .plotstyle import ensure_all_xticks
    path_stem = Path(path_stem)
    fig.canvas.draw()                       # finalise limits and default ticks before checking them
    ensure_all_xticks(fig)                  # every panel gets at least two labelled x ticks
    fig.savefig(str(path_stem) + ".pdf", bbox_inches="tight")
    fig.savefig(str(path_stem) + ".png", dpi=200, bbox_inches="tight")
