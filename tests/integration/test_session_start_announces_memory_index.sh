#!/usr/bin/env bash
# Fresh-install activation proof for the memory-index announcement.
#
# The bug this closes is ARTIFACT-WITHOUT-ACTIVATION: logic can sit in the repo
# fully tested and green while nothing installed ever runs it, so every install
# gets the code and none gets the behavior. File presence is NOT the assertion.
# The assertion is that the command the INSTALLER wires into settings.json, run
# verbatim, actually announces an unreachable memory.
#
# Asserts, against a sandboxed HOME:
#   0. NEGATIVE CONTROL: pre-install settings.json has no session-start loader.
#   1. The loader is registered on SessionStart by the real installer.
#   2. The registered command names a script that exists.
#   3. END-TO-END: that command, run verbatim against a memory dir with an
#      unreachable memo, announces it.                      <- activation works
#   4. END-TO-END negative control: healthy dir -> no announcement.
#   5. The announcement rides the SAME payload as the session context, so it
#      costs no extra SessionStart cold start (that event is at fan-out budget).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"

# A REAL interpreter, resolved absolutely. Bare `python3` may be a refuse-shim
# that exit-1s on every invocation, failing this test for an unrelated reason.
PY=""
for c in /opt/homebrew/bin/python3 /usr/bin/python3 /usr/local/bin/python3; do
  [ -x "$c" ] && "$c" -c 'import sys' >/dev/null 2>&1 && { PY="$c"; break; }
done
if [ -z "$PY" ]; then
  PY="$(command -v python3 || true)"
  [ -n "$PY" ] && ! "$PY" -c 'import sys' >/dev/null 2>&1 && PY=""
fi
[ -z "$PY" ] && { echo "SKIP: no usable python3"; exit 0; }

INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"
LOADER_NAME="session-start-context.py"
LOADER="$REPO_ROOT/hooks/$LOADER_NAME"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SETTINGS="$TMP/.claude/settings.json"
mkdir -p "$TMP/.claude"

# A real install has the repo at ~/.claude/skills/ai-brain-starter. Seed it so
# the path baked into the wired command RESOLVES. Without this, running the
# repo copy instead would prove nothing about the installed wiring — a stale or
# missing installed copy would stay invisible, which is the exact
# ARTIFACT-WITHOUT-ACTIVATION class this file exists to close.
mkdir -p "$TMP/.claude/skills"
ln -s "$REPO_ROOT" "$TMP/.claude/skills/ai-brain-starter"

fail() { echo "FAIL: $*" >&2; exit 1; }

# --- 0. negative control -------------------------------------------------------
printf '{"hooks":{}}' > "$SETTINGS"
grep -q "$LOADER_NAME" "$SETTINGS" && fail "negative control broken: loader present pre-install"
echo "PASS  0. pre-install settings.json has no session-start loader"

run_sandboxed "$TMP" env -u CLAUDECODE "$PY" "$INSTALLER" \
  --hooks-source "$REPO_ROOT/hooks.json" --settings "$SETTINGS" --quiet

# --- 1. registered on SessionStart --------------------------------------------
EVENTS="$("$PY" - "$SETTINGS" "$LOADER_NAME" <<'PYEOF'
import json, sys
settings, needle = sys.argv[1], sys.argv[2]
for event, blocks in json.load(open(settings)).get("hooks", {}).items():
    for blk in blocks:
        for e in blk.get("hooks", []):
            if needle in e.get("command", ""):
                print(event)
PYEOF
)"
echo "$EVENTS" | grep -qx "SessionStart" \
  || fail "loader not registered on SessionStart (found: ${EVENTS:-none})"
echo "PASS  1. loader registered on SessionStart"

# --- 2. the registered command names a real script ----------------------------
[ -f "$LOADER" ] || fail "registered loader missing from the repo: $LOADER"
echo "PASS  2. registered command names an existing script"

# --- 3. END-TO-END: announces an unreachable memo -----------------------------
MEM="$TMP/memory"; mkdir -p "$MEM"
printf -- '---\nname: ghost\n---\n\nbody\n' > "$MEM/ghost.md"
printf -- '# Index\n\n' > "$MEM/MEMORY.md"
# Run the command string the INSTALLER WROTE, verbatim, with HOME pointed at
# the sandbox so `~` resolves to the seeded install. Not the repo copy.
WIRED="$("$PY" - "$SETTINGS" "$LOADER_NAME" <<'PYEOF'
import json, sys
settings, needle = sys.argv[1], sys.argv[2]
for blocks in json.load(open(settings)).get("hooks", {}).values():
    for blk in blocks:
        for e in blk.get("hooks", []):
            if needle in e.get("command", ""):
                print(e["command"]); raise SystemExit
PYEOF
)"
[ -n "$WIRED" ] || fail "could not read the wired command out of settings.json"
OUT="$(cd "$TMP" && run_sandboxed "$TMP" env AGENT_MEMORY_DIR="$MEM" bash -c "$WIRED" <<< '{}')"
printf '%s' "$OUT" | grep -q "ghost.md" \
  || fail "the WIRED command did not announce the unreachable memo. cmd: $WIRED | stdout: ${OUT:0:300}"
printf '%s' "$OUT" | grep -q "memory-index" \
  || fail "announcement missing its [memory-index] tag"
echo "PASS  3. end-to-end: the WIRED command announces an unreachable memo"

# --- 4. END-TO-END negative control -------------------------------------------
MEM2="$TMP/memory-ok"; mkdir -p "$MEM2"
printf -- '---\nname: alpha\n---\n\nbody\n' > "$MEM2/alpha.md"
printf -- '# Index\n\n- [Alpha](alpha.md) - hook\n' > "$MEM2/MEMORY.md"
OUT2="$(cd "$TMP" && run_sandboxed "$TMP" env AGENT_MEMORY_DIR="$MEM2" bash -c "$WIRED" <<< '{}')"
printf '%s' "$OUT2" | grep -q "memory-index" \
  && fail "false positive: healthy memory dir produced an announcement"
echo "PASS  4. end-to-end negative control: healthy dir is silent"

# --- 5. no extra SessionStart cold start --------------------------------------
COUNT="$("$PY" - "$REPO_ROOT/hooks.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
c = d.get("hooks", d)
print(sum(1 for b in c.get("SessionStart", []) for e in b.get("hooks", [])
          if "memory-index" in e.get("command", "")
          or "memory_index" in e.get("command", "")))
PYEOF
)"
[ "$COUNT" = "0" ] || fail "memory-index got its own SessionStart hook (count=$COUNT); it must ride the loader"
echo "PASS  5. announcement adds no SessionStart cold start"

echo "OK  all 6 assertions passed"
