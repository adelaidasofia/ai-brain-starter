#!/usr/bin/env bash
# Regression test: the install must never offer a work-only / operational-only
# path. The personal brain (journaling, floor framework, advisory panel,
# insights, life reflection) installs by default for everyone.
#
# Bug class: the install detects (or the user describes) a heavily operational
# vault and the assistant improvises a global scope question -- "what do you
# want this brain *for*? that decides which phases I run" -- then skips the
# whole personal half. The skill already says "one install, every phase, no
# tiers," but it had no explicit guard against narrowing scope from a
# detected-operational vault, so the improvisation leaked.
#
# Source incident: a real /setup-brain run on an all-operational vault. The
# assistant asked the user to pick a scope ("solo operación") and skipped
# journaling, the panel, health, and books. The personal brain is the gift,
# not an add-on -- it must arrive as the default design, never as a removed
# option or a bug.
#
# This test fails if the scope guard is removed from SKILL.md or
# phase-01-welcome.md, or if the existing "no tiers" guard regresses.
#
# Self-contained. Exit 0 = pass. Exit 1 = fail with details.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SKILL="$REPO_ROOT/SKILL.md"
PHASE01="$REPO_ROOT/phases/phase-01-welcome.md"

FAILED=0

# must_contain <file> <failure-label> <literal-substring>
must_contain() {
  local file="$1" label="$2" needle="$3"
  if [ ! -f "$file" ]; then
    echo "FAIL: $label" >&2
    echo "  file not found: $file" >&2
    FAILED=$((FAILED + 1))
    return 0
  fi
  if ! grep -qF -- "$needle" "$file"; then
    echo "FAIL: $label" >&2
    echo "  expected to find in $(basename "$file"): \"$needle\"" >&2
    FAILED=$((FAILED + 1))
  fi
  return 0
}

# 1. SKILL.md banned-patterns table carries the scope-gate row.
must_contain "$SKILL" \
  "SKILL.md banned-patterns table no longer bans the work-only / ops-only scope gate" \
  "no work-only / ops-only path"

# 2. SKILL.md states the personal brain is the default gift, not an add-on.
must_contain "$SKILL" \
  "SKILL.md no longer affirms the personal brain as the non-negotiable default" \
  "The personal brain is the gift, not an add-on."

# 3. The pre-existing "no tiers" guard must not regress.
must_contain "$SKILL" \
  "SKILL.md regressed: the 'no tiers, no light/full split' guard is gone" \
  "No tiers, no light/full split"

# 4. phase-01 carries the scope-fixed guard (Step 1.-1c) for new + upgrade modes.
must_contain "$PHASE01" \
  "phase-01-welcome.md is missing the scope-fixed guard (Step 1.-1c)" \
  "Scope is fixed: the personal brain always installs"
must_contain "$PHASE01" \
  "phase-01-welcome.md no longer forbids asking what the brain is 'for'" \
  "ask what the brain is"

# 5. An explicit ops-only request gets ONE warm pitch, then the user's answer
#    wins. The old "decline once and proceed with the full install" contract
#    overrode an explicit user choice — fresh model instances (correctly)
#    refuse to run that, which killed entire installs. Pin the new contract.
must_contain "$PHASE01" \
  "phase-01-welcome.md is missing the pitch-once line for ops-only requests" \
  "make the case once, warmly"
must_contain "$PHASE01" \
  "phase-01-welcome.md no longer honors an explicit ops-only choice" \
  "their explicit choice always wins"

if [ "$FAILED" -gt 0 ]; then
  echo "" >&2
  echo "$FAILED personal-brain-not-optional check(s) failed." >&2
  echo "The install must never offer a work-only / operational-only path." >&2
  echo "The personal brain installs by default. See SKILL.md banned-patterns" >&2
  echo "table and phases/phase-01-welcome.md Step 1.-1c." >&2
  exit 1
fi

echo "PASS: personal brain is non-optional. SKILL.md scope-gate ban + 'gift not add-on' statement + 'no tiers' guard, and phase-01 Step 1.-1c scope guard all intact."
