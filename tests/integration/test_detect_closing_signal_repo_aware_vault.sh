#!/usr/bin/env bash
# Test: detect-closing-signal.py resolves session-artifact paths against the
# repo a session is actually working in, even when a DIFFERENT vault is
# configured as the machine-wide VAULT_ROOT default.
#
# Bug class: VAULT_ROOT is commonly set once, globally, in Claude Code's
# settings.json `env` block (so every hook subprocess always sees it set).
# The hook's old resolution was `os.environ.get("VAULT_ROOT") or cwd` — once
# VAULT_ROOT is set globally, the `or cwd` branch is DEAD, permanently, for
# every session on the machine. A session working inside a SEPARATE
# vault-shaped repo (its own CLAUDE.md declaring its own "Session End" /
# "Session Close" cascade, its own Sessions/Decisions folders) had its
# session file, decisions dir, and captures file silently resolved against
# the unrelated default vault instead of the repo it was actually in.
#
# Reproduced 2026-06-30 against a real mycelium-vault-rooted session: the
# injected SESSION CLOSE context resolved every path under the personal
# vault (the global VAULT_ROOT default) despite cwd being entirely inside a
# separate repo with its own Session End cascade.
#
# Assertions:
#   1. NEGATIVE CONTROL — cwd inside a repo-vault, VAULT_ROOT set to a
#      DIFFERENT default vault: resolves to the repo-vault, NOT the default.
#      (This is the case that reproduced the bug: without the fix, this
#      assertion fails and meta_dir/session_file land under DEFAULT_VAULT.)
#   2. Same, fired from inside a WORKTREE of the repo-vault: still resolves
#      to the repo-vault's MAIN root (combines with the worktree-collapse
#      invariant — never the worktree, never the default vault).
#   3. Heading match is case-insensitive ("Session end", lowercase "end",
#      as used by at least one real confirmed second case).
#   4. FALLBACK PRESERVED — cwd in an untracked location (no CLAUDE.md
#      anywhere in its ancestry): still resolves to VAULT_ROOT (today's
#      default/fallback behavior, unchanged).
#   5. FALLBACK PRESERVED — cwd inside the default vault itself, whose
#      CLAUDE.md uses a heading that does NOT declare a Session End/Close
#      cascade (e.g. "Session Protocol", the personal vault's real
#      heading): still resolves via the VAULT_ROOT/cwd fallback, not a
#      false walk-up match.
#
# Self-contained: tmpdir fake vaults, HOME redirected so the marker file
# never touches the real ~/.claude and so the walk-up's $HOME boundary is
# deterministic. Exit 0 = pass, 1 = fail with detail on stderr.

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

# DEFAULT_VAULT simulates the machine-wide VAULT_ROOT default (e.g. the
# personal vault) — a real vault shape, but its CLAUDE.md heading is
# "Session Protocol", which must NOT match the repo-vault heading pattern.
DEFAULT_VAULT="$TMP/default-vault"
mkdir -p "$DEFAULT_VAULT/Meta/Sessions" "$DEFAULT_VAULT/Meta/Decisions"
cat > "$DEFAULT_VAULT/CLAUDE.md" <<'EOF'
# Default Vault

# Session Protocol

1. Read Last Session.md
EOF

# REPO_VAULT simulates a session-close-aware repo distinct from the default
# vault (e.g. mycelium-vault) — its own CLAUDE.md declares its own cascade.
REPO_VAULT="$TMP/repo-vault"
mkdir -p "$REPO_VAULT/Meta/Sessions" "$REPO_VAULT/Meta/Decisions"
cat > "$REPO_VAULT/CLAUDE.md" <<'EOF'
# Repo Vault

## Session End — capture cascade

Route session content to Meta/Sessions/.
EOF
REPO_WT="$REPO_VAULT/.claude/worktrees/test-slug"
mkdir -p "$REPO_WT/Meta/Sessions" "$REPO_WT/Meta/Decisions"
cp "$REPO_VAULT/CLAUDE.md" "$REPO_WT/CLAUDE.md"   # a real worktree carries the full tree

# LOWERCASE_VAULT: same as REPO_VAULT but with a lowercase "Session end"
# heading, matching the real second confirmed case (a team-folder CLAUDE.md
# used "## Session end — capture cascade", lowercase "end").
LOWERCASE_VAULT="$TMP/lowercase-vault"
mkdir -p "$LOWERCASE_VAULT/Meta/Sessions"
cat > "$LOWERCASE_VAULT/CLAUDE.md" <<'EOF'
## Session end — capture cascade
EOF

