#!/usr/bin/env bash
#
# Integration test: the worktree HEAD-isolation gate is ACTIVATED, and its
# wiring can actually deliver a block.
#
# Two distinct failures this covers, neither of which any per-hook logic test
# can see:
#
#   1. ARTIFACT-WITHOUT-ACTIVATION. check-cd-outside-worktree.py shipped and was
#      tested from MYC-782 onward while being absent from hooks.json and from
#      install-hooks-user-level.py's ABS_* registries. A fresh install placed the
#      file on disk and never wired it: the guard was dormant everywhere except
#      machines that had hand-wired it into ~/.claude/settings.json.
#
#   2. WIRING-DISCARDS-THE-VERDICT. This hook signals by EXIT CODE 2 and writes
#      its remediation prose to STDERR. The repo's usual command idiom is
#      `<script> 2>/dev/null || echo <allow-json>`, which would (a) swallow the
#      message and (b) turn the exit-2 block into an allow — leaving a
#      registered, deployed, passing-its-own-tests hook that can never block.
#      scripts/check-hook-emission-channel.py deliberately exempts exit-code
#      signallers, so that check cannot catch this; only asserting on the wiring
#      string plus an end-to-end run can.
#
# Every assertion drives the REAL command string from hooks.json — never a
# hardcoded copy — so editing hooks.json toward the discarding idiom fails here.
#
# Pure string-level: the hook path-matches off the payload `cwd`, so no real
# dirs or git repos are needed. Fast + hermetic.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
PY="${PYTHON:-python3}"
HOOKS_JSON="$REPO_ROOT/hooks.json"

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; exit 1; }
assert_eq() { # <label> <got> <want>
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (got '$2', want '$3')"; fi
}

# --- 1. REGISTRATION: wired in hooks.json under PreToolUse / Bash ------------
registered="$("$PY" - "$HOOKS_JSON" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
for group in doc.get("hooks", {}).get("PreToolUse", []):
    if group.get("matcher") != "Bash":
        continue
    for hook in group.get("hooks", []):
        if "check-cd-outside-worktree.py" in hook.get("command", ""):
            print("yes")
            sys.exit(0)
print("no")
PY
)"
assert_eq "wired in hooks.json under PreToolUse/Bash" "$registered" "yes"

# --- 2. OWNED by the installer (both registries) -----------------------------
owned="$("$PY" - "$REPO_ROOT" <<'PY'
import importlib.util, pathlib, sys
root = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "inst", root / "scripts" / "install-hooks-user-level.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
skill = "python3 ~/.claude/skills/ai-brain-starter/hooks/check-cd-outside-worktree.py"
hand = "/usr/bin/python3 /home/u/.claude/hooks/check-cd-outside-worktree.py"
# Owned via fingerprint (skill path) AND basename (hand-wired copy), and the two
# must dedup to ONE hook — else a re-install double-fires on machines that
# hand-wired it before registration existed.
print("yes" if mod.is_abs_owned(skill) and mod.is_abs_owned(hand)
      and mod.is_same_command(skill, hand) else "no")
PY
)"
assert_eq "installer owns it + dedups skill-path vs hand-wired copy" "$owned" "yes"

# --- 3. WIRING SHAPE: must not discard stderr or swallow the exit code -------
# The load-bearing regression guard. A future edit normalizing this entry to the
# common `2>/dev/null || echo <allow>` idiom silently disarms the gate; that edit
# must fail CI here rather than ship a mute, non-blocking guard.
cmd="$("$PY" - "$HOOKS_JSON" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
for group in doc.get("hooks", {}).get("PreToolUse", []):
    if group.get("matcher") != "Bash":
        continue
    for hook in group.get("hooks", []):
        c = hook.get("command", "")
        if "check-cd-outside-worktree.py" in c:
            print(c)
            sys.exit(0)
