from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent

INPUT_JSON = ROOT / "poolday_graph_response.json"

OUT_DIR = ROOT / "exp_data" / "pool_data_2026_01_to_04"
OUT_DIR.mkdir(parents=True, exist_ok=True)

POOL_A = "0x4e68ccd3e89f51c3074ca5072bbac773960dfa36"
POOL_B = "0x11b815efb8f581194ae79006d24e0d814b7697f6"

with open(INPUT_JSON, "r", encoding="utf-8") as f:
    obj = json.load(f)

data = obj["data"]

outputs = {
    POOL_A: data["poolA"],
    POOL_B: data["poolB"],
}

for pool_id, rows in outputs.items():
    out = {
        "data": {
            "poolDayDatas": rows
        }
    }

    path = OUT_DIR / f"{pool_id}.json"

    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print(f"wrote {path} with {len(rows)} rows")