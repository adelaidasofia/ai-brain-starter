#!/usr/bin/env bash
# Test: detect-closing-signal.py emits PHASE 4b (/goal clear) when — and only
# when — this session has a /goal still set.
#
# Why: `/goal <condition>` installs a session-scoped Stop hook that blocks
# stopping until the condition holds. At a deliberate close that hook fights the
# cascade: the model finishes, Stop fires, the goal blocks it, and the model is
# re-invoked with nothing left to do. Only the user can run `/goal clear` (it is
# a client-side command with no tool behind it), so the cascade has to hand the
# instruction back to them.
#
# The goal condition lives only in Claude Code's in-memory session state — there
# is no goal state file — so detection replays the transcript's /goal command
# records. That makes precision the whole ballgame, hence the negative controls:
# a false PHASE 4b tells the user to clear a goal that is not there, and the
# harness explicitly rules out nagging about /goal clear after success.
#
# Assertions:
#   FIRES (PHASE 4b present):
#     1. /goal set, never cleared, substantive session
#     2. /goal set, trivial session (<5 user msgs) — a live goal blocks the
#        close regardless of session size, so it survives the skip-if-trivial rule
#     3. /goal set, cleared, then set again — last write wins
#   DOES NOT FIRE (no PHASE 4b):
#     4. no /goal anywhere in the transcript
#     5. /goal set then explicitly cleared with `/goal clear`
#     6. bare /goal with no args (that is a status query, not a set)
#     7. prose that merely mentions "/goal clear" with no command record
#        (precision control — this very repo's own commit messages do that)
#
# Self-contained: tmpdir fake vault + synthetic transcript, HOME redirected.
# Exit 0 = pass, 1 = fail.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/detect-closing-signal.py"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
if [ ! -f "$HOOK" ]; then
  echo "ERROR: $HOOK not found" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
sandbox_home "$TMP/fake-home"

VAULT="$TMP/vault"
META="$VAULT/Meta"
mkdir -p "$META/Sessions" "$META/Decisions"

# Build a transcript from a list of user-message bodies.
build_transcript() {
  local out="$1"; shift
  : > "$out"
  for body in "$@"; do
    python3 -c '
import json, sys
print(json.dumps({"type": "user",
                  "message": {"role": "user", "content": sys.argv[1]}}))
' "$body" >> "$out"
  done
}

GOAL_SET='<command-name>/goal</command-name>
            <command-message>goal</command-message>
            <command-args>ship the release and keep every test green</command-args>'
GOAL_CLEAR='<command-name>/goal</command-name>
            <command-message>goal</command-message>
            <command-args>clear</command-args>'
GOAL_BARE='<command-name>/goal</command-name>
            <command-message>goal</command-message>'
GOAL_PROSE='add "/goal clear" to the session close cascade if the session started with "/goal"'

# 5 filler messages keeps the session above the <5 skip-if-trivial threshold.
FILLER=("work one" "work two" "work three" "work four" "work five")

run_hook() {
  local transcript="$1"
  printf '{"prompt":%s,"session_id":"test-sid","cwd":%s,"transcript_path":%s}' \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "close this session")" \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$VAULT")" \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$transcript")" \
    | VAULT_ROOT="$VAULT" python3 "$HOOK"
}

failed=0

assert_phase4b() {
  local label="$1" transcript="$2" output
  output="$(run_hook "$transcript")"
  if ! echo "$output" | grep -q "PHASE 4b"; then
    echo "FAIL [expected PHASE 4b]: $label" >&2
    failed=1
    return
  fi
  # The condition must be carried through, else the model cannot tell the user
  # which goal it is talking about.
  if ! echo "$output" | grep -q "keep every test green"; then
    echo "FAIL [PHASE 4b present but condition missing]: $label" >&2
    failed=1
    return
  fi
  # It must hand the literal command back — that is the whole payload.
  if ! echo "$output" | grep -q "/goal clear"; then
    echo "FAIL [PHASE 4b present but no /goal clear command]: $label" >&2
    failed=1
    return
  fi
  echo "ok   [fires]     $label"
}

assert_no_phase4b() {
  local label="$1" transcript="$2" output
  output="$(run_hook "$transcript")"
  # The cascade itself must still fire — this asserts the goal block is absent,
  # not that the hook went silent.
  if ! echo "$output" | grep -qE "SESSION CLOSE"; then
    echo "FAIL [cascade did not fire at all]: $label" >&2
    failed=1
    return
  fi
  if echo "$output" | grep -q "PHASE 4b"; then
    echo "FAIL [unexpected PHASE 4b]: $label" >&2
    echo "  output: $output" >&2
    failed=1
    return
  fi
  echo "ok   [no-fire]   $label"
}

# 1. goal set, never cleared
build_transcript "$TMP/t1.jsonl" "$GOAL_SET" "${FILLER[@]}"
assert_phase4b "goal set, never cleared" "$TMP/t1.jsonl"

# 2. goal set, trivial session (2 user messages, under the skip threshold)
build_transcript "$TMP/t2.jsonl" "$GOAL_SET" "one bit of work"
assert_phase4b "goal set, trivial session" "$TMP/t2.jsonl"

# 3. set, cleared, set again — last write wins
build_transcript "$TMP/t3.jsonl" \
  '<command-name>/goal</command-name>
            <command-args>an older goal</command-args>' \
  "$GOAL_CLEAR" "$GOAL_SET" "${FILLER[@]}"
assert_phase4b "set, cleared, set again" "$TMP/t3.jsonl"

# 4. no goal at all
build_transcript "$TMP/t4.jsonl" "${FILLER[@]}"
assert_no_phase4b "no /goal in transcript" "$TMP/t4.jsonl"

# 5. goal set then explicitly cleared
build_transcript "$TMP/t5.jsonl" "$GOAL_SET" "${FILLER[@]}" "$GOAL_CLEAR"
assert_no_phase4b "goal set then cleared" "$TMP/t5.jsonl"

# 6. bare /goal (status query, no args)
build_transcript "$TMP/t6.jsonl" "$GOAL_BARE" "${FILLER[@]}"
assert_no_phase4b "bare /goal status query" "$TMP/t6.jsonl"

# 7. prose mentioning /goal clear, no command record
build_transcript "$TMP/t7.jsonl" "$GOAL_PROSE" "${FILLER[@]}"
assert_no_phase4b "prose mentioning /goal, no command record" "$TMP/t7.jsonl"

if [ "$failed" -ne 0 ]; then
  echo "FAILED: detect-closing-signal /goal clear phase" >&2
  exit 1
fi
echo "PASS: detect-closing-signal emits PHASE 4b only for a live /goal"