PY
)"
[ -n "$cmd" ] || fail "could not extract the wired command from hooks.json"
case "$cmd" in
  *2\>/dev/null*) fail "wiring discards stderr (2>/dev/null) — block message would be mute" ;;
  *) pass "wiring preserves stderr" ;;
esac
case "$cmd" in
  *"|| echo"*) fail "wiring swallows the exit code (|| echo) — exit-2 block becomes an allow" ;;
  *) pass "wiring preserves the exit-2 block" ;;
esac

# --- 4. END-TO-END through the REAL wired command ----------------------------
# Resolve the wired command against this checkout: [PYTHON] -> the interpreter,
# and the installed skill path -> this repo. What runs below is the shipped
# command string, not a reconstruction.
resolved="${cmd//\[PYTHON\]/$PY}"
resolved="${resolved//\~\/.claude\/skills\/ai-brain-starter\/hooks/$REPO_ROOT/hooks}"

main="/tmp/abs-wiring-repo-$$"
wt="$main/.claude/worktrees/slug"

# run <cwd> <command> -> echoes exit code; stderr captured to $STDERR_FILE
STDERR_FILE="$(mktemp)"
trap 'rm -f "$STDERR_FILE"' EXIT
run() {
  local cwd="$1" command="$2" payload
  payload="$("$PY" - "$cwd" "$command" <<'PY'
import json, sys
print(json.dumps({"tool_name": "Bash", "cwd": sys.argv[1],
                  "tool_input": {"command": sys.argv[2]}}))
PY
)"
  printf '%s' "$payload" \
    | env -u WORKTREE_CD_BYPASS bash -c "$resolved" >/dev/null 2>"$STDERR_FILE" \
    && echo 0 || echo $?
}

# 4a. NEGATIVE CONTROL — the thing the guard exists to catch. A guard earns
#     trust only by failing on it, THROUGH the real wiring.
assert_eq "worktree session: cd into main BLOCKS (exit 2)" "$(run "$wt" "cd $main")" 2

# 4b. the remediation prose survives the wiring (not just the exit code)
if grep -q "CONCURRENT-SESSION-HEAD-DRIFT" "$STDERR_FILE"; then
  pass "block message reaches stderr through the wiring"
else
  fail "block message lost — stderr was: $(cat "$STDERR_FILE")"
fi

# 4c. no-op for the overwhelming majority of installs: a session NOT rooted in a
#     worktree must be untouched, or registering this gate breaks every user.
assert_eq "non-worktree session: cd anywhere ALLOWS" "$(run "$main" "cd $main")" 0
assert_eq "non-worktree session: unrelated cd ALLOWS" "$(run "/tmp" "cd /usr/local")" 0

# 4d. the advertised bypass still works through the wiring
assert_eq "inline WORKTREE_CD_BYPASS=1 allows" \
  "$(run "$wt" "WORKTREE_CD_BYPASS=1 cd $main")" 0

# 4e. staying inside the worktrees subtree is fine (still HEAD-isolated)
assert_eq "cd to a sibling worktree ALLOWS" \
  "$(run "$wt" "cd $main/.claude/worktrees/other")" 0

# --- 5. MISSING-FILE FALLBACK: absent script must not wedge Bash -------------
# The `if [ -f ]` guard's else-branch. If the file is missing the hook must emit
# allow-JSON and exit 0, never a shell error that blocks every Bash call.
absent="${resolved//$REPO_ROOT\/hooks/\/nonexistent-abs-path}"
out="$(printf '{"tool_name":"Bash","cwd":"/tmp","tool_input":{"command":"cd /tmp"}}' \
  | bash -c "$absent" 2>/dev/null && echo "|rc=0" || echo "|rc=$?")"
case "$out" in
  *'"permissionDecision":"allow"'*'|rc=0') pass "missing script falls back to allow (exit 0)" ;;
  *) fail "missing-script fallback wrong: $out" ;;
esac

echo
echo "ALL PASS: worktree HEAD-isolation gate is registered, owned, and its wiring delivers the block."
