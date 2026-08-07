#!/usr/bin/env bash
# Test: surface-stranded-session-artifacts.py — SessionStart watchdog that
# detects session artifacts left UNCOMMITTED inside a worktree.
#
# Bug class: when the close cascade wrote a session file / Decisions / Time
# Tracking into a worktree's own checkout, those writes sat uncommitted and
# were discarded when the worktree was archived ("N uncommitted changes that
# will be permanently discarded"). detect-closing-signal.py now resolves
# close-cascade writes to the main vault; this watchdog is the Layer 3 canary
# that surfaces anything that still slips through, at the NEXT session start.
#
# Complements surface-orphan-claude-branches.py: that hook catches the
# committed-but-unmerged half (commits on claude/* branches); this hook
# catches the uncommitted half (changes in a worktree working dir).
#
# Assertions:
#   1. Silent when every worktree is clean.
#   2. Detects an uncommitted session file in a worktree, names that worktree.
#   3. Does not flag a clean worktree.
#   4. Does not flag a session artifact once it is committed (that is the
#      sibling hook's job — this one is uncommitted-only).
#
# Self-contained: tmpdir fake vault + real git worktrees. Exit 0 = pass.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/surface-stranded-session-artifacts.py"
if [ ! -f "$HOOK" ]; then
  echo "FAIL: watchdog hook not found at $HOOK" >&2
  exit 1
fi

# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/sandbox_home.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# This suite invokes the hook directly, and the hook resolves ~/.claude. Without a
# sandbox it therefore rewrites the DEVELOPER'S real ~/.claude/settings.json on every
# run — measured here as a fresh settings.json.bak per run, 309 of them going back to
# 2026-04-16. Sandbox HOME *and* USERPROFILE (the Windows resolution path) before any
# hook runs; the harness fails the suite if the real file is touched.
sandbox_home "$TMP"

VAULT="$TMP/vault"
mkdir -p "$VAULT"
cd "$VAULT"
git init --quiet --initial-branch=master
git config user.email "test@example.com"
git config user.name "Test"

mkdir -p "Meta/Sessions" "Meta/Decisions"
echo "committed session" > "Meta/Sessions/2026-01-01T00-00-main.md"
echo "# vault" > README.md
git add -A
git commit --quiet -m "init"

# Two worktrees: one will get a stranded artifact, one stays clean.
git worktree add --quiet -b claude/wt-stranded ".claude/worktrees/wt-stranded"
git worktree add --quiet -b claude/wt-clean ".claude/worktrees/wt-clean"

# run_watchdog -> echoes the systemMessage text ("" when the hook is silent)
run_watchdog() {
  printf '{}' | python3 "$HOOK" 2>/dev/null \
    | python3 -c "import json,sys; raw=sys.stdin.read().strip(); print(json.loads(raw).get('systemMessage','') if raw else '')"
}

# --- Assertion 1: clean state -> silent ----------------------------------
cd "$VAULT"
OUT="$(run_watchdog)"
if [ -n "$OUT" ]; then
  echo "FAIL: watchdog flagged something when all worktrees are clean" >&2
  echo "  got: $OUT" >&2
  exit 1
fi
echo "PASS: watchdog silent when no artifacts are stranded"

# --- Assertion 2: strand a session file -> detected ----------------------
echo "stranded session body" \
  > "$VAULT/.claude/worktrees/wt-stranded/Meta/Sessions/2026-05-17T12-00-wt-stranded.md"
cd "$VAULT"
OUT="$(run_watchdog)"
if [ -z "$OUT" ]; then
  echo "FAIL: watchdog did not detect the uncommitted session file in wt-stranded" >&2
  exit 1
fi
case "$OUT" in
  *wt-stranded*) ;;
  *)
    echo "FAIL: watchdog message did not name the wt-stranded worktree" >&2
    echo "  got: $OUT" >&2
    exit 1
    ;;
esac
echo "PASS: watchdog detects an uncommitted session file in a worktree"

# --- Assertion 3: the clean worktree must NOT be named -------------------
case "$OUT" in
  *wt-clean*)
    echo "FAIL: watchdog falsely flagged the clean worktree" >&2
    echo "  got: $OUT" >&2
    exit 1
    ;;
esac
echo "PASS: watchdog does not flag the clean worktree"

# --- Assertion 4: a COMMITTED artifact is not flagged --------------------
# Once committed it is no longer "uncommitted" — the committed-but-unmerged
# case belongs to the sibling hook surface-orphan-claude-branches.py.
cd "$VAULT/.claude/worktrees/wt-stranded"
git add -A >/dev/null 2>&1
git commit --quiet -m "commit the session file"
cd "$VAULT"
OUT="$(run_watchdog)"
case "$OUT" in
  *wt-stranded*)
    echo "FAIL: watchdog flagged a COMMITTED session artifact (uncommitted-only scope)" >&2
    echo "  got: $OUT" >&2
    exit 1
    ;;
esac
echo "PASS: watchdog does not flag committed session artifacts"

echo
echo "All assertions passed. stranded-session-artifact watchdog invariant holds."
