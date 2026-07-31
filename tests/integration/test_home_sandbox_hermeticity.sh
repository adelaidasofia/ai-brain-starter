#!/usr/bin/env bash
# Static guard: a test that redirects HOME must redirect USERPROFILE with it.
#
# THE BUG THIS LOCKS OUT (MYC-3536)
#
# Tests here sandbox HOME so the developer's real ~/.claude is never touched.
# On POSIX that works. On Windows it does not: Python resolves "~" through
# ntpath.expanduser, which reads USERPROFILE and ignores HOME entirely. So a
# test that sets only HOME looks sandboxed, passes review, and runs against the
# real ~/.claude on every Windows dev machine.
#
# It did. On 2026-07-30 the suite rewrote the real ~/.claude/settings.json,
# repointing 95 of 111 hook entries at hook_runner.py inside the throwaway git
# worktree the tests happened to run from. Deleting that worktree left every
# hook launching a file that no longer existed; CPython exits 2 for "can't open
# file", and exit 2 is Claude Code's intentional-BLOCK signal — so every tool
# call in later, unrelated sessions was denied, with nothing tying the failure
# back to the test run. Same fail-closed class as #375 and #409.
#
# The companion runtime tripwire lives in scripts/ci.sh (content + mtime of the
# real settings.json around every test). This static half catches the mistake at
# author time, on Linux CI, where the runtime half cannot see it — HOME works
# fine there, so the escape is invisible until someone runs the suite on Windows.
#
# Asserts, for every tests/integration/test_*.sh that assigns HOME:
#   (a) it sources lib/sandbox_home.sh, or otherwise assigns USERPROFILE too;
#   (b) NEGATIVE CONTROL: a synthetic HOME-only test is actually caught, so a
#       broken detector can't pass this file by matching nothing.
#
# Stdlib bash only. Exit 0 = pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1 :: $2"; }

# Assignments to HOME itself — not FAKE_HOME=, TMP_HOME=, HOME_COPY=, or "$HOME".
# Anchored on start-of-line / whitespace / `(` so only the bare name matches.
HOME_ASSIGN='(^|[[:space:]]|\()(export[[:space:]]+)?HOME='
USERPROFILE_ASSIGN='(^|[[:space:]]|\()(export[[:space:]]+)?USERPROFILE='

# Tests that assign HOME but are deliberately NOT converted. Each needs a reason.
# Rule for this list: the test must not reach the installer/merge path or any
# other writer of ~/.claude. Verified 2026-07-31 by running the full suite under
# a decoy USERPROFILE and confirming none of these wrote into the decoy home.
# A test that starts touching ~/.claude must come off this list, not stay on it.
SANDBOX_EXEMPT=(
  test_granola_sync_offline           # offline Granola parse; HOME only picks the config dir
  test_meeting_workflow_trigger_hook  # trigger-phrase matching against a temp vault
  test_post_update_email_ask          # prompt-copy assertions; no ~/.claude write
  test_resource_aware_session_close    # load-shedding arithmetic against a temp vault
  test_scan_prior_failclosed_scrub    # scrubber runs entirely inside its own tmpdir
  test_scan_prior_single_instance     # lockfile contention inside its own tmpdir
  test_session_coordination_guards    # session lockfiles inside its own tmpdir
  test_vault_script_sync              # import-closure check over repo files only
)

is_exempt() {
  local n="$1" e
  for e in "${SANDBOX_EXEMPT[@]}"; do
    [ "$e" = "$n" ] && return 0
  done
  return 1
}

echo "=== 1. every HOME-redirecting test also redirects USERPROFILE ==="
offenders=()
checked=0
for f in "$SCRIPT_DIR"/test_*.sh; do
  name="$(basename "$f" .sh)"
  grep -qE "$HOME_ASSIGN" "$f" || continue
  checked=$((checked + 1))
  is_exempt "$name" && continue
  if grep -q 'sandbox_home.sh' "$f"; then continue; fi
  if grep -qE "$USERPROFILE_ASSIGN" "$f"; then continue; fi
  offenders+=("$name")
done

if [ "$checked" -eq 0 ]; then
  bad "detector matched something" "no test assigns HOME — the pattern must have rotted"
else
  ok "scanned $checked HOME-redirecting test(s)"
fi

if [ "${#offenders[@]}" -eq 0 ]; then
  ok "no test redirects HOME without USERPROFILE"
else
  bad "HOME-only sandbox" "these redirect HOME but not USERPROFILE, so on Windows they run against the REAL ~/.claude. Source tests/integration/lib/sandbox_home.sh and use sandbox_home/run_sandboxed (or add to SANDBOX_EXEMPT with a reason): ${offenders[*]}"
fi

echo "=== 2. exempt list stays honest (no rows for files that no longer exist) ==="
stale=()
for e in "${SANDBOX_EXEMPT[@]}"; do
  [ -f "$SCRIPT_DIR/$e.sh" ] || stale+=("$e")
done
if [ "${#stale[@]}" -eq 0 ]; then
  ok "every SANDBOX_EXEMPT row names a real test"
else
  bad "stale exempt rows" "remove: ${stale[*]}"
fi

echo "=== 3. NEGATIVE CONTROL: the detector actually bites ==="
CTL="$(mktemp -d)"
trap 'rm -rf "$CTL"' EXIT
printf '%s\n' '#!/usr/bin/env bash' 'export HOME="$TMP/fake"' > "$CTL/test_synthetic_offender.sh"
if grep -qE "$HOME_ASSIGN" "$CTL/test_synthetic_offender.sh" \
   && ! grep -qE "$USERPROFILE_ASSIGN" "$CTL/test_synthetic_offender.sh"; then
  ok "synthetic HOME-only test is detected"
else
  bad "negative control" "the detector did NOT flag a synthetic HOME-only test — it would pass anything"
fi

# A file that sets both must NOT be flagged (guards against a detector that
# reports everything and is therefore equally useless).
printf '%s\n' '#!/usr/bin/env bash' 'export HOME="$T"' 'export USERPROFILE="$T"' \
  > "$CTL/test_synthetic_clean.sh"
if grep -qE "$USERPROFILE_ASSIGN" "$CTL/test_synthetic_clean.sh"; then
  ok "synthetic HOME+USERPROFILE test is not flagged"
else
  bad "negative control" "a correctly-sandboxed synthetic test was flagged"
fi

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
echo "PASS: HOME sandboxing is hermetic on Windows as well as POSIX (MYC-3536)"
