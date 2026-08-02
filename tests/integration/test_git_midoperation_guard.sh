#!/usr/bin/env bash
# Controls for CHECK A of hooks/check-cd-outside-worktree.py — the
# git-mutation-into-a-MID-OPERATION-repo gate (GIT-MUTATION-INTO-STALLED-OPERATION).
#
# Three control classes, all required:
#   POSITIVE  — blocks a real mutation into a stalled rebase (the bug).
#   ANTI-TRAP — NEVER blocks the commands that RESOLVE the stall, nor reads.
#               A guard that blocks --abort/--quit/--continue/--skip locks the
#               operator inside the state it is flagging, which is worse than
#               the bug. This class is the reason the guard is safe to ship.
#   NEGATIVE  — silent on a clean repo, a non-git command, and a non-repo dir.
#
# CHECK B (cd-out-of-worktree) has its own suites:
#   test_cd_worktree_guard_wiring.sh / test_cd_worktree_inline_bypass.sh
set -u
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/check-cd-outside-worktree.py"
[ -f "$HOOK" ] || { echo "FAIL: hook not found at $HOOK"; exit 1; }

TD=$(mktemp -d)
trap 'cd /; rm -rf "$TD"' EXIT
PASS=0; FAIL=0

check() { # <name> <expected_exit> <cwd> <command>
  local name="$1" want="$2" cwd="$3" cmd="$4" rc
  python3 -c '
import json,sys
print(json.dumps({"tool_name":"Bash","cwd":sys.argv[1],"tool_input":{"command":sys.argv[2]}}))
' "$cwd" "$cmd" | python3 "$HOOK" >/dev/null 2>&1
  rc=$?
  if [ "$rc" = "$want" ]; then
    echo "  PASS  $name"; PASS=$((PASS+1))
  else
    echo "  FAIL  $name (got exit $rc, want $want)"; FAIL=$((FAIL+1))
  fi
}

# --- fixture: a repo genuinely stalled mid-rebase ---------------------------
R="$TD/stalled"
git init -q "$R"; cd "$R" || exit 1
git config user.email t@t; git config user.name t
printf 'a\n' > f; git add f; git commit -qm base
printf 'b\n' > f; git add f; git commit -qm second
printf 'c\n' > f; git add f; git commit -qm third
BASE=$(git rev-parse --abbrev-ref HEAD)
git checkout -q -b side HEAD~2
printf 'x\n' > f; git add f; git commit -qm side-change
git rebase "$BASE" >/dev/null 2>&1   # conflicts and stops
if [ ! -d "$R/.git/rebase-merge" ] && [ ! -d "$R/.git/rebase-apply" ]; then
  echo "FAIL: fixture did not stall mid-rebase — controls would be vacuous"; exit 1
fi
echo "fixture: repo is mid-rebase (positive controls are meaningful)"

# --- fixture: a clean repo --------------------------------------------------
C="$TD/clean"
git init -q "$C"; cd "$C" || exit 1
git config user.email t@t; git config user.name t
printf 'a\n' > f; git add f; git commit -qm base

echo
echo "POSITIVE — must BLOCK (exit 2):"
check "commit into stalled rebase"          2 "$R" "git commit -m x"
check "push from stalled rebase"            2 "$R" "git push origin HEAD"
check "reset --hard in stalled rebase"      2 "$R" "git reset --hard HEAD~1"
check "git -C reaching into stalled repo"   2 "$C" "git -C $R commit -m x"
check "env-prefixed commit"                 2 "$R" "FOO=1 git commit -m x"
check "chained after a harmless command"    2 "$R" "ls && git commit -m x"

echo
echo "ANTI-TRAP — must ALLOW (exit 0); the exits are never blocked:"
check "rebase --abort"                      0 "$R" "git rebase --abort"
check "rebase --quit"                       0 "$R" "git rebase --quit"
check "rebase --continue"                   0 "$R" "git rebase --continue"
check "rebase --skip"                       0 "$R" "git rebase --skip"
check "status (read)"                       0 "$R" "git status"
check "log (read)"                          0 "$R" "git log --oneline"
check "stash list (read form of a verb)"    0 "$R" "git stash list"
check "documented bypass"                   0 "$R" "GIT_MIDOP_BYPASS=1 git commit -m x"

echo
echo "NEGATIVE — must ALLOW (exit 0):"
check "commit in a clean repo"              0 "$C" "git commit -m x"
check "push in a clean repo"                0 "$C" "git push origin main"
check "non-git command in stalled repo"     0 "$R" "echo hello"
check "not a git repo at all"               0 "$TD" "git commit -m x"

echo
echo "test_git_midoperation_guard: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ] || exit 1
