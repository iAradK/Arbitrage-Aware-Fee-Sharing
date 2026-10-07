#!/bin/bash
# Redo blockscoped_iso without ParticipationAwareHookFeeTest (that suite refuses --isolate; it runs in blockscoped_noiso).
W="/mnt/c/Users/aradk/Desktop/master/Implermenant loss/rbitrage-Aware-Fee-Sharing/.claude/worktrees/s1"
cd "$W/contracts" || exit 9
C="$1"; FULL="$2"
[ -n "$C" ] || exit 8
L="$W/results/e7_block_scope/logs_$C"
F=~/.foundry/bin/forge
{
  echo "# commit $C ($FULL); $(date -u +%FT%TZ); $($F --version | head -1)"
  echo "# forge test --offline --isolate --match-contract '^BlockScopedHookTest\$' --ffi"
  $F test --offline --isolate --match-contract '^BlockScopedHookTest$' --ffi 2>&1
} > "$L/blockscoped_iso.log"
grep -E '^Ran [0-9]+ test suites?' "$L/blockscoped_iso.log"
