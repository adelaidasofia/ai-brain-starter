#!/usr/bin/env bash
# Regression test for the session-coordination guard hooks:
#   hooks/check-cd-outside-worktree.py   (blocks cd into main from a worktree)
#   hooks/check-py-import-precommit.py   (blocks committing F821 / missing imports)
#   hooks/session-lock.py                (warns on SessionStart + PreToolUse-gates
#                                         git-mutating ops when a sibling is live)
#
# Fails on revert: if any hook's logic, bypass env, or fail-open behavior
# regresses, an assertion below flips and the script exits non-zero.
#
# Deterministic with or without ruff on PATH: the F821 block assertion is
# conditional (ruff present -> must block; absent -> must fail-open + nudge).
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOKS="$REPO_ROOT/hooks"
CD_HOOK="$HOOKS/check-cd-outside-worktree.py"
PY_HOOK="$HOOKS/check-py-import-precommit.py"
LOCK_HOOK="$HOOKS/session-lock.py"

PASS=0
FAIL=0
TMPDIRS=()
cleanup() { for d in "${TMPDIRS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done; }
trap cleanup EXIT

# run_hook <hook.py> <json-payload> [env-assignments...] ; sets RC + OUT
run_hook() {
  local hook="$1"; local json="$2"; shift 2
  OUT="$(printf '%s' "$json" | env "$@" python3 "$hook" 2>/tmp/_sc_err.$$)"
  RC=$?
  ERR="$(cat /tmp/_sc_err.$$ 2>/dev/null)"; rm -f /tmp/_sc_err.$$
  return 0
}
ok()  { PASS=$((PASS+1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL  $1 :: $2"; }
assert_rc() { [ "$RC" = "$2" ] && ok "$1" || bad "$1" "rc=$RC want $2 (err=${ERR:0:80})"; }

newrepo() { local d; d="$(mktemp -d)"; TMPDIRS+=("$d"); ( cd "$d" && git init -q && git config user.email t@t.co && git config user.name t ); echo "$d"; }

echo "=== precondition: hooks exist ==="
for h in "$CD_HOOK" "$PY_HOOK" "$LOCK_HOOK"; do
  [ -f "$h" ] && ok "exists: $(basename "$h")" || bad "exists: $(basename "$h")" "missing"
done

echo "=== check-cd-outside-worktree.py ==="
WT="/tmp/sc-test-repo/.claude/worktrees/test-slug"
MAIN="/tmp/sc-test-repo"
run_hook "$CD_HOOK" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cd $MAIN\"},\"cwd\":\"$WT\"}"
assert_rc "CD blocks bare cd into main from worktree" 2
run_hook "$CD_HOOK" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cd $WT/src\"},\"cwd\":\"$WT\"}"
assert_rc "CD allows cd within worktree" 0
run_hook "$CD_HOOK" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cd /etc\"},\"cwd\":\"/tmp/not-a-worktree\"}"
assert_rc "CD allows when session not in a worktree" 0
run_hook "$CD_HOOK" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cd $MAIN\"},\"cwd\":\"$WT\"}" WORKTREE_CD_BYPASS=1
assert_rc "CD bypass env disables block" 0
run_hook "$CD_HOOK" "{ not json"
assert_rc "CD fail-open on malformed json" 0
run_hook "$CD_HOOK" "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$MAIN\"},\"cwd\":\"$WT\"}"
assert_rc "CD ignores non-Bash tool" 0

echo "=== check-py-import-precommit.py ==="
REPO="$(newrepo)"
printf 'def go():\n    return undefined_name_xyz + 1\n' > "$REPO/bad.py"
( cd "$REPO" && git add bad.py )
run_hook "$PY_HOOK" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m x\"},\"cwd\":\"$REPO\"}"
if command -v ruff >/dev/null 2>&1; then
  assert_rc "PY blocks commit with F821 (ruff present)" 2
else
  assert_rc "PY fail-open when ruff absent" 0
fi
REPO2="$(newrepo)"
printf 'import os\n\n\ndef go():\n    return os.getcwd()\n' > "$REPO2/good.py"
( cd "$REPO2" && git add good.py )
run_hook "$PY_HOOK" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m x\"},\"cwd\":\"$REPO2\"}"
assert_rc "PY allows clean commit" 0
run_hook "$PY_HOOK" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git status\"},\"cwd\":\"$REPO\"}"
assert_rc "PY ignores non-commit command" 0
run_hook "$PY_HOOK" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m x\"},\"cwd\":\"$REPO\"}" PRECOMMIT_F821_BYPASS=1
assert_rc "PY bypass env disables block" 0
# Inline `VAR=1 <cmd>` prefix: os.environ can't see this, only the command
# string can (HOOK-READS-SESSION-ENV-NOT-COMMAND-ENV). The prefix lives in
# tool_input.command, not in the session env passed to run_hook.
run_hook "$PY_HOOK" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"PRECOMMIT_F821_BYPASS=1 git commit -m x\"},\"cwd\":\"$REPO\"}"
if command -v ruff >/dev/null 2>&1; then
  assert_rc "PY inline-prefix bypass disables block (ruff present)" 0
else
  assert_rc "PY inline-prefix bypass (ruff absent, already fail-open)" 0
fi

echo "=== session-lock.py (SessionStart + heartbeat) ==="
# Isolate the per-session cache pointer (CACHE_DIR resolves under $HOME) so this
# section never pollutes the real ~/.claude cache and the PreToolUse layer below
# resolves the repo deterministically from the cached pointer.
LHOME="$(mktemp -d)"; TMPDIRS+=("$LHOME")
LREPO="$(newrepo)"
LOCKFILE="$LREPO/.claude/.session-lock.json"
run_hook "$LOCK_HOOK" "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"sess-1\",\"cwd\":\"$LREPO\"}" HOME="$LHOME"
assert_rc "LOCK SessionStart exits 0" 0
printf '%s' "$OUT" | python3 -c 'import json,sys; json.loads(sys.stdin.read())' 2>/dev/null \
  && ok "LOCK SessionStart emits valid JSON" || bad "LOCK SessionStart emits valid JSON" "out=${OUT:0:80}"
[ -f "$LOCKFILE" ] && ok "LOCK writes lock file" || bad "LOCK writes lock file" "absent"
python3 -c "import json; d=json.load(open('$LOCKFILE')); assert d.get('version')==2 and 'sess-1' in d.get('sessions',{})" 2>/dev/null \
  && ok "LOCK records session in v2 sessions map" || bad "LOCK records session in v2 sessions map" "shape"
run_hook "$LOCK_HOOK" "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"sess-2\",\"cwd\":\"$LREPO\"}" HOME="$LHOME"
case "$OUT" in *session-lock*sess-1*) ok "LOCK second session warns about sess-1" ;; *) bad "LOCK second session warns about sess-1" "out=${OUT:0:100}" ;; esac
run_hook "$LOCK_HOOK" "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"sess-3\",\"cwd\":\"$LREPO\"}" HOME="$LHOME" SIBLING_SESSION_LOCK_BYPASS=1
case "$OUT" in *session-lock*) bad "LOCK bypass suppresses warn" "warned" ;; *) ok "LOCK bypass suppresses warn" ;; esac
PLAIN="$(mktemp -d)"; TMPDIRS+=("$PLAIN")
run_hook "$LOCK_HOOK" "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"sx\",\"cwd\":\"$PLAIN\"}" HOME="$LHOME"
{ [ ! -f "$PLAIN/.claude/.session-lock.json" ] && [ "$RC" = 0 ]; } \
  && ok "LOCK no lock in non-git cwd" || bad "LOCK no lock in non-git cwd" "rc=$RC"
