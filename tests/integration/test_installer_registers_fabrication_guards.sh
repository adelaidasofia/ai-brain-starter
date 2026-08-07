#!/usr/bin/env bash
# Fresh-install smoke for the anti-fabrication guard family (MYC-1017).
#
# The bug this closes is ARTIFACT-WITHOUT-ACTIVATION: the guards shipped into
# the repo as FILES for months while `scripts/install-hooks-user-level.py` never
# registered them, so every install got the files and none got the behavior.
# File presence is therefore NOT the assertion — registration in the installed
# settings.json is, plus proof the registered command actually executes.
#
# Asserts, by running the REAL installer against a sandboxed HOME:
#   0. NEGATIVE CONTROL: a pre-install settings.json has NONE of the guards.
#   1. check-fabricated-verification.py registered on Stop.
#   2. check-fabricated-hook-attribution.py registered on Stop.
#   3. warn-chained-state-command-truncated.py registered on PreToolUse.
#   4. Every registered guard script EXISTS at the path the command names.
#   5. END-TO-END: the registered Stop command, run verbatim on a transcript
#      that reproduces the incident, BLOCKS.  <- activation actually works
#   6. END-TO-END negative control: the same command on an honest transcript
#      does NOT block.  <- no false positive on the shipped wiring
#   7. Idempotent: a second install does not duplicate the entries.
#
# Stdlib python3 + bash only. No network, no git. Tmpdir removed on exit.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"

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

# A real install has the repo at ~/.claude/skills/ai-brain-starter. Seed it so
# the paths baked into the hook commands resolve — that is what makes leg 5 a
# genuine end-to-end activation proof rather than a string match.
mkdir -p "$TMP/.claude/skills"
cp -R "$REPO_ROOT" "$TMP/.claude/skills/ai-brain-starter"
SETTINGS="$TMP/.claude/settings.json"
echo '{}' > "$SETTINGS"

GUARDS=(
  check-fabricated-verification.py
  check-fabricated-hook-attribution.py
  warn-chained-state-command-truncated.py
)

# Emits the event name for each registered ai-brain-starter command containing $1.
registered_events() {
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
            if needle in e.get("command", ""):
                print(event)
PY
}

echo "=== 0. NEGATIVE CONTROL: guards absent before install ==="
pre_found=""
for g in "${GUARDS[@]}"; do
  [ -n "$(registered_events "$g")" ] && pre_found="$pre_found $g"
done
if [ -z "$pre_found" ]; then
  ok "no fabrication guard registered pre-install (control holds)"
else
  bad "pre-install control" "guards already present:$pre_found — the test cannot prove activation"
fi

# Run the REAL installer against the sandboxed HOME.
run_sandboxed "$TMP" env -u CLAUDECODE "$PY" "$INSTALLER" \
  --hooks-source "$REPO_ROOT/hooks.json" --settings "$SETTINGS" --quiet >/dev/null 2>&1

echo "=== 1-3. each guard registered on its event ==="
check_event() { # $1=script  $2=expected event
  local events
  events="$(registered_events "$1")"
  if echo "$events" | grep -qx "$2"; then
    ok "$1 registered on $2"
  else
    bad "$1 on $2" "registered events: [${events//$'\n'/,}]"
  fi
}
check_event check-fabricated-verification.py Stop
check_event check-fabricated-hook-attribution.py Stop
check_event warn-chained-state-command-truncated.py PreToolUse

echo "=== 4. every registered guard script exists on disk ==="
missing=""
for g in "${GUARDS[@]}"; do
  [ -f "$TMP/.claude/skills/ai-brain-starter/hooks/$g" ] || missing="$missing $g"
done
if [ -z "$missing" ]; then
  ok "all guard scripts present at the registered path"
else
  bad "guard scripts on disk" "missing:$missing"
fi

# Pull the EXACT command string the installer wrote, so legs 5/6 execute the
# shipped wiring (interpreter substitution, if/then/else form and all) rather
# than a hand-built invocation that could diverge from it.
STOP_CMD="$("$PY" - "$SETTINGS" <<'PY'
import json, sys
hooks = json.load(open(sys.argv[1])).get("hooks", {})
for blk in hooks.get("Stop", []):
    for e in blk.get("hooks", []):
        c = e.get("command", "")
        if "check-fabricated-verification.py" in c:
            print(c)
            break
PY
)"

mk_transcript() { # $1=outfile  $2=final assistant text  $3=bash command that ran
  "$PY" - "$1" "$2" "$3" <<'PY'
import json, sys
out, text, cmd = sys.argv[1], sys.argv[2], sys.argv[3]
with open(out, "w") as f:
    f.write(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "id": "t0", "input": {"command": cmd}}]}}) + "\n")
    f.write(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "error: hook denied gh pr create"}]}}) + "\n")
    f.write(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}}) + "\n")
PY
}

echo "=== 5. END-TO-END: shipped Stop wiring BLOCKS the incident ==="
if [ -z "$STOP_CMD" ]; then
  bad "stop command extracted" "no Stop command names check-fabricated-verification.py"
else
  mk_transcript "$TMP/incident.jsonl" \
    "All 16 fixes are committed and pushed to origin. PR #144 contains every one of them." \
    "git add . && git commit -m fix && git push origin br && gh pr create | tail -3"
  out="$(echo "{\"transcript_path\":\"$TMP/incident.jsonl\"}" \
        | run_sandboxed "$TMP" env -u CLAUDECODE bash -c "$STOP_CMD" 2>/dev/null)"
  if echo "$out" | grep -q '"decision"[[:space:]]*:[[:space:]]*"block"'; then
    ok "shipped wiring blocks a 'pushed / PR contains' claim with no remote read"
  else
    bad "shipped wiring blocks" "no block decision; got: ${out:0:200}"
  fi
fi

echo "=== 6. END-TO-END negative control: honest close is NOT blocked ==="
if [ -n "$STOP_CMD" ]; then
  mk_transcript "$TMP/honest.jsonl" \
    "Refactored the parser and added two tests. Both pass locally." \
    "pytest -q"
  out="$(echo "{\"transcript_path\":\"$TMP/honest.jsonl\"}" \
        | run_sandboxed "$TMP" env -u CLAUDECODE bash -c "$STOP_CMD" 2>/dev/null)"
  if echo "$out" | grep -q '"decision"[[:space:]]*:[[:space:]]*"block"'; then
    bad "honest close passes" "shipped wiring false-positived: ${out:0:200}"
  else
    ok "honest close passes the shipped wiring"
  fi
fi

echo "=== 7. idempotent: a second install does not duplicate ==="
run_sandboxed "$TMP" env -u CLAUDECODE "$PY" "$INSTALLER" \
  --hooks-source "$REPO_ROOT/hooks.json" --settings "$SETTINGS" --quiet >/dev/null 2>&1
dupes=""
for g in "${GUARDS[@]}"; do
  n="$(registered_events "$g" | wc -l | tr -d ' ')"
  [ "$n" -gt 1 ] && dupes="$dupes $g(x$n)"
done
if [ -z "$dupes" ]; then
  ok "no duplicate registrations after re-install"
else
  bad "idempotent" "duplicated:$dupes"
fi

echo
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
