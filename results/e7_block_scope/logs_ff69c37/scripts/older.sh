#!/bin/bash
# Runs the five older transaction-scoped suites in a scratch copy of contracts/ at a commit (extracted with
# git archive). Gitignored inputs are symlinked from the main checkout; every output is redirected under the
# scratch tree; results/e7 and contracts/results of the main checkout are hashed before and after.
C="$1"   # commit the scratch copy was extracted from
S=/mnt/c/Users/aradk/AppData/Local/Temp/claude/C--Users-aradk-Desktop-master-Implermenant-loss-rbitrage-Aware-Fee-Sharing/6ce6939b-f118-4a1b-8862-9a22d7a2c5f4/scratchpad/older
M="/mnt/c/Users/aradk/Desktop/master/Implermenant loss/rbitrage-Aware-Fee-Sharing"
F=~/.foundry/bin/forge
mkdir -p "$S/results/e7" "$S/contracts/results" "$S/results/out"
[ -e "$S/contracts/lib" ] || ln -s "$M/contracts/lib" "$S/contracts/lib"
# forge resolves symlinks and refuses reads outside fs_permissions, so the inputs are read-only copies
for f in results/e7/vectors.json contracts/results/test_vectors.json; do
  rm -f "$S/$f"; cp "$M/$f" "$S/$f"; chmod 444 "$S/$f"
  cmp -s "$M/$f" "$S/$f" || { echo "copy differs: $f"; exit 7; }
  echo "input $f sha256 $(sha256sum < "$S/$f" | cut -c1-16) mode $(stat -c %A "$S/$f")"
done
rm -rf "$S/results/out"; mkdir -p "$S/results/out"
snap() { (cd "$M" && find results/e7 contracts/results -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum; \
          find results/e7 contracts/results -type f -print0 | sort -z | xargs -0 stat -c '%Y %s %n' | sha256sum; \
          find results/e7 contracts/results -type f | wc -l); }
before=$(snap)
O="$S/results/out"
cd "$S/contracts" || exit 9
{
  echo "# scratch copy of contracts/ at $C; inputs symlinked from the main checkout; outputs under $O"
  echo "# $($F --version | head -1); $(date -u +%FT%TZ)"
  E7_VECTORS=../results/e7/vectors.json VECTORS=results/test_vectors.json \
  E7_COUNTS=../results/out/conformance_counts.json E7_GAS_CSV=../results/out/gas_cumulative.csv \
  HOOK_LIFECYCLE_GAS_OUTPUT=../results/out/hook_lifecycle_gas.csv \
  CUMULATIVE_GAS_OUTPUT=../results/out/cumulative_solidity_gas.csv GAS_OUTPUT=../results/out/solidity_gas.csv \
  $F test --offline \
    --match-contract '^(CumulativeSurplusAccountingTest|HookGasBenchmarkTest|E7ConformanceTest|SurplusSharingAccountingTest|E7GasProfileTest)$' -vv 2>&1
} > "$O/older_suites.log"
after=$(snap)
echo "main results/e7 + contracts/results (mtime,size,name hash; file count):"
echo " before: $before" | tr '\n' ' '; echo
echo " after:  $after" | tr '\n' ' '; echo
[ "$before" = "$after" ] && echo "UNCHANGED" || echo "CHANGED"
ls -la "$O"
grep -E "^Ran |Suite result|FAIL" "$O/older_suites.log"
