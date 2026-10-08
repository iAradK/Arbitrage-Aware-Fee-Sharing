"""Read a credential from the environment or from data/.secrets.env (gitignored, never committed).

File format, one per line:  GRAPH_API_KEY=...   ETH_RPC_URL=https://...
"""
from __future__ import annotations

import os
from pathlib import Path

SECRETS_FILE = Path(__file__).resolve().parent / ".secrets.env"


def get(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""
