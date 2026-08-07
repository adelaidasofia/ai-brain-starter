#!/usr/bin/env bash
# Test: verify-session-close-cascade.py resolves its vault root the SAME
# repo-aware way as detect-closing-signal.py, so a Layer-1 fix that starts
# correctly writing session artifacts into a session's own repo-vault does
# not turn into a Layer-3 false hard-block against an unrelated default
# vault's runner/session-file state.
#
# Bug class this guards: BEFORE this fix, this hook computed its own
# VAULT_ROOT independently, straight from `os.environ.get("VAULT_ROOT", ...)`
# with zero cwd-awareness. If a global VAULT_ROOT default happens to have
# the session-close-runner.sh cascade installed (a real, common case — the
# default vault is usually the one that installed it first), this hook's
# fail-safe (`runner_installed()`) would see "installed" for EVERY session
# on the machine, regardless of which repo it's actually in, and switch
# into hard-block enforcement mode using the WRONG vault's Sessions/ dir —
# which never has this worktree's session file, because it was correctly
# written into the repo-vault instead. Silent mis-filing (the bug
# detect-closing-signal.py's own repo-aware fix corrects) would turn into
# an active false block quoting a path in the wrong vault, UNLESS this
# verifier resolves to the same repo-vault.
#
# Assertions:
#   1. NEGATIVE CONTROL — repo-vault has NOT opted into the cascade (no
#      runner installed there), but the configured default VAULT_ROOT DOES
#      have one installed. A closing claim from inside the repo-vault must
#      degrade to advisory (never block) — the presence of a DIFFERENT
#      vault's runner must not leak into this repo's enforcement decision.
#   2. Repo-vault HAS its own runner installed and the cascade actually
#      ran cleanly: allows the close (proves enforcement still engages
#      correctly for a repo that legitimately opted in, using the
#      repo-vault's OWN Sessions/ dir — not the default vault's).
#   3. Repo-vault HAS its own runner installed but the cascade is
#      incomplete: BLOCKS, and the diagnostic message is emitted — proves
#      gate 1 (session file) is checked against the repo-vault, not the
#      default vault (which would show DIFFERENT missing-file evidence).
#
# Self-contained: tmpdir fake vaults, ABS_RUNNER_REPORT redirected. Exit 0 = pass.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/verify-session-close-cascade.py"
if [ ! -f "$HOOK" ]; then
  echo "ERROR: $HOOK not found" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
REPORT="$TMP/runner.report"
TODAY="$(date +%Y-%m-%d)"
CLOSING="Closing the session now — that's all for today."

run_hook() {  # VAULT_ROOT_env CWD TEXT [EXTRA_ENV=val ...] -> exit code
  local vault_root_env="$1" cwd="$2" text="$3"; shift 3
  local tpath="$TMP/transcript.jsonl"
  python3 -c "import json,sys; open(sys.argv[1],'w',encoding='utf-8').write(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':sys.argv[2]}]}})+'\n')" "$tpath" "$text"
  local stdin_json
  stdin_json=$(python3 -c "import json,sys; print(json.dumps({'cwd':sys.argv[1],'transcript_path':sys.argv[2]}))" "$cwd" "$tpath")
  set +e
  printf '%s' "$stdin_json" | env -u VERIFY_CASCADE_BYPASS -u VERIFY_CASCADE_SOFT \
    VAULT_ROOT="$vault_root_env" ABS_RUNNER_REPORT="$REPORT" "$@" \
    python3 "$HOOK" >"$TMP/out.txt" 2>"$TMP/err.txt"
  local rc=$?
  set -e
  echo "$rc"
}

assert_rc() {  # label expected actual
  if [ "$2" != "$3" ]; then
    echo "FAIL: $1 — expected exit $2, got $3" >&2
    echo "  --- stderr ---" >&2; sed 's/^/  /' "$TMP/err.txt" >&2
    exit 1
  fi
  echo "PASS: $1 (exit $3)"
}

