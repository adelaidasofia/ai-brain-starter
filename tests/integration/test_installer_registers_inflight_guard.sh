#!/usr/bin/env bash
# Fresh-install smoke for block-git-mutation-mid-operation.py (incident 2026-07-28).
#
# The bug class this closes is ARTIFACT-WITHOUT-ACTIVATION: a guard shipped as a
# FILE but never registered by scripts/install-hooks-user-level.py gets copied to
# every install and protects none of them. File presence is therefore NOT the
# assertion -- registration in the installed settings.json is, plus proof the
# registered command BLOCKS a real stalled rebase when run verbatim.
#
# Asserts, by running the REAL installer against a sandboxed HOME:
#   0. NEGATIVE CONTROL: a pre-install settings.json does not have the guard.
#   1. The guard is registered on PreToolUse after install.
#   2. The registered script EXISTS at the path the command names.
#   3. The command uses the `if [ -f ]` form, NOT `2>/dev/null || echo <allow>`.
#      A blocking hook wired the second way has its stderr discarded and its
#      exit-2 converted into an allow -- registered, running, and inert.
#   4. END-TO-END: the registered command, run verbatim against a repo with a
#      genuinely stalled rebase, BLOCKS.        <- activation actually works
#   5. END-TO-END negative control: the same command against a clean repo does
#      NOT block.                               <- no false positive when wired
#   6. Idempotent: a second install does not duplicate the entry.
#
# Stdlib python3 + bash + git. No network. Tmpdir removed on exit.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"
GUARD="block-git-mutation-mid-operation.py"

# HOME alone does not sandbox `~` on Windows: Python resolves expanduser("~")
# through USERPROFILE and ignores HOME entirely, so a bare `HOME=$TMP installer`
# runs the REAL installer against the developer's REAL ~/.claude. This suite
# runs a real installer twice, so that is not theoretical.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"

