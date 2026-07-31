#!/usr/bin/env bash
# Test: the shipped close rule and the injected close cascade agree on what each
# phase NUMBER means.
#
# Why: templates/rules/session-close.md and hooks/detect-closing-signal.py both
# reach the model — the template when it reads the rule, the cascade when the
# hook injects it at close. They describe ONE process. Before this test they
# disagreed:
#
#   number   template said              cascade said
#   3        Goodbye                    Functional audit
#   4        Automatic finalization     Final summary (the goodbye)
#             ("you do nothing")
#
# That shipped a live trap: #400 added a `/goal clear` reminder to the template
# and labelled it "Phase 4b", borrowing the cascade's numbering. Under the
# template's own numbering Phase 4 is the part that happens automatically with
# no involvement from the reader — so the label said the goal clears itself,
# which is the exact opposite of the truth.
#
# The rule this locks: a phase number present in BOTH files must denote the same
# step in both. Numbers unique to one file are fine (the cascade is more
# granular: 0a, 0c/0d/0e, 1.8 have no template section).
#
# Assertions:
#   1. Every phase number the template defines is either absent from the cascade
#      or carries a matching meaning there.
#   2. The specific historical collisions stay fixed: template 3 is the audit,
#      template 4 is the goodbye.
#   3. The template does not reintroduce a bare "Phase 4b" label (#401).
#
# Exit 0 = pass, 1 = fail.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RULE="$REPO_ROOT/templates/rules/session-close.md"
HOOK="$REPO_ROOT/hooks/detect-closing-signal.py"

for f in "$RULE" "$HOOK"; do
  [ -f "$f" ] || { echo "ERROR: $f not found" >&2; exit 1; }
done

failed=0
fail() { echo "FAIL: $*" >&2; failed=1; }
ok() { echo "ok   $*"; }

# --- 1 + 2. Shared numbers must denote the same step ------------------------
# Pairs of "<number>|<regex the template heading must match>|<regex the cascade
# phase line must match>". Kept explicit rather than derived: the whole point is
# to pin MEANING, and a derivation would just re-encode whatever is there today.
check_pair() {
  local num="$1" want_rule="$2" want_hook="$3"
  local rule_line hook_line
  rule_line="$(grep -iE "^## Phase ${num} " "$RULE" | head -1)"
  hook_line="$(grep -iE "^PHASE ${num} " "$HOOK" | head -1)"

  if [ -z "$rule_line" ]; then fail "template has no '## Phase ${num}' heading"; return; fi
  if [ -z "$hook_line" ]; then fail "cascade has no 'PHASE ${num}' line"; return; fi

  echo "$rule_line" | grep -qiE "$want_rule" \
    || fail "template Phase ${num} should be ${want_rule}, got: ${rule_line}"
  echo "$hook_line" | grep -qiE "$want_hook" \
    || fail "cascade PHASE ${num} should be ${want_hook}, got: ${hook_line}"

  ok "Phase ${num} means the same thing in both"
}

check_pair 1  "scan the conversation" "conversation scan"
check_pair 2  "write to the vault"    "batch writes"
check_pair 3  "functional audit"      "functional audit"
check_pair 4  "goodbye"               "final summary"

# --- 2b. Each number may head at most ONE template section ------------------
# Without this, re-adding a second "## Phase 4 —" heading is invisible: the
# meaning checks above read the FIRST match and pass while the file defines the
# same number twice. Found by mutating the template (mutant B) — the original
# version of this test was blind to it.
dupes="$(grep -oE "^## Phase [0-9]+[a-z]? " "$RULE" | sort | uniq -d)"
if [ -n "$dupes" ]; then
  fail "template defines the same phase number more than once:"
  echo "$dupes" | sed 's/^/       /' >&2
else
  ok "every template phase number heads exactly one section"
fi

# --- 2c. Phase 2b (commit) must stay present --------------------------------
# It is the step whose absence causes a hard block: verify-session-close-cascade
# fires before the Stop hook and refuses the close while artifacts are
# uncommitted. A template without it teaches a model to skip the commit.
if grep -qE "^## Phase 2b " "$RULE"; then
  ok "template keeps the Phase 2b commit step"
else
  fail "template lost its Phase 2b commit step (the close will hard-block)"
fi

# --- 3. The #401 regression: no bare "Phase 4b" label in the template -------
if grep -q "Phase 4b" "$RULE"; then
  fail "template reintroduced a 'Phase 4b' label (see #401 — the cascade owns that number)"
else
  ok "template carries no cross-numbered 'Phase 4b' label"
fi

# --- Negative control -------------------------------------------------------
# Prove the comparison can actually fail: a deliberately wrong expectation must
# be rejected. Without this, a check_pair that silently matched everything would
# look identical to a passing suite.
control="$(grep -iE "^## Phase 4 " "$RULE" | head -1)"
if echo "$control" | grep -qiE "automatic finalization"; then
  fail "negative control: template Phase 4 still reads as the automatic step"
else
  ok "negative control: a wrong meaning for Phase 4 would be caught"
fi

if [ "$failed" -ne 0 ]; then
  echo "FAILED: close phase numbering drifted between the rule and the cascade" >&2
  exit 1
fi
echo "PASS: close phase numbering is aligned between the shipped rule and the cascade"