run_hook "$LOCK_HOOK" "}{not json" HOME="$LHOME"
{ [ "$RC" = 0 ] && case "$OUT" in *continue*) true;; *) false;; esac; } \
  && ok "LOCK fail-open on malformed json" || bad "LOCK fail-open on malformed json" "rc=$RC out=${OUT:0:60}"

echo "=== session-lock.py PreToolUse enforcement ==="
# The full resolver matrix (read-only vs mutating, -C/--work-tree/--git-dir
# cross-repo redirects, cd-tracking, $VAR, coarse multi-line -m) lives in the
# Python unit suite; run it here so CI gates it.
if python3 "$HOOKS/test_session_lock_enforcement.py" >/tmp/_sl_unit.$$ 2>&1; then
  ok "LOCK resolver unit suite (hooks/test_session_lock_enforcement.py)"
else
  bad "LOCK resolver unit suite" "$(tail -3 /tmp/_sl_unit.$$ | tr '\n' ' ')"
fi
rm -f /tmp/_sl_unit.$$

# End-to-end exit codes with a LIVE sibling. PTHOME isolates the cache pointer.
PTHOME="$(mktemp -d)"; TMPDIRS+=("$PTHOME")
# Physical path: git rev-parse (which sets main_root via the cached pointer)
# canonicalizes symlinked tmp paths (/var -> /private/var on macOS); the payload
# cwd must match it so the home-repo check resolves (a no-op on real /Users repos).
EREPO="$(newrepo)"; EREPO="$(cd "$EREPO" && pwd -P)"
OREPO="$(mktemp -d)"; TMPDIRS+=("$OREPO")   # a DIFFERENT repo, for cross-repo redirects
# sibling A + me B both register in EREPO (B's SessionStart caches its lock pointer).
run_hook "$LOCK_HOOK" "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"sib-A\",\"cwd\":\"$EREPO\"}" HOME="$PTHOME"
run_hook "$LOCK_HOOK" "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"me-B\",\"cwd\":\"$EREPO\"}" HOME="$PTHOME"
# pt <command> [extra-env...] : me-B issues a PreToolUse(Bash) for <command>.
pt() { run_hook "$LOCK_HOOK" "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"session_id\":\"me-B\",\"cwd\":\"$EREPO\",\"tool_input\":{\"command\":\"$1\"}}" HOME="$PTHOME" "${@:2}"; }
# 1. git commit in home + live sibling -> BLOCK (every time).
pt "git commit -m x"
assert_rc "LOCK PreToolUse blocks home git commit (live sibling)" 2
case "$ERR" in *BLOCKED*) ok "LOCK block message is BLOCKED" ;; *) bad "LOCK block message is BLOCKED" "err=${ERR:0:80}" ;; esac
# 2. same commit + bypass -> allow.
pt "git commit -m x" SIBLING_SESSION_LOCK_BYPASS=1
assert_rc "LOCK PreToolUse bypass lets commit through" 0
# 2b. same commit, inline `VAR=1 <cmd>` prefix instead of session env -> allow.
# os.environ can't see this prefix (HOOK-READS-SESSION-ENV-NOT-COMMAND-ENV);
# it only lives in tool_input.command, which `pt`'s $1 becomes verbatim.
pt "SIBLING_SESSION_LOCK_BYPASS=1 git commit -m x"
assert_rc "LOCK PreToolUse inline-prefix bypass lets commit through" 0
# 3. non-git command -> one-time HEADS-UP (exit 2), sets the warned marker.
pt "ls -la"
assert_rc "LOCK PreToolUse heads-up once on read-only (exit 2)" 2
case "$ERR" in *HEADS-UP*) ok "LOCK heads-up message is HEADS-UP" ;; *) bad "LOCK heads-up message is HEADS-UP" "err=${ERR:0:80}" ;; esac
# 4. same non-git again -> quiet (marker present).
pt "ls -la"
assert_rc "LOCK PreToolUse heads-up fires only once (exit 0)" 0
# 5. cross-repo --git-dir commit -> the git-dir (collision surface) is elsewhere -> allow.
pt "git --git-dir=$OREPO/.git commit -m x"
assert_rc "LOCK PreToolUse no false-block on --git-dir cross-repo" 0
# 5b. --work-tree=/other while the git-dir is still HOME -> still a home mutation -> BLOCK.
#     (--work-tree relocates only the working tree; the home HEAD/index still get written.)
pt "git --work-tree=$OREPO commit -m x"
assert_rc "LOCK PreToolUse blocks --work-tree relocate with home git-dir" 2
# 6. read-only git in home -> allow (marker present).
pt "git status"
assert_rc "LOCK PreToolUse allows read-only git in home" 0
# 7. non-Bash tool -> ignored.
run_hook "$LOCK_HOOK" "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Edit\",\"session_id\":\"me-B\",\"cwd\":\"$EREPO\",\"tool_input\":{\"file_path\":\"$EREPO/x\"}}" HOME="$PTHOME"
assert_rc "LOCK PreToolUse ignores non-Bash tool" 0
# 8. no live sibling -> git commit allowed.
SREPO="$(newrepo)"; SREPO="$(cd "$SREPO" && pwd -P)"
run_hook "$LOCK_HOOK" "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"solo\",\"cwd\":\"$SREPO\"}" HOME="$PTHOME"
run_hook "$LOCK_HOOK" "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"session_id\":\"solo\",\"cwd\":\"$SREPO\",\"tool_input\":{\"command\":\"git commit -m x\"}}" HOME="$PTHOME"
assert_rc "LOCK PreToolUse allows commit with no live sibling" 0

echo ""
echo "=== SUMMARY: $PASS passed, $FAIL failed ==="
[ "$FAIL" = 0 ]
