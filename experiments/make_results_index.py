#!/usr/bin/env python3
"""Index of the result files the paper cites: path, SHA-256 and producing commit (NOTES/results_index.csv).

Result files are not tracked in git, so this index is the tracked record of what the paper's numbers were read from.
Citations are collected from the paper sources (main-sigmetrics.tex, paper/*.tex) and NOTES/*number_sources*.md:
every `results/...` path, glob (expanded) and bare result file name (`e7_smin_share_test.csv`, resolved under results/,
preferring the experiment's own folder over snapshot subfolders gas_contract_*/pre_*). A citation that resolves to
nothing, a directory or a template is listed without a hash.

Producing commit, first rule that applies (commit_source says which):
  manifest_self     the file is a manifest: its git_commit
  dir_name          a snapshot folder named after its commit (gas_contract_<hash>)
  column            a contracts_commit column (results/gas/gas_cold.csv)
  manifest_outputs  a manifest of the folder (or its experiment folder) lists the file with this SHA-256
  manifest_tag      the tagged manifest whose tag is the longest part of the file name (e2_bootstrap_test_lag1_k15_med.csv
                    -> manifest_test_lag1_k15_med.json)
  manifest_split    the only manifest of the folder (or experiment folder) for the file's split (_valid/_test/_train)
  manifest_latest   the folder's or experiment folder's manifest.json (latest run; not file-specific, weakest)
  document          a cited .md file (not a result)
  unknown           none of the above
"manifest lists another hash" is flagged in `note`.

  python experiments/make_results_index.py --source-root <main checkout>     # regenerate after every rerun
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "NOTES" / "results_index.csv"
PATH_RE = re.compile(r"results/[A-Za-z0-9_./*{}<>\-]+")
BARE_RE = re.compile(r"(?<![/\w.\-])([A-Za-z0-9][A-Za-z0-9_.\-]*\.(?:csv\.gz|csv|json|txt|parquet|tex|png|pdf))\b")
SNAPSHOT = re.compile(r"/(gas_contract_[0-9a-f]+|pre_[a-z_]+)/")
SPLITS = ("valid", "test", "train")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sources(src: Path) -> list[Path]:
    out = [src / "main-sigmetrics.tex"] + sorted((src / "paper").glob("*.tex")) + sorted((src / "NOTES").glob("*number_sources*.md"))
    return [p for p in out if p.exists()]


def citations(src: Path) -> dict[str, set[str]]:
    """citation -> source files citing it (paths relative to src)."""
    cites: dict[str, set[str]] = {}
    for f in sources(src):
        text = f.read_text(encoding="utf-8", errors="replace")
        rel = f.relative_to(src).as_posix()
        for m in PATH_RE.findall(text):
            cites.setdefault(m.rstrip(".,;:)}"), set()).add(rel)
        for m in BARE_RE.findall(text):
            if not m.startswith("results"):
                cites.setdefault("bare:" + m, set()).add(rel)
    return cites


def resolve(src: Path, cite: str, by_name: dict[str, list[str]]) -> tuple[list[str], str]:
    if cite.startswith("bare:"):
        name = cite[5:]
        hits = by_name.get(name, [])
        main = [h for h in hits if not SNAPSHOT.search("/" + h)]
        if len(hits) == 0:
            return [], "bare name, no file under results/"
        if len(main) == 1:
            return main, "bare name"
        return (main or hits), f"bare name, {len(main or hits)} candidates"
    if any(c in cite for c in "{}<>"):
        return [], "template, not resolved"
    if "*" in cite:
        hits = sorted(Path(h).relative_to(src).as_posix() for h in glob.glob(str(src / cite)) if Path(h).is_file())
        return hits, "glob" if hits else "glob, no match"
    p = src / cite
    if p.is_dir():
        return [], "directory"
    if p.is_file():
        return [cite], ""
    return [], "missing"


def manifests_for(src: Path, rel: str) -> list[tuple[Path, dict]]:
    p = src / rel
    parts = Path(rel).parts
    dirs = [p.parent]
    if len(parts) > 2:
        dirs.append(src / parts[0] / parts[1])            # the experiment folder results/<exp>
    out, seen = [], set()
    for d in dirs:
        for m in sorted(d.glob("manifest*.json")):
            if m in seen:
                continue
            seen.add(m)
            try:
                out.append((m, json.loads(m.read_text())))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
    return out


def producing_commit(src: Path, rel: str, digest: str) -> tuple[str, str, str]:
    p = src / rel
    name = p.name
    if name.endswith(".md"):
        return "", "document", "not a result file"
    if name.startswith("manifest") and name.endswith(".json"):
        try:
            return json.loads(p.read_text()).get("git_commit", ""), "manifest_self", ""
        except json.JSONDecodeError:
            pass
    m = re.search(r"gas_contract_([0-9a-f]{6,40})", rel)
    if m:
        return m.group(1), "dir_name", ""
    if name.endswith(".csv"):
        try:
            head = pd.read_csv(p, nrows=5000)
            if "contracts_commit" in head.columns:
                return ";".join(sorted(set(head["contracts_commit"].astype(str)))), "column", ""
        except Exception:                                   # noqa: BLE001 (not every .csv parses as a table)
            pass
    mans = manifests_for(src, rel)
    note = ""
    for mp, mj in mans:
        outs = mj.get("outputs") or {}
        if name in outs:
            if outs[name] == digest:
                return mj.get("git_commit", ""), "manifest_outputs", mp.relative_to(src).as_posix()
            note = f"{mp.relative_to(src).as_posix()} lists another hash"
    def joined(mp):
        return "; ".join(x for x in (mp.relative_to(src).as_posix(), note) if x)
    stem = name.split(".")[0]
    tagged = [(len(str(mj["tag"])), mp, mj) for mp, mj in mans
              if mj.get("tag") and mp.name != "manifest.json" and str(mj["tag"]) in stem]
    if tagged:
        _, mp, mj = max(tagged, key=lambda x: x[0])
        return mj.get("git_commit", ""), "manifest_tag", joined(mp)
    split = next((s for s in SPLITS if re.search(rf"(_|^){s}(_|\.|$)", name)), None)
    if split:
        same = [(mp, mj) for mp, mj in mans if mj.get("split") == split]
        if len(same) == 1:
            mp, mj = same[0]
            return mj.get("git_commit", ""), "manifest_split", joined(mp)
    parts = Path(rel).parts
    exp_dir = src / parts[0] / parts[1] if len(parts) > 2 else p.parent
    latest = next((d / "manifest.json" for d in (p.parent, exp_dir) if (d / "manifest.json").exists()), p.parent / "manifest.json")
    if latest.exists():
        try:
            return json.loads(latest.read_text()).get("git_commit", ""), "manifest_latest", \
                "; ".join(x for x in (latest.relative_to(src).as_posix(), note) if x)
        except json.JSONDecodeError:
            pass
    return "", "unknown", note


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-root", default=str(ROOT), help="checkout holding the paper sources and results/ (default: this one)")
    a = ap.parse_args()
    src = Path(a.source_root).resolve()
    by_name: dict[str, list[str]] = {}
    for f in (src / "results").rglob("*"):
        if f.is_file():
            by_name.setdefault(f.name, []).append(f.relative_to(src).as_posix())
    rows = []
    for cite, cited_in in sorted(citations(src).items()):
        files, how = resolve(src, cite, by_name)
        if cite.startswith("bare:") and not files:
            continue                                         # a bare name that is not a result file (e.g. a script)
        base = {"citation": cite.removeprefix("bare:"), "cited_in": "; ".join(sorted(cited_in)), "resolution": how}
        if not files:
            rows.append({**base, "path": "", "sha256": "", "bytes": "", "modified_utc": "", "producing_commit": "",
                         "commit_source": "", "note": ""})
            continue
        for rel in files:
            p = src / rel
            d = sha256(p)
            c, cs, note = producing_commit(src, rel, d)
            rows.append({**base, "path": rel, "sha256": d, "bytes": p.stat().st_size,
                         "modified_utc": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "producing_commit": c, "commit_source": cs, "note": note})
    df = pd.DataFrame(rows)
    df = df.sort_values(["path", "citation"]).drop_duplicates(subset=["path", "citation"]).reset_index(drop=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    df.to_csv(OUT, index=False)
    n_files = df["path"].replace("", pd.NA).dropna().nunique()
    print(f"{OUT.relative_to(ROOT)}: {len(df)} rows, {n_files} files, generated by {head[:7]} from {src}")
    print(df.groupby("commit_source", dropna=False).size().to_string())
    print(df[df["path"] == ""][["citation", "resolution", "cited_in"]].to_string(index=False))


if __name__ == "__main__":
    main()
