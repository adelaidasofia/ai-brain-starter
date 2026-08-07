#!/usr/bin/env bash
# Test: detect-closing-signal.py resolves session-artifact paths to the MAIN
# vault, even when the close cascade fires from inside a worktree.
#
# Bug class: the UserPromptSubmit hook set vault_root from its own cwd. Fired
# inside <vault>/.claude/worktrees/<slug>/, cwd IS the worktree, so the
# session file, decisions dir, captures and time-tracking all resolved
# worktree-side. When the worktree was archived those writes were discarded
# as "uncommitted changes" — session history silently lost.
#
# Same bug class as issue #65 (tests/integration/test_worktree_session_close.sh)
# but for the UserPromptSubmit hook, not the Stop hook. PR #66 fixed the Stop
# hook; this hook (Layer 1 of the cascade) was never brought to parity.
#
# Assertions:
#   1. Fired from a worktree cwd, the resolved meta_dir is the MAIN vault's.
#   2. The session file resolves under the MAIN vault, never .claude/worktrees/.
#   3. The pre-built session shell is physically written to the MAIN vault.
#   4. The worktree slug is still preserved in the session filename.
#   5. Fired from a normal (non-worktree) cwd, paths resolve unchanged.
#
# Self-contained: tmpdir fake vault, HOME redirected so the marker file never
# touches the real ~/.claude. Exit 0 = pass, 1 = fail with detail on stderr.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
HOOK="$REPO_ROOT/hooks/detect-closing-signal.py"
if [ ! -f "$HOOK" ]; then
  echo "ERROR: $HOOK not found" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

VAULT="$TMP/vault"
WT="$VAULT/.claude/worktrees/test-slug"
# A real worktree carries the vault's Meta/ tree (it is a git checkout of it),
# so the fixture creates Meta/ on both the main vault and the worktree side.
mkdir -p "$VAULT/Meta/Sessions" "$VAULT/Meta/Decisions"
mkdir -p "$WT/Meta/Sessions" "$WT/Meta/Decisions"

# run_hook <cwd> <session_id> -> echoes the marker file path the hook wrote.
# VAULT_ROOT is explicitly unset so the hook exercises its cwd-based default
# resolution (the path the real session-close cascade takes).
run_hook() {
  local cwd="$1" sid="$2" stdin_json
  stdin_json=$(python3 -c "import json,sys; print(json.dumps({'prompt':sys.argv[1],'session_id':sys.argv[2],'cwd':sys.argv[3]}))" \
    "let's close this session" "$sid" "$cwd")
  printf '%s' "$stdin_json" | run_sandboxed "$TMP" env -u VAULT_ROOT python3 "$HOOK" >/dev/null 2>&1 || true
  echo "$TMP/.claude/.closing-signal-${sid}.json"
}

marker_field() {
  python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2"
}

# --- Worktree case --------------------------------------------------------
MARKER_WT="$(run_hook "$WT" "test-wt")"
if [ ! -f "$MARKER_WT" ]; then
  echo "FAIL: hook wrote no marker for the worktree run (close signal not detected?)" >&2
  exit 1
fi

META_WT="$(marker_field "$MARKER_WT" meta_dir)"
SESSION_WT="$(marker_field "$MARKER_WT" session_file)"

if [ "$META_WT" != "$VAULT/Meta" ]; then
  echo "FAIL: meta_dir resolved worktree-side, not to the main vault" >&2
  echo "  expected: $VAULT/Meta" >&2
  echo "  got:      $META_WT" >&2
  exit 1
fi
echo "PASS: meta_dir resolves to the main vault from a worktree"

case "$SESSION_WT" in
  *"/.claude/worktrees/"*)
    echo "FAIL: session file resolved inside the worktree (discarded on archive)" >&2
    echo "  got: $SESSION_WT" >&2
    exit 1
    ;;
esac
case "$SESSION_WT" in
  "$VAULT/Meta/Sessions/"*) ;;
  *)
    echo "FAIL: session file is not under the main vault Meta/Sessions/" >&2
    echo "  got: $SESSION_WT" >&2
    exit 1
    ;;
esac
echo "PASS: session file resolves under the main vault, not the worktree"

if [ ! -f "$SESSION_WT" ]; then
  echo "FAIL: pre-built session shell was not written at $SESSION_WT" >&2
  exit 1
fi
echo "PASS: session shell physically written to the main vault"

case "$(basename "$SESSION_WT")" in
  *-test-slug.md) ;;
  *)
    echo "FAIL: worktree slug lost from the session filename" >&2
    echo "  got: $(basename "$SESSION_WT")" >&2
    exit 1
    ;;
esac
echo "PASS: worktree slug preserved in the session filename"

# --- Non-worktree case (no regression) ------------------------------------
MARKER_MAIN="$(run_hook "$VAULT" "test-main")"
if [ ! -f "$MARKER_MAIN" ]; then
  echo "FAIL: hook wrote no marker for the non-worktree run" >&2
  exit 1
fi

META_MAIN="$(marker_field "$MARKER_MAIN" meta_dir)"
SESSION_MAIN="$(marker_field "$MARKER_MAIN" session_file)"

if [ "$META_MAIN" != "$VAULT/Meta" ]; then
  echo "FAIL: non-worktree meta_dir resolution regressed" >&2
  echo "  expected: $VAULT/Meta" >&2
  echo "  got:      $META_MAIN" >&2
  exit 1
fi
case "$SESSION_MAIN" in
  "$VAULT/Meta/Sessions/"*) ;;
  *)
    echo "FAIL: non-worktree session file resolution regressed" >&2
    echo "  got: $SESSION_MAIN" >&2
    exit 1
    ;;
esac
echo "PASS: non-worktree paths resolve unchanged (no regression)"

echo
echo "All assertions passed. detect-closing-signal.py worktree invariant holds."
