#!/bin/bash
# Reruns every block-scope suite (and the tx-scoped suites that write no files) on the worktree's HEAD,
# saving one log per run under results/e7_block_scope/logs_<commit>/.
W="/mnt/c/Users/aradk/Desktop/master/Implermenant loss/rbitrage-Aware-Fee-Sharing/.claude/worktrees/s1"
cd "$W/contracts" || exit 9
C="$1"; FULL="$2"   # short commit (+dirty) and full hash, from Windows git (WSL git cannot read the worktree)
[ -n "$C" ] || exit 8
L="$W/results/e7_block_scope/logs_${C%+dirty}"
mkdir -p "$L"
F=~/.foundry/bin/forge
run() { # name mode pattern [args]
  local name="$1" mode="$2" pat="$3"; shift 3
  local iso=""; [ "$mode" = iso ] && iso="--isolate"
  {
    echo "# commit $C ($FULL); $(date -u +%FT%TZ); $($F --version | head -1)"
    echo "# forge test --offline $iso --match-contract '$pat' $*"
    $F test --offline $iso --match-contract "$pat" "$@" 2>&1
  } > "$L/$name.log"
  echo "$name: $(grep -E '^Ran [0-9]+ test suites' "$L/$name.log" | tail -1)"
}
run conformance_iso     iso   '^BlockScopeConformanceTest$' -vv
run blockscoped_iso     iso   '^(BlockScopedHookTest|ParticipationAwareHookFeeTest)$' --ffi
run scenarios_iso       iso   '^BlockScopedHookScenariosTest$' --ffi
run boundary_iso        iso   '^BlockScopeBoundaryTest$' --ffi
run props_iso           iso   '^BlockScopeV1PropertiesTest$' --ffi
run fuzz_iso            iso   '^BlockScopeFuzzTest$' --ffi -vv
run gas_iso             iso   '^BlockScopedHookGasTest$' -vv
run boundary_noiso      noiso '^BlockScopeBoundary' --ffi
run blockscoped_noiso   noiso '^ParticipationAwareHookFeeTest$' --ffi
run fuzz_noiso          noiso '^BlockScopeFuzzTest$' --ffi -vv
run txscoped_noiso      noiso '^(TxScopedParticipationAwareHookTest|HookGasIsolatedTest|HookGasSettledTest)$'
run txscoped_iso        iso   '^(TxScopedParticipationAwareHookTest|HookGasIsolatedTest|HookGasSettledTest)$'
echo "logs: $L"
