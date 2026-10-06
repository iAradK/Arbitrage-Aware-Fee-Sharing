# Block-scope conformance (2026-10-06)

All suites ran on commit 42017a6 (forge 1.5.1, `forge_log.txt`). The log's header lists files that WSL's git reported as modified. Those are CRLF line-ending artifacts of the Windows checkout: Windows git and `git diff --ignore-cr-at-eol` show no change.

| Item | Suite | Mode | Result |
|---|---|---|---|
| 1 | `BlockScopeConformanceTest`: 48 E7 sequences (validation months), 360 fragments per mode | `--isolate` | a (one tx) 0, b (tx per fragment) 0, c (2-4 blocks) 0 mismatches; 70/70/57 charged fragments (`conformance_counts.json`) |
| 2 | `BlockScopeFuzzTest`: 2,000 runs, seed 20261006 | `--isolate --ffi` and `--ffi` | 0 mismatches in both (`fuzz_summary.csv`, per-run `fuzz_*.csv`) |
| 3 | `BlockScopeBoundaryTest` (8) and `BlockScopeBoundaryBaseFeeTest` (1) | the 8 in both modes; base-fee test without `--isolate` | 17 / 17 pass |

`vectors.json` is written by `experiments/e7_block_scope_vectors.py`. The fuzz test calls `experiments/block_scope_ffi.py`. Both use `common.fixedpoint.ScopedHookReference`.
