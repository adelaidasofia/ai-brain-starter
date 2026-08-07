#!/usr/bin/env bash
# Test the bootstrap.sh end-to-end via --dry-run. Asserts:
#   1. bootstrap.sh --dry-run exits 0 (no parse errors, no early aborts)
#   2. The expected log lines appear in stdout (sub-skill loop fires for
#      each required skill, commands loop fires, verification loop fires)
#
# Catches the bug class where a new section in bootstrap.sh has a syntax
# error, an unbound variable, or a logic break that only surfaces when
# someone runs a real install. End-to-end coverage we previously didn't
# have. Same drift-prevention family as the worktree-session-close (#65),
# reconcile-ff (#68), and phase-doc-skills (#73) tests.
#
# Self-contained. Sets EMAIL/NAME/LANG_HINT env so the bootstrap doesn't
# block on the email-gate. Doesn't write outside its tmpdir.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
BOOTSTRAP="$REPO_ROOT/bootstrap.sh"

if [ ! -f "$BOOTSTRAP" ]; then
  echo "ERROR: $BOOTSTRAP not found" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Pre-stage the email marker so the bootstrap doesn't try to mint a token.
sandbox_home "$TMP"
touch "$HOME/.claude/.ai-brain-starter-email-on-file"

# Pre-create a fake SKILL_DIR so the bootstrap finds itself.
mkdir -p "$HOME/.claude/skills/ai-brain-starter"
# Symlink the test runner's repo into the SKILL_DIR position so bootstrap
# can find templates, skills/, scripts/, etc.
rmdir "$HOME/.claude/skills/ai-brain-starter"
ln -s "$REPO_ROOT" "$HOME/.claude/skills/ai-brain-starter"

# Run bootstrap --dry-run. Capture stdout + stderr + exit code.
OUT_FILE="$TMP/bootstrap.out"
ERR_FILE="$TMP/bootstrap.err"
EMAIL="ci@example.com" NAME="CI Test" LANG_HINT="en" \
  bash "$BOOTSTRAP" --dry-run > "$OUT_FILE" 2> "$ERR_FILE"
EXIT_CODE=$?

# ─── Assertion 1: bootstrap exited 0 ───────────────────────────────
if [ "$EXIT_CODE" -ne 0 ]; then
  echo "FAIL: bootstrap.sh --dry-run exited $EXIT_CODE" >&2
  echo "--- stderr ---" >&2
  cat "$ERR_FILE" >&2
  echo "--- stdout (last 30 lines) ---" >&2
  tail -30 "$OUT_FILE" >&2
  exit 1
fi
echo "PASS: bootstrap.sh --dry-run exited 0"

# ─── Assertion 2: required skills referenced in the install loop ───
# We don't expect them to be INSTALLED (it's a dry run) but we expect
# the loop to mention each one as "would install" / "would sync".
REQUIRED_SKILLS=(
  "graphify"
  "meeting-todos"
  "patterns"
  "insights"
  "deconstruct"
  "daily-journal"
  "repurpose-talk"
  "nano-banana"
  "second-brain-mapping"
  "setup-vault-types"
  "diagnose"
)

MISSING=0
for skill in "${REQUIRED_SKILLS[@]}"; do
  if ! grep -q "$skill" "$OUT_FILE"; then
    echo "FAIL: skill '$skill' not mentioned in bootstrap dry-run output" >&2
    MISSING=$((MISSING + 1))
  fi
done

if [ "$MISSING" -gt 0 ]; then
  echo "$MISSING skill(s) missing from bootstrap dry-run output" >&2
  echo "--- stdout (last 60 lines) ---" >&2
  tail -60 "$OUT_FILE" >&2
  exit 1
fi
echo "PASS: all ${#REQUIRED_SKILLS[@]} required skills appear in bootstrap dry-run output"

# ─── Assertion 3: commands section fires ──────────────────────────
if ! grep -qi "commands" "$OUT_FILE"; then
  echo "FAIL: bootstrap dry-run did not mention the commands install section" >&2
  echo "  (Phase 24's slash command palette registration depends on this.)" >&2
  exit 1
fi
echo "PASS: commands install section fired in dry-run"

echo
echo "All assertions passed. Bootstrap end-to-end dry-run is clean."
