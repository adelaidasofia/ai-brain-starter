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
# MYC-3528: #410 (the three checks above) only fixed NEW registrations. Every
# install that ran setup before that fix still carries a broken task, and
# nothing repaired it. The guardrail for that fix: "Do NOT let this become
# 'print a warning.' ... Repair by default; warn only when repair is
# impossible." The fourth check below guards THAT discipline - a script that
# reads an existing task's health back but never repairs it is the same bug
# one level up, just with better logging.
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

# --- 4. finding a broken EXISTING task must repair it, not just warn --------
# MYC-3528's guardrail, verbatim: "Do NOT let this become 'print a warning.'
# The failure mode of this entire class is that every layer reported success.
# A warning nobody reads is the same bug one level up. Repair by default;
# warn only when repair is impossible." The single most plausible way this
# regresses again is a well-intentioned half-edit: the health check and its
# branch stay, only the repair CALL inside the broken branch gets dropped (a
# file-wide "does Register-BackupTask appear anywhere" grep would miss this,
# since the function's own definition keeps the symbol present) - so this
# anchors on the actual branch point (`.Count -eq 0`) and requires a repair
# call within a few lines of it, not just somewhere in the file.
heals=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  # code_hits (not a raw grep): a file that only EXPLAINS why it calls
  # Get-ScheduledTask - like this guard's own file, and like
  # test-vault-backup-task-healing.ps1's docstring, which says in prose that
  # Get-ScheduledTask "cannot run here" - is not a registrar. Caught by hand:
  # a raw `grep -qF` version flagged that test file on its own comment.
  [ -n "$(code_hits "$f" 'Get-ScheduledTask')" ] || continue
  validates=0
  for sym in 'Get-BackupTaskProblems' 'Test-BackupTaskProblems' 'Test-BackupTaskHealthy'; do
    [ -n "$(code_hits "$f" "$sym")" ] && validates=1
  done
  [ "$validates" -eq 1 ] || continue
  heals=$((heals + 1))
  decision_line="$(grep -nE '\.Count -eq 0' "$REPO/$f" | grep -vE '^[0-9]+:[[:space:]]*#' | head -1 | cut -d: -f1)"
  if [ -z "$decision_line" ]; then
    echo "FAIL  validates an existing task's health but the healthy/broken branch point"
    echo "        (a '.Count -eq 0' check) is not where this guard expects it:"
    echo "        $f"
    fail=1
    continue
  fi
  # Bound the window at THIS if/else block's own closing brace (a lone "}" at
  # the SAME indentation as the "if" line), not a fixed line count. A fixed
  # count leaks into the NEXT sibling branch's own legitimate repair call
  # (the "no task exists yet, register fresh" branch also calls
  # Register-BackupTask a few lines later) and would false-pass exactly the
  # regression this check exists to catch - caught by hand while writing this
  # guard: a 15-line window matched the sibling branch's call and missed a
  # deliberately-broken "warn only" version entirely.
  indent="$(sed -n "${decision_line}p" "$REPO/$f" | sed -E 's/^([[:space:]]*).*/\1/')"
  block_end="$(awk -v start="$decision_line" -v pat="^${indent}}\$" 'NR>=start && $0 ~ pat {print NR; exit}' "$REPO/$f")"
  if [ -z "$block_end" ]; then block_end=$((decision_line + 10)); fi
  if ! sed -n "${decision_line},${block_end}p" "$REPO/$f" | grep -qE 'Register-BackupTask|Register-ScheduledTask'; then
    echo "FAIL  found the healthy/broken branch but no repair call within it:"
    echo "        $f (lines $decision_line-$block_end)"
    echo "        A detector that only warns is the same bug one level up."
    fail=1
  fi
done < <(ps1_files)

if [ "$fail" -eq 0 ]; then
  echo "PASS  $n .ps1 file(s): no empty self-path, no hardcoded pwsh, $regs registrar(s) verify their own work, $heals self-heal path(s) repair (not just warn)"
  exit 0
fi
exit 1
