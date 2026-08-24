#!/usr/bin/env bash
# Fresh-install smoke for the never-wired MCP secret guards (the
# block-claude-mcp-inline-secret.py / block-mcp-config-inline-secret.py pair).
#
# THE BUG CLASS THIS CLOSES: ARTIFACT-WITHOUT-ACTIVATION. Both guards were
# written after three real GitHub PAT leaks and shipped as working files --
# referenced nowhere (not hooks.json, not a phase doc, not a settings
# template) -- so they never once fired on any install. File presence is
# therefore NOT the assertion; registration in the installed settings.json
# is, plus proof the registered command actually BLOCKS a seeded secret.
#
# Asserts, by running the REAL installer against a sandboxed HOME, for EACH
# guard:
#   0. NEGATIVE CONTROL: a pre-install settings.json does not have the guard.
#   1. The guard is registered on PreToolUse after install.
#   2. The registered script EXISTS at the path the command names.
#   3. The command uses the `if [ -f ]` form, NOT `2>/dev/null || echo <allow>`.
#      A blocking hook wired the second way has its stderr discarded and its
#      exit-2 converted into an allow -- registered, running, and inert.
#   4. END-TO-END: the registered command, run verbatim against a payload
#      carrying an obviously-fake secret, BLOCKS (rc=2). <- activation works
#   5. END-TO-END negative control: the same command against a clean payload
#      does NOT block (rc=0).                            <- no false block
#   6. Idempotent: a second install does not duplicate either entry.
#
# Stdlib python3 + bash. No network. Tmpdir removed on exit.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"

# HOME alone does not sandbox `~` on Windows (Python resolves expanduser("~")
# through USERPROFILE), so a bare `HOME=$TMP installer` would run the REAL
# installer against the developer's REAL ~/.claude. This suite runs a real
# installer twice.
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

# A real install has the repo at ~/.claude/skills/ai-brain-starter. Seed it so
# the paths baked into the hook commands resolve -- that is what makes step
# 4/5 genuine end-to-end activation proofs rather than string matches.
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

GUARDS="block-claude-mcp-inline-secret.py block-mcp-config-inline-secret.py"

echo "=== 0. NEGATIVE CONTROL: guards absent before install ==="
for g in $GUARDS; do
  if [ -z "$(registered_entries "$g")" ]; then
    ok "0. $g not registered pre-install (control holds)"
  else
    bad "0. pre-install control ($g)" "already present -- the test cannot prove activation"
  fi
done

echo "=== run the REAL installer against a sandboxed HOME ==="
run_sandboxed "$TMP" "$PY" "$INSTALLER" --quiet >/dev/null 2>&1
inst_rc=$?
if [ "$inst_rc" -ne 0 ]; then
  # Non-fatal on its own: assert on the OUTCOME below, not the exit code.
  echo "note: installer exited $inst_rc (asserting on resulting settings.json)"
fi

# Run the REGISTERED command verbatim, with [PYTHON] resolved as the installer
# does and HOME pointed at the sandbox so `~` expands into it.
run_registered() {  # run_registered GUARD PAYLOAD_JSON
  local cmd
  cmd="$(registered_entries "$1" | head -1 | cut -f2-)"
  printf '%s' "$2" | run_sandboxed "$TMP" bash -c "${cmd//\[PYTHON\]/$PY}" >/dev/null 2>&1
  echo $?
}