# DEFAULT_VAULT: simulates the machine-wide VAULT_ROOT default that HAS the
# cascade installed (e.g. the personal vault, where it was built first).
DEFAULT_VAULT="$TMP/default-vault"
mkdir -p "$DEFAULT_VAULT/⚙️ Meta/scripts" "$DEFAULT_VAULT/⚙️ Meta/Sessions"
: > "$DEFAULT_VAULT/⚙️ Meta/scripts/session-close-runner.sh"

# REPO_VAULT_NORUNNER: a session-close-aware repo that has NOT installed the
# runner cascade — the common case for a repo that just adopted the pattern.
REPO_NORUN="$TMP/repo-norunner"
mkdir -p "$REPO_NORUN/⚙️ Meta/Sessions"
cat > "$REPO_NORUN/CLAUDE.md" <<'EOF'
## Session End — capture cascade
EOF
WT_NORUN="$REPO_NORUN/.claude/worktrees/slug1"
mkdir -p "$WT_NORUN"

# --- 1. NEGATIVE CONTROL: default vault's runner must not leak into a
#        repo-vault session's enforcement decision ------------------------
rm -f "$REPORT"
rc=$(run_hook "$DEFAULT_VAULT" "$WT_NORUN" "$CLOSING")
assert_rc "default vault's installed runner does not leak into repo-vault enforcement" 0 "$rc"

# --- 2. Repo-vault has ITS OWN runner + all gates pass ---------------------
REPO_PASS="$TMP/repo-pass"
mkdir -p "$REPO_PASS/⚙️ Meta/scripts" "$REPO_PASS/⚙️ Meta/Sessions"
cat > "$REPO_PASS/CLAUDE.md" <<'EOF'
## Session End — capture cascade
EOF
: > "$REPO_PASS/⚙️ Meta/scripts/session-close-runner.sh"
echo "session" > "$REPO_PASS/⚙️ Meta/Sessions/${TODAY}T120000-slugpass.md"
git -C "$REPO_PASS" init -q
git -C "$REPO_PASS" -c user.email=t@t -c user.name=t add -A
git -C "$REPO_PASS" -c user.email=t@t -c user.name=t commit -qm init
WT_PASS="$REPO_PASS/.claude/worktrees/slugpass"
mkdir -p "$WT_PASS"
printf 'RUNNER COMPLETE @ %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$REPORT"
rc=$(run_hook "$DEFAULT_VAULT" "$WT_PASS" "$CLOSING")
assert_rc "repo-vault with its own runner + all gates pass allows close (checked against the repo, not the default)" 0 "$rc"

# --- 3. Repo-vault has ITS OWN runner but the cascade is incomplete --------
REPO_INCOMPLETE="$TMP/repo-incomplete"
mkdir -p "$REPO_INCOMPLETE/⚙️ Meta/scripts" "$REPO_INCOMPLETE/⚙️ Meta/Sessions"
cat > "$REPO_INCOMPLETE/CLAUDE.md" <<'EOF'
## Session End — capture cascade
EOF
: > "$REPO_INCOMPLETE/⚙️ Meta/scripts/session-close-runner.sh"
WT_INCOMPLETE="$REPO_INCOMPLETE/.claude/worktrees/sluginc"
mkdir -p "$WT_INCOMPLETE"
rm -f "$REPORT"   # gate 2: no fresh report -> incomplete
rc=$(run_hook "$DEFAULT_VAULT" "$WT_INCOMPLETE" "$CLOSING")
assert_rc "repo-vault with its own runner + incomplete cascade blocks (evaluated against the repo)" 2 "$rc"
if ! grep -q "BLOCKED by verify-session-close-cascade" "$TMP/err.txt"; then
  echo "FAIL: block message missing from stderr" >&2; exit 1
fi
echo "PASS: block emits the diagnostic message"

echo
echo "All assertions passed. verify-session-close-cascade.py repo-aware vault-root invariant holds."