PASS=0; FAIL=0
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
ok()  { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1 :: $2"; }

# A REAL interpreter, resolved absolutely. Bare `python3` may be the
# trailofbits modern-python refuse-shim, which exit-1s on every invocation.
PY=""
for c in /opt/homebrew/bin/python3 /usr/bin/python3 /usr/local/bin/python3; do
  [ -x "$c" ] && "$c" -c 'import sys' >/dev/null 2>&1 && { PY="$c"; break; }
done
if [ -z "$PY" ]; then
  PY="$(command -v python3 || true)"
  [ -n "$PY" ] && ! "$PY" -c 'import sys' >/dev/null 2>&1 && PY=""
fi
[ -z "$PY" ] && { echo "SKIP: no usable python3"; exit 0; }
command -v git >/dev/null 2>&1 || { echo "SKIP: no git"; exit 0; }

# A real install has the repo at ~/.claude/skills/ai-brain-starter. Seed it so
# the paths baked into the hook command resolve -- that is what makes legs 4/5
# genuine end-to-end activation proofs rather than string matches.
mkdir -p "$TMP/.claude/skills"
cp -R "$REPO_ROOT" "$TMP/.claude/skills/ai-brain-starter"
SETTINGS="$TMP/.claude/settings.json"
echo '{}' > "$SETTINGS"

# Emits "<event>\t<command>" for each registered entry whose command contains $1.
registered_entries() {
  "$PY" - "$SETTINGS" "$1" <<'PY'
import json, sys
settings, needle = sys.argv[1], sys.argv[2]
try:
    hooks = json.load(open(settings)).get("hooks", {})
except Exception:
    hooks = {}
for event, blocks in hooks.items():
    for blk in blocks:
        for e in blk.get("hooks", []):
            cmd = e.get("command", "")
            if needle in cmd:
                print(event + "\t" + cmd)
PY
}

echo "=== 0. NEGATIVE CONTROL: guard absent before install ==="
if [ -z "$(registered_entries "$GUARD")" ]; then
  ok "guard not registered pre-install (control holds)"
else
  bad "pre-install control" "already present — the test cannot prove activation"
fi

echo "=== run the REAL installer against a sandboxed HOME ==="
run_sandboxed "$TMP" "$PY" "$INSTALLER" --quiet >/dev/null 2>&1
inst_rc=$?
if [ "$inst_rc" -ne 0 ]; then
  # Non-fatal on its own: assert on the OUTCOME below, not the exit code.
  echo "note: installer exited $inst_rc (asserting on resulting settings.json)"
fi

ENTRIES="$(registered_entries "$GUARD")"

echo "=== 1. registered on PreToolUse ==="
if printf '%s\n' "$ENTRIES" | grep -q '^PreToolUse'; then
  ok "1. $GUARD registered on PreToolUse"
else
  bad "1. registration" "not registered on PreToolUse after a real install: ${ENTRIES:-<none>}"
fi

echo "=== 2. the registered script exists at the path named ==="
if [ -f "$TMP/.claude/skills/ai-brain-starter/hooks/$GUARD" ]; then
  ok "2. script present at the installed path"
else
  bad "2. script path" "command names a script that is not on disk"
fi

echo "=== 3. wired in the block-preserving form ==="
CMD="$(printf '%s\n' "$ENTRIES" | head -1 | cut -f2-)"
if printf '%s' "$CMD" | grep -q 'if \[ -f ' && ! printf '%s' "$CMD" | grep -q '2>/dev/null ||'; then
  ok "3. uses the \`if [ -f ]\` form (exit 2 + stderr survive)"
else
  bad "3. wiring form" "a blocking hook wired with '2>/dev/null || echo allow' is inert: $CMD"
fi

# ---- build a repo with a genuinely stalled rebase (not a faked marker) ------
STALL="$TMP/stalled"
mkdir -p "$STALL"
git init -q "$STALL"
git -C "$STALL" config user.email t@example.com
git -C "$STALL" config user.name t
git -C "$STALL" checkout -q -B main
printf '1\n' > "$STALL/f.txt"; git -C "$STALL" add f.txt; git -C "$STALL" commit -q -m base
git -C "$STALL" checkout -q -b topic
printf 'topic\n' > "$STALL/f.txt"; git -C "$STALL" commit -q -am topic
git -C "$STALL" checkout -q main
printf 'main\n' > "$STALL/f.txt"; git -C "$STALL" commit -q -am main
git -C "$STALL" rebase topic >/dev/null 2>&1   # conflicts -> paused, HEAD detached

GD="$(git -C "$STALL" rev-parse --git-dir)"
case "$GD" in /*) ;; *) GD="$STALL/$GD" ;; esac
if [ ! -d "$GD/rebase-merge" ] && [ ! -d "$GD/rebase-apply" ]; then
  bad "setup" "could not stall a rebase; legs 4/5 would be vacuous"
else
  ok "setup: a real conflicted rebase is paused"

  CLEAN="$TMP/clean"
  mkdir -p "$CLEAN"
  git init -q "$CLEAN"
  git -C "$CLEAN" config user.email t@example.com
  git -C "$CLEAN" config user.name t
  git -C "$CLEAN" checkout -q -B main
  printf '1\n' > "$CLEAN/f.txt"; git -C "$CLEAN" add f.txt; git -C "$CLEAN" commit -q -m base

  # Run the REGISTERED command verbatim, with [PYTHON] resolved as the installer
  # does and HOME pointed at the sandbox so `~` expands into it.
  run_registered() {
    payload="$("$PY" - "$1" <<'PY'
import json, sys
print(json.dumps({"tool_name": "Bash", "cwd": sys.argv[1],
                  "tool_input": {"command": "git commit -m x"}}))
PY
)"
    printf '%s' "$payload" | run_sandboxed "$TMP" bash -c "${CMD//\[PYTHON\]/$PY}" >/dev/null 2>&1
    echo $?
  }

  echo "=== 4. END-TO-END: the registered command blocks a stalled rebase ==="
  rc="$(run_registered "$STALL")"
  if [ "$rc" = "2" ]; then
    ok "4. shipped wiring BLOCKS a commit into a stalled rebase (rc=2)"
  else
    bad "4. end-to-end block" "registered command returned rc=$rc, expected 2"
  fi

  echo "=== 5. END-TO-END negative control: clean repo is allowed ==="
  rc="$(run_registered "$CLEAN")"
  if [ "$rc" = "0" ]; then
    ok "5. shipped wiring ALLOWS a commit in a clean repo (rc=0)"
  else
    bad "5. end-to-end allow" "registered command returned rc=$rc on a clean repo, expected 0"
  fi
fi

echo "=== 6. idempotent: a second install does not duplicate ==="
before="$(registered_entries "$GUARD" | wc -l | tr -d ' ')"
run_sandboxed "$TMP" "$PY" "$INSTALLER" --quiet >/dev/null 2>&1
after="$(registered_entries "$GUARD" | wc -l | tr -d ' ')"
if [ "$before" = "$after" ]; then
  ok "6. second install did not duplicate the entry ($after)"
else
  bad "6. idempotency" "entries went $before -> $after"
fi

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
