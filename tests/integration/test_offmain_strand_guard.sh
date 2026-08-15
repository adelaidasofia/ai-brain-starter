#!/usr/bin/env bash
# Negative-control test for git-hooks/guard-session-artifacts-on-default-branch.sh
#
# Proves the guard FIRES on the failure it exists to catch (a session-close
# artifact staged while off the default branch) and does NOT over-block
# (plain code work off-branch, on-branch commits, and the bypass all pass).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
GUARD="${1:-$HERE/../../git-hooks/guard-session-artifacts-on-default-branch.sh}"
[ -f "$GUARD" ] || { echo "guard not found: $GUARD"; exit 2; }

# WHY THIS TEST SANDBOXES HOME
#
# Every case below runs `git init` and `git commit` in a throwaway repo. A
# throwaway repo is not a hermetic one: git still reads the developer's own
# global configuration, and two keys in it reach straight into this test.
#
#   core.hooksPath      Commonly set globally (it is on the maintainer's
#                       machine, pointing at ~/.claude/git-hooks). Every
#                       `git commit` below then EXECUTES the operator's real
#                       global pre-commit/post-commit hooks - arbitrary local
#                       code, run eight times per suite run, inside a test that
#                       is only supposed to exercise the guard passed as $1.
#   init.defaultBranch  Feeds the very fallback the guard under test resolves
#                       (guard line: `git config --get init.defaultBranch`).
#                       On a machine with `defaultBranch = master` the
#                       "default-branch + artifact -> ALLOW" case FAILS against
#                       a completely correct guard, because the guard computes
#                       _def=master while the case is on main. Verified by
#                       running this file under that config: 1 FAILED.
#
# So the assertions were a function of the machine, not of the guard. HOME alone
# does not sandbox ~ on Windows - see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$HERE/lib/sandbox_home.sh"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
sandbox_home "$SANDBOX"
# HOME closes ~/.gitconfig and (with XDG_CONFIG_HOME unset) ~/.config/git/config
# too. These close the two routes it does not: an exported XDG_CONFIG_HOME, and
# the system-level gitconfig. Same defect, remaining spellings.
export XDG_CONFIG_HOME="$SANDBOX/.config"
export GIT_CONFIG_NOSYSTEM=1

fails=0
pass(){ echo "PASS: $1"; }
fail(){ echo "FAIL: $1"; fails=$((fails+1)); }

# Negative control for the sandbox itself. If it ever silently stops taking
# effect, every case below quietly goes back to executing the operator's real
# global git hooks and resolving their real init.defaultBranch - and the cases
# would still pass on a machine where those happen to be benign. This is the
# only thing that would say so.
hermetic_check(){
  local d; d="$(mktemp -d)"
  (
    cd "$d" || exit 99
    git init -q -b main
    echo "core.hooksPath=$(git config --get core.hooksPath 2>/dev/null || true)"
    echo "init.defaultBranch=$(git config --get init.defaultBranch 2>/dev/null || true)"
  ) > "$d/out" 2>&1
  local leaked
  leaked="$(grep -E '^(core\.hooksPath|init\.defaultBranch)=.+' "$d/out" || true)"
  if [ -z "$leaked" ]; then
    pass "sandbox holds (no real global git config reaches the cases)"
  else
    fail "sandbox leaked real git config: $(printf '%s' "$leaked" | tr '\n' ' ')"
  fi
  rm -rf "$d"
}
hermetic_check

# name, branch, stage-path, bypass(0/1), expect_exit, extra-pattern(optional)
run_case(){
  local name="$1" branch="$2" path="$3" bypass="$4" expect="$5" extra="${6:-}"
  local d; d="$(mktemp -d)"
  (
    cd "$d" || exit 99
    git init -q -b main
    git config user.email t@t; git config user.name t
    mkdir -p .githooks
    cp "$GUARD" .githooks/guard.sh; chmod +x .githooks/guard.sh
    [ -n "$extra" ] && printf '%s\n' "$extra" > .githooks/session-artifact-paths.txt
    echo x > seed; git add seed; git commit -qm seed
    [ "$branch" != "main" ] && git checkout -q -b "$branch"
    mkdir -p "$(dirname "$path")"; echo content > "$path"; git add -- "$path"
    [ "$bypass" = "1" ] && export SESSION_ARTIFACT_BRANCH_BYPASS=1
    .githooks/guard.sh; echo "EXIT=$?"
  ) > "$d/out" 2>&1
  local got; got="$(sed -n 's/^EXIT=//p' "$d/out")"
  if [ "$got" = "$expect" ]; then pass "$name (exit $got)"; else fail "$name (got '$got' want '$expect')"; sed 's/^/    /' "$d/out"; fi
  rm -rf "$d"
}

run_case "default-branch + artifact -> ALLOW"          main      "Meta/Sessions/2026-07-07.md"   0 0
run_case "feature-branch + artifact -> BLOCK"          claude/x  "Meta/Sessions/2026-07-07.md"   0 1
run_case "feature-branch + code-only -> ALLOW"         claude/x  "src/foo.py"                     0 0
run_case "feature-branch + artifact + bypass -> ALLOW" claude/x  "Meta/Decisions/d.md"            1 0
run_case "feature-branch + aggregate -> BLOCK"         claude/x  "Meta/Last Session.md"           0 1
run_case "feature-branch + emoji-prefixed artifact -> BLOCK" claude/x "⚙️ Meta/Sessions/e.md"     0 1
run_case "feature-branch + per-repo extension -> BLOCK" claude/x "notes/Weekly Digest.md"         0 1 "Weekly Digest.md"
run_case "feature-branch + unlisted path -> ALLOW"     claude/x  "notes/random.md"                0 0

echo "---"
if [ "$fails" -eq 0 ]; then echo "ALL PASS ($((0)) failures)"; exit 0; else echo "$fails FAILED"; exit 1; fi
