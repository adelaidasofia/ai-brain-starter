#!/usr/bin/env bash
# Test: detect-closing-signal.py strict_guards suppress all tiers (including
# explicit + high_confidence + custom) for unambiguous non-close contexts.
#
# Bug class: high_confidence regex `\b(let'?s\s+)?close\s+(this|the|out|up)\s+session\b`
# matched meta-discussion of the close cascade itself ("fix close-session regex",
# "why does session keep firing", "let me close the database session"). The
# 2026-05-12 fix made strong tiers override false_positive_guards (so legit
# "okay, let's close this session" wouldn't be suppressed by the "okay let's"
# transition guard) — but that broke meta-discussion suppression entirely. 139
# empty session stubs accumulated vault-wide before the strict_guards tier was
# added.
#
# Assertions:
#   META-DISCUSSION (should NOT fire):
#     1. "why do my sessions keep auto archiving? please diagnose and fix"
#     2. "fix the close-this-session regex"
#     3. "debug why close this session keeps firing"
#     4. "the regex pattern is `close this session` and it fires too much"
#     5. "let me close out the database session"
#     6. "sessions keep getting auto-archived"
#     7. "sessions keep firing the cascade"
#   LEGITIMATE CLOSES (should still fire — strict guards must NOT over-suppress):
#     8. "bye for now"
#     9. "good night"
#    10. "let's close this session"
#    11. "wrap it up"
#    12. "thanks that's all"
#    13. "okay let's close this session" (2026-05-12 regression case)
#   INTERROGATIVE meta-questions ABOUT a close (should NOT fire — added
#   2026-06-30 after "did you close this session?" fired the full cascade):
#    14. "did you close this session?"   (the reported false-positive)
#    15. "have you closed the session?"
#    16. "is the session closed?"
#   MODAL-REQUEST close phrased as a question (should STILL fire — the
#   negative control proving the interrogative guard discriminates a
#   question ABOUT closing from a request TO close):
#    17. "can you close this session?"
#
# Self-contained: tmpdir fake vault, HOME redirected. Exit 0 = pass, 1 = fail.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/detect-closing-signal.py"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
PACK="$REPO_ROOT/templates/closing-signals/en.json"
if [ ! -f "$HOOK" ]; then
  echo "ERROR: $HOOK not found" >&2
  exit 1
fi
if [ ! -f "$PACK" ]; then
  echo "ERROR: $PACK not found" >&2
  exit 1
fi

# Verify pack has strict_guards key — otherwise the test would silently
# pass against the pre-fix code where strong-tier overrides FP guards.
if ! python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get('strict_guards') else 1)" "$PACK" 2>/dev/null; then
  echo "ERROR: $PACK missing strict_guards array (fix regressed)" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
sandbox_home "$TMP/fake-home"

VAULT="$TMP/vault"
META="$VAULT/Meta"
mkdir -p "$META/Sessions" "$META/Decisions"

run_hook() {
  local prompt="$1"
  printf '{"prompt":%s,"session_id":"test-sid","cwd":%s}' \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$prompt")" \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$VAULT")" \
    | VAULT_ROOT="$VAULT" python3 "$HOOK"
}

assert_no_fire() {
  local prompt="$1"
  local output
  output="$(run_hook "$prompt")"
  # No-fire = passthrough JSON, no SESSION CLOSE / POSSIBLE SESSION CLOSE marker
  if echo "$output" | grep -qE "SESSION CLOSE|POSSIBLE SESSION CLOSE"; then
    echo "FAIL [should NOT fire]: $prompt" >&2
    echo "  output: $output" >&2
    return 1
  fi
  return 0
}

assert_fires() {
  local prompt="$1"
  local output
  output="$(run_hook "$prompt")"
  # Should fire = SESSION CLOSE detected (or POSSIBLE for ambiguous)
  if ! echo "$output" | grep -qE "SESSION CLOSE|POSSIBLE SESSION CLOSE"; then
    echo "FAIL [should fire]: $prompt" >&2
    echo "  output: $output" >&2
    return 1
  fi
  return 0
}

failed=0

# META-DISCUSSION (should NOT fire — strict_guards must suppress)
for p in \
  "why do my sessions keep auto archiving? please diagnose and fix" \
  "fix the close-this-session regex" \
  "debug why close this session keeps firing" \
  "the regex pattern is \`close this session\` and it fires too much" \
  "let me close out the database session" \
  "sessions keep getting auto-archived" \
  "sessions keep firing the cascade" \
  "did you close this session?" \
  "have you closed the session?" \
  "is the session closed?" \
; do
  assert_no_fire "$p" || failed=$((failed+1))
done

# LEGITIMATE CLOSES (should still fire — strict_guards must not over-suppress)
for p in \
  "bye for now" \
  "good night" \
  "let's close this session" \
  "wrap it up" \
  "thanks that's all" \
  "okay let's close this session" \
  "can you close this session?" \
; do
  assert_fires "$p" || failed=$((failed+1))
done

if [ "$failed" -gt 0 ]; then
  echo "FAIL: $failed assertion(s) failed" >&2
  exit 1
fi
echo "PASS: strict_guards correctly gate meta-discussion vs legitimate closes"
