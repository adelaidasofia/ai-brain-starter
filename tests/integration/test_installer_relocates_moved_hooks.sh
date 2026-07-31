#!/usr/bin/env bash
# CI lock for installer MOVED-hook relocation (MYC-2359).
#
# When a hook MOVES events in the template (e.g. session-start-context.py +
# inject-instinct-context.py: UserPromptSubmit -> SessionStart), merge_hooks()
# adds the new-event copy but, scoping its search per-event, never removes the
# stale OLD-event copy. Without relocate_moved_hooks() a moved hook would fire on
# BOTH events on every EXISTING install — neutering the move (the stale UPS copy
# keeps re-injecting every message, the exact cost MYC-2359 fixes).
#
# Asserts, by running the REAL installer into a throwaway $HOME seeded with the
# pre-move (UPS) wiring:
#   1. RELOCATE: after install, the moved hooks are on SessionStart ONLY (stale
#      UserPromptSubmit copy removed).
#   2. PRESERVE: the user's own non-ai-brain-starter UPS hook is untouched.
#   3. IDEMPOTENT: a second install adds nothing and keeps exactly one copy.
#
# Stdlib python3 + bash only. No network, no git. Tmpdir removed on exit.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"
# HOME alone does not sandbox the installer on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$SCRIPT_DIR/lib/sandbox_home.sh"

PASS=0; FAIL=0
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
ok()  { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1 :: $2"; }

mkdir -p "$TMP/.claude"
# Seed an EXISTING install: both hooks wired on UserPromptSubmit (the pre-MYC-2359
# world) plus a user's own hook that must survive untouched.
cat > "$TMP/.claude/settings.json" <<'JSON'
{ "hooks": { "UserPromptSubmit": [ { "hooks": [
  { "type": "command", "command": "python3 ~/.claude/skills/ai-brain-starter/hooks/session-start-context.py 2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'", "once": true },
  { "type": "command", "command": "python3 ~/.claude/skills/ai-brain-starter/hooks/inject-instinct-context.py 2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'", "once": true },
  { "type": "command", "command": "MY_OWN_USER_HOOK --keep-me" }
] } ] } }
JSON

run_sandboxed "$TMP" python3 "$INSTALLER" --hooks-source "$REPO_ROOT/hooks.json" --quiet >/dev/null 2>&1

assert_events() { # $1 settings.json  $2 basename  $3 expected-events-csv  $4 label
  got="$(python3 - "$1" "$2" <<'PY'
import json, sys
h = json.load(open(sys.argv[1]))["hooks"]
bn = sys.argv[2]
evs = [ev for ev, blocks in h.items() for blk in blocks
       for e in blk.get("hooks", []) if bn in e.get("command", "")]
print(",".join(sorted(set(evs))))
PY
)"
  if [ "$got" = "$3" ]; then ok "$4 ($got)"; else bad "$4" "got [$got] want [$3]"; fi
}

echo "=== 1. RELOCATE: moved hooks on SessionStart only (stale UPS removed) ==="
assert_events "$TMP/.claude/settings.json" "session-start-context.py"   "SessionStart" "session-start-context relocated UPS->SessionStart"
assert_events "$TMP/.claude/settings.json" "inject-instinct-context.py" "SessionStart" "inject-instinct relocated UPS->SessionStart"

echo "=== 2. PRESERVE: user's own UPS hook untouched ==="
assert_events "$TMP/.claude/settings.json" "MY_OWN_USER_HOOK" "UserPromptSubmit" "user's own hook preserved"

echo "=== 3. IDEMPOTENT: second install keeps exactly one copy ==="
run_sandboxed "$TMP" python3 "$INSTALLER" --hooks-source "$REPO_ROOT/hooks.json" --quiet >/dev/null 2>&1
dup="$(python3 - "$TMP/.claude/settings.json" <<'PY'
import json, sys
h = json.load(open(sys.argv[1]))["hooks"]
bad = []
for bn in ("session-start-context.py", "inject-instinct-context.py"):
    n = sum(1 for ev, blocks in h.items() for blk in blocks
            for e in blk.get("hooks", []) if bn in e.get("command", ""))
    if n != 1:
        bad.append(f"{bn}={n}")
print(";".join(bad))
PY
)"
if [ -z "$dup" ]; then ok "no duplication after re-install"; else bad "idempotent" "duplicated: $dup"; fi

echo
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