# check_guard GUARD BLOCK_PAYLOAD CLEAN_PAYLOAD -- assertions 1-5 for one guard.
check_guard() {
  local guard="$1" block_payload="$2" clean_payload="$3" entries cmd rc

  entries="$(registered_entries "$guard")"

  echo "--- 1. $guard registered on PreToolUse ---"
  if printf '%s\n' "$entries" | grep -q '^PreToolUse'; then
    ok "1. $guard registered on PreToolUse"
  else
    bad "1. registration ($guard)" "not registered on PreToolUse after a real install: ${entries:-<none>}"
  fi

  echo "--- 2. $guard present at the installed path ---"
  if [ -f "$TMP/.claude/skills/ai-brain-starter/hooks/$guard" ]; then
    ok "2. $guard present at the installed path"
  else
    bad "2. script path ($guard)" "command names a script that is not on disk"
  fi

  cmd="$(printf '%s\n' "$entries" | head -1 | cut -f2-)"
  echo "--- 3. $guard wired in the block-preserving form ---"
  if printf '%s' "$cmd" | grep -q 'if \[ -f ' && ! printf '%s' "$cmd" | grep -q '2>/dev/null ||'; then
    ok "3. $guard uses the \`if [ -f ]\` form (exit 2 + stderr survive)"
  else
    bad "3. wiring form ($guard)" "a blocking hook wired with '2>/dev/null || echo allow' is inert: $cmd"
  fi

  echo "--- 4. END-TO-END: $guard blocks a seeded secret ---"
  rc="$(run_registered "$guard" "$block_payload")"
  if [ "$rc" = "2" ]; then
    ok "4. $guard BLOCKS a seeded secret (rc=2)"
  else
    bad "4. end-to-end block ($guard)" "registered command returned rc=$rc, expected 2"
  fi

  echo "--- 5. END-TO-END negative control: $guard allows a clean payload ---"
  rc="$(run_registered "$guard" "$clean_payload")"
  if [ "$rc" = "0" ]; then
    ok "5. $guard ALLOWS a clean payload (rc=0)"
  else
    bad "5. end-to-end allow ($guard)" "registered command returned rc=$rc on a clean payload, expected 0"
  fi
}

# --- block-claude-mcp-inline-secret.py (PreToolUse Bash) -------------------
# FAKE_TOKEN is shaped like a GitHub classic PAT but is obviously not one --
# 36 'x' characters, never a real-looking value.
FAKE_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
check_guard "block-claude-mcp-inline-secret.py" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"claude mcp add demo --env GITHUB_TOKEN=${FAKE_TOKEN}\"}}" \
  '{"tool_name":"Bash","tool_input":{"command":"claude mcp add demo --env FOO=bar"}}'

# --- block-mcp-config-inline-secret.py (PreToolUse Write|Edit|MultiEdit) ---
CFG_PATH="$TMP/.claude.json"
BLOCK_WRITE_PAYLOAD="$("$PY" - "$CFG_PATH" "$FAKE_TOKEN" <<'PY'
import json, sys
path, token = sys.argv[1], sys.argv[2]
content = json.dumps({"mcpServers": {"demo": {"env": {"TOKEN": token}}}})
print(json.dumps({"tool_name": "Write", "tool_input": {"file_path": path, "content": content}}))
PY
)"
CLEAN_WRITE_PAYLOAD="$("$PY" - "$CFG_PATH" <<'PY'
import json, sys
path = sys.argv[1]
content = json.dumps({"mcpServers": {"demo": {"env": {"TOKEN": "use-keychain-instead"}}}})
print(json.dumps({"tool_name": "Write", "tool_input": {"file_path": path, "content": content}}))
PY
)"
check_guard "block-mcp-config-inline-secret.py" "$BLOCK_WRITE_PAYLOAD" "$CLEAN_WRITE_PAYLOAD"

echo "=== 6. idempotent: a second install does not duplicate ==="
for g in $GUARDS; do
  before="$(registered_entries "$g" | wc -l | tr -d ' ')"
  run_sandboxed "$TMP" "$PY" "$INSTALLER" --quiet >/dev/null 2>&1
  after="$(registered_entries "$g" | wc -l | tr -d ' ')"
  if [ "$before" = "$after" ]; then
    ok "6. $g: second install did not duplicate ($after)"
  else
    bad "6. idempotency ($g)" "entries went $before -> $after"
  fi
done

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
