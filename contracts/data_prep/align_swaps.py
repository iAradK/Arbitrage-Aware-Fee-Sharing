import json
from pathlib import Path

input_file = Path("../data/jan_univ4_swaps.json")
output_file = Path("../data/all_swaps.json")

text = input_file.read_text(encoding="utf-8")

decoder = json.JSONDecoder()
position = 0
all_swaps = []

while position < len(text):
    # Skip whitespace between JSON objects
    while position < len(text) and text[position].isspace():
        position += 1

    if position >= len(text):
        break

    try:
        response, end_position = decoder.raw_decode(text, position)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Invalid JSON near character {position}: {error}"
        ) from error

    swaps = response.get("data", {}).get("swaps", [])

    if not isinstance(swaps, list):
        raise ValueError(
            f"'data.swaps' is not a list near character {position}"
        )

    all_swaps.extend(swaps)
    position = end_position

# Remove duplicates by swap ID
unique_swaps = {}

for swap in all_swaps:
    swap_id = swap.get("id")

    if swap_id is None:
        raise ValueError("Found a swap without an 'id'.")

    unique_swaps[swap_id] = swap

# Sort swaps chronologically
merged_swaps = sorted(
    unique_swaps.values(),
    key=lambda swap: (
        int(swap.get("timestamp", 0)),
        int(swap.get("logIndex", 0)),
    ),
)

result = {
    "data": {
        "swaps": merged_swaps
    }
}

output_file.write_text(
    json.dumps(result, indent=2, ensure_ascii=False),
    encoding="utf-8",
)

print(f"Read {len(all_swaps)} swaps.")
print(f"Saved {len(merged_swaps)} unique swaps to {output_file}.")