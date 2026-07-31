#!/usr/bin/env bash
# Structural regression - the "registered but dead" class.
#
# vault-backup.ps1 setup registered a daily task that could never run, on any
# machine, and then printed "Backup is live." Measured on a real install: 25
# days with no snapshot. Three independent defects in two lines:
#
#   1. `$self = $MyInvocation.MyCommand.Path` INSIDE a function. That variable
#      describes the FUNCTION's invocation, not the script file, and is EMPTY
#      there - so the task registered with `-File ""`. `$PSCommandPath` is the
#      correct spelling and is right at BOTH scopes, which is why this guard
#      bans the fragile one outright instead of trying to detect scope.
#   2. `-Execute "pwsh"`. PowerShell 7 is NOT on a stock Windows install, so
#      the action fails 0x80070002 on the interpreter even given a good path.
#   3. Default task settings refuse to start on battery, so a laptop's 03:00
#      run is refused nightly (0x800710E0) and the vault quietly rots.
#
# The reason all three survived to a user: `Register-ScheduledTask` SUCCEEDS
# when handed an action that can never execute. Registration is not execution.
# So the third check below requires any registrar to read its own work back.
#
# Bash only, no network. exit 0 = no registrar can ship dead again.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fail=0

ps1_files() { git -C "$REPO" ls-files -- '*.ps1' 2>/dev/null; }

# Code only. A guard that cannot tell a banned call from a comment EXPLAINING
# the ban flags its own documentation - including the comment a few lines up.
code_hits() { grep -nF -- "$2" "$REPO/$1" 2>/dev/null | grep -vE '^[0-9]+:[[:space:]]*#'; }

# --- 1. $MyInvocation.MyCommand.Path is empty inside a function -------------
n=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  n=$((n + 1))
  if [ -n "$(code_hits "$f" 'MyInvocation.MyCommand.Path')" ]; then
    echo "FAIL  \$MyInvocation.MyCommand.Path is EMPTY inside a function:"
    echo "        $f"
    echo "        Use \$PSCommandPath - correct at top level AND in a function."
    fail=1
  fi
done < <(ps1_files)
if [ "$n" -eq 0 ]; then
  echo "FAIL  found ZERO tracked .ps1 files - this guard is blind."
  exit 1
fi

# --- 2. a hardcoded pwsh is a missing interpreter on stock Windows ----------
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if grep -nE 'New-ScheduledTaskAction[^|]*-Execute[[:space:]]+"?pwsh"?' "$REPO/$f" >/dev/null 2>&1; then
    echo "FAIL  scheduled task hardcodes 'pwsh', absent on a stock Windows install:"
    echo "        $f"
    echo "        Resolve it: pwsh if present, else powershell.exe, else fail loud."
    fail=1
  fi
done < <(ps1_files)

# --- 3. registering is not running: a registrar must verify its own work ----
regs=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  grep -qF 'Register-ScheduledTask' "$REPO/$f" || continue
  regs=$((regs + 1))
  if ! grep -qF 'Get-ScheduledTask' "$REPO/$f"; then
    echo "FAIL  registers a scheduled task but never reads it back:"
    echo "        $f"
    echo "        Register-ScheduledTask SUCCEEDS on an action that cannot run."
    echo "        Read the registration back and prove exe + -File both resolve."
    fail=1
  fi
done < <(ps1_files)

if [ "$fail" -eq 0 ]; then
  echo "PASS  $n .ps1 file(s): no empty self-path, no hardcoded pwsh, $regs registrar(s) verify their own work"
  exit 0
fi
exit 1
