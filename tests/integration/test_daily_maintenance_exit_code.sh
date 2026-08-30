#!/usr/bin/env bash
# Negative control for MYC-4116: scripts/vault-daily-maintenance.sh used to log
# every step's exit code and then DISCARD it, ending in an unconditional
# `exit 0`. A leg that had been failing for weeks reported success to launchd
# every night, so the one surface anyone checks said the job was healthy.
#
# WHAT THIS PROVES, AND WHAT IT DOES NOT
#   It exercises the REAL `run()` definition and the REAL summary block, lifted
#   verbatim out of the shipped script rather than retyped here -- a control
#   built from a copy of the logic proves only that the copy works. The
#   extraction is asserted, so if either block is renamed or moved this test
#   goes RED rather than silently testing nothing.
#
#   It does NOT boot the whole maintenance pass (that needs a real vault, the
#   close mutex, and git). The claim is scoped to the accounting and the exit
#   predicate, which is exactly what MYC-4116 was about.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TARGET="$REPO_ROOT/scripts/vault-daily-maintenance.sh"
FAILED=0

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fail() { echo "  FAIL: $*" >&2; FAILED=1; }
ok()   { echo "  ok   $*"; }

[ -f "$TARGET" ] || { echo "FAIL: $TARGET missing" >&2; exit 1; }

# --- lift the two real blocks out of the shipped script ---------------------
# `run()` ... its closing brace, and the summary block from MAINT_STATE_DIR to
# the final `exit 0`. Both extractions are asserted below: an empty lift means
# the block moved, and a control that tests an empty string always passes.
run_block="$(awk '/^run\(\) \{/,/^\}/' "$TARGET")"
summary_block="$(awk '/^MAINT_STATE_DIR=/,/^exit 0$/' "$TARGET")"

if ! printf '%s' "$run_block" | grep -q 'FAILED_STEPS+='; then
  fail "could not lift run() with its FAILED_STEPS accounting -- the block moved,"
  fail "so this control would have tested nothing. Fix the extraction, not the assert."
  exit 1
fi
if ! printf '%s' "$summary_block" | grep -q 'exit 1'; then
  fail "could not lift the summary block with its non-zero exit -- extraction is stale."
  exit 1
fi
ok "lifted run() + summary block from the shipped script (not retyped)"

# --- harness: the real blocks, with the vault/mutex setup stubbed out -------
build_harness() { # build_harness <steps...>  -> a runnable script at $tmp/h.sh
  {
    echo '#!/usr/bin/env bash'
    echo 'set -uo pipefail'
    echo 'log() { printf "%s\n" "$*"; }'
    echo 'FAILED_STEPS=()'
    printf '%s\n' "$run_block"
    cat                      # caller pipes in the step invocations
    printf '%s\n' "$summary_block"
  } > "$tmp/h.sh"
  chmod +x "$tmp/h.sh"
}

# CASE 1 -- a FAILING step must reach the exit code. This is the defect.
build_harness <<'STEPS'
run "deliberately-failing-leg" false
STEPS
set +e
HOME="$tmp/home1" bash "$tmp/h.sh" > "$tmp/o1" 2>&1
rc1=$?
set -e
if [ "$rc1" -eq 0 ]; then
  fail "a failing step still exited 0 -- the swallow is back (MYC-4116)"
else
  ok "failing step -> exit $rc1"
fi
grep -q 'FAILED STEP' "$tmp/o1" || fail "the log line does not name the failed step"
if [ -f "$tmp/home1/.claude/state/vault-daily-maintenance-last.json" ]; then
  grep -q 'deliberately-failing-leg' \
    "$tmp/home1/.claude/state/vault-daily-maintenance-last.json" \
    || fail "state file does not name the failed step"
  ok "state file names the failed step"
else
  fail "no state file written for a failed run"
fi

# CASE 2 -- the inverse. Without this, a harness that exited non-zero for ANY
# reason would satisfy CASE 1 and the control would be theatre.
build_harness <<'STEPS'
run "healthy-leg" true
STEPS
set +e
HOME="$tmp/home2" bash "$tmp/h.sh" > "$tmp/o2" 2>&1
rc2=$?
set -e
if [ "$rc2" -ne 0 ]; then
  fail "an all-green pass exited $rc2, expected 0 -- CASE 1 may be passing for the wrong reason"
else
  ok "all-green pass -> exit 0"
fi

# CASE 3 -- a failing step must NOT abort the remaining steps. That behaviour
# was correct before and is the reason run() swallows rather than `set -e`s;
# breaking it while fixing the exit code would trade one defect for a worse one.
build_harness <<'STEPS'
run "first-leg-fails" false
run "second-leg-still-runs" true
STEPS
set +e
HOME="$tmp/home3" bash "$tmp/h.sh" > "$tmp/o3" 2>&1
rc3=$?
set -e
grep -q 'second-leg-still-runs' "$tmp/o3" \
  || fail "a failing leg aborted the pass -- later legs must still run"
[ "$rc3" -ne 0 ] || fail "mixed pass should still exit non-zero"
ok "a failing leg does not abort the legs after it (exit $rc3)"

if [ "$FAILED" -ne 0 ]; then
  echo "test_daily_maintenance_exit_code: FAILED" >&2
  exit 1
fi
echo "test_daily_maintenance_exit_code: OK"