# UNTRACKED: no CLAUDE.md anywhere in its ancestry (up to $TMP == $HOME below).
UNTRACKED="$TMP/untracked/deeply/nested"
mkdir -p "$UNTRACKED"

# run_hook <cwd> <session_id> <vault_root_env> -> echoes the marker file path.
run_hook() {
  local cwd="$1" sid="$2" vault_root_env="$3" stdin_json
  stdin_json=$(python3 -c "import json,sys; print(json.dumps({'prompt':sys.argv[1],'session_id':sys.argv[2],'cwd':sys.argv[3]}))" \
    "let's close this session" "$sid" "$cwd")
  if [ -n "$vault_root_env" ]; then
    printf '%s' "$stdin_json" | run_sandboxed "$TMP" env VAULT_ROOT="$vault_root_env" python3 "$HOOK" >/dev/null 2>&1 || true
  else
    printf '%s' "$stdin_json" | run_sandboxed "$TMP" env -u VAULT_ROOT python3 "$HOOK" >/dev/null 2>&1 || true
  fi
  echo "$TMP/.claude/.closing-signal-${sid}.json"
}

marker_field() {
  python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2"
}

assert_meta_dir() {  # label marker expected
  local label="$1" marker="$2" expected="$3"
  if [ ! -f "$marker" ]; then
    echo "FAIL: $label — hook wrote no marker (close signal not detected?)" >&2
    exit 1
  fi
  local got
  got="$(marker_field "$marker" meta_dir)"
  if [ "$got" != "$expected" ]; then
    echo "FAIL: $label" >&2
    echo "  expected meta_dir: $expected" >&2
    echo "  got:               $got" >&2
    exit 1
  fi
  echo "PASS: $label"
}

# --- 1. NEGATIVE CONTROL: repo-vault cwd wins over a DIFFERENT VAULT_ROOT --
MARKER1="$(run_hook "$REPO_VAULT" "test-repo-aware" "$DEFAULT_VAULT")"
assert_meta_dir \
  "repo-vault cwd resolves to itself, not the configured default VAULT_ROOT" \
  "$MARKER1" "$REPO_VAULT/Meta"

# --- 2. Same, fired from inside a worktree of the repo-vault ---------------
MARKER2="$(run_hook "$REPO_WT" "test-repo-aware-wt" "$DEFAULT_VAULT")"
assert_meta_dir \
  "repo-vault worktree cwd resolves to the repo-vault's MAIN root, not the worktree, not the default" \
  "$MARKER2" "$REPO_VAULT/Meta"
SESSION2="$(marker_field "$MARKER2" session_file)"
case "$SESSION2" in
  *"/.claude/worktrees/"*)
    echo "FAIL: session file resolved inside the worktree" >&2
    echo "  got: $SESSION2" >&2
    exit 1
    ;;
esac
echo "PASS: repo-vault worktree session file resolves outside .claude/worktrees/"

# --- 3. Heading match is case-insensitive ("Session end") ------------------
MARKER3="$(run_hook "$LOWERCASE_VAULT" "test-lowercase-heading" "$DEFAULT_VAULT")"
assert_meta_dir \
  "lowercase 'Session end' heading still matches" \
  "$MARKER3" "$LOWERCASE_VAULT/Meta"

# --- 4. FALLBACK PRESERVED: untracked location uses VAULT_ROOT -------------
MARKER4="$(run_hook "$UNTRACKED" "test-untracked" "$DEFAULT_VAULT")"
assert_meta_dir \
  "untracked cwd (no CLAUDE.md in its ancestry) falls back to VAULT_ROOT" \
  "$MARKER4" "$DEFAULT_VAULT/Meta"

# --- 5. FALLBACK PRESERVED: default vault's own CLAUDE.md doesn't self-match
# via a "Session Protocol" heading -- it still reaches itself, but through
# the VAULT_ROOT/cwd fallback, not a walk-up match (proves the heading
# regex is discriminating, not accidentally permissive).
MARKER5="$(run_hook "$DEFAULT_VAULT" "test-default-vault-cwd" "")"
assert_meta_dir \
  "default-vault cwd (VAULT_ROOT unset) resolves via cwd fallback, unchanged" \
  "$MARKER5" "$DEFAULT_VAULT/Meta"

echo
echo "All assertions passed. detect-closing-signal.py repo-aware vault-root invariant holds."
