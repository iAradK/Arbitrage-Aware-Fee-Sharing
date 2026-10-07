# Test logs, final block-scoped hook (commit ff69c37)

All runs use contracts at `ff69c37737e1645df8be9f1326d66cfe883dae34` (clean tree), forge 1.5.1-stable in WSL, with `--offline`. Each log starts with its commit and command. Scripts: `scripts/run_all.sh` and `scripts/run_fix.sh` (block-scope suites, in the worktree), `scripts/older.sh` (older suites, in a scratch copy).

| Log | Mode | Suites | Result |
|---|---|---|---|
| conformance_iso | --isolate | BlockScopeConformanceTest | 1/1 pass; 360 fragments per mode, mismatches a/b/c 0/0/0, charged 90/119/87 |
| blockscoped_iso | --isolate --ffi | BlockScopedHookTest | 17/17 pass (rerun by run_fix.sh, see below) |
| scenarios_iso | --isolate --ffi | BlockScopedHookScenariosTest | 6/6 pass |
| boundary_iso | --isolate --ffi | BlockScopeBoundaryTest | 11/11 pass |
| props_iso | --isolate --ffi | BlockScopeV1PropertiesTest | 2/2 pass |
| fuzz_iso | --isolate --ffi | BlockScopeFuzzTest (2,000 runs) | pass |
| gas_iso | --isolate | BlockScopedHookGasTest | 33/33 pass |
| boundary_noiso | none, --ffi | BlockScopeBoundaryTest, BlockScopeBoundaryBaseFeeTest | 11 pass, 1 skipped (`test_OverflowClosedSurplusSaturates` needs separate transactions) |
| blockscoped_noiso | none, --ffi | ParticipationAwareHookFeeTest | 3/3 pass |
| fuzz_noiso | none, --ffi | BlockScopeFuzzTest (2,000 runs) | pass |
| txscoped_noiso | none | TxScopedParticipationAwareHookTest, HookGasIsolatedTest, HookGasSettledTest | 22/22 pass |
| txscoped_iso | --isolate | the same three | 16 pass, 6 fail; not a supported mode, see below |
| older_txscoped_scratch | none | CumulativeSurplusAccountingTest, HookGasBenchmarkTest, E7ConformanceTest, SurplusSharingAccountingTest, E7GasProfileTest | 23/23 pass |

## Notes on individual logs

**blockscoped_iso.** `run_all.sh` first grouped `ParticipationAwareHookFeeTest` with the isolated run. That suite refuses `--isolate` (`test_NotIsolated`), and isolated calls see base fee 0, so it reported 3 failures. `run_fix.sh` reran the isolated log with `BlockScopedHookTest` only. `ParticipationAwareHookFeeTest` is covered by blockscoped_noiso.

**txscoped_iso.** `TxScopedParticipationAwareHookTest` keeps the transaction-scoped hook's transient state across several external calls, so it requires a single transaction. Its six failures under `--isolate` come from that: no charge, or a stale transient watermark. The hook and the test are byte-identical to fcb66ae. The suite's supported mode is txscoped_noiso.

**older_txscoped_scratch.** Run in a scratch copy of `contracts/` extracted with `git archive ff69c37`, with `lib` linked:
- **Inputs.** The gitignored inputs `results/e7/vectors.json` and `contracts/results/test_vectors.json` are read-only copies (mode 444, `cmp`-identical to the main checkout). Symlinks were rejected because forge resolves them and refuses reads outside `fs_permissions`.
- **Outputs.** Every output path is redirected by environment variable under the scratch tree: `E7_COUNTS`, `E7_GAS_CSV`, `HOOK_LIFECYCLE_GAS_OUTPUT`, `CUMULATIVE_GAS_OUTPUT`, `GAS_OUTPUT`.
- **Check.** The main checkout's `results/e7` and `contracts/results` were hashed before and after the run (content sha256, mtime+size, file count 88). They were unchanged.
- **Found.** In the main checkout, `results/e7/gas_cumulative.csv` (75 bytes) and `contracts/results/cumulative_solidity_gas.csv` (107 bytes) hold only their first data row. Both were written 2026-10-06 19:46; neither is tracked. The scratch run writes 2,714 and 1,653 bytes. `conformance_counts.json` has the same counts and 0 mismatches; its `computeTransfer` gas is 27 lower (1,546–1,633 against 1,573–1,660).
