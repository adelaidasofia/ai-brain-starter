#!/usr/bin/env bash
# Test scripts/install-hooks-user-level.py retires a removed hook from an
# EXISTING user's settings.json. This is the propagation step that actually
# un-nags people who installed before the every-session email gate was
# deleted: merge_hooks() never removes a hook gone from the template, so
# without retire_stale_hooks() the dead hook stays wired forever.
#
# Asserts:
#   1. A wired email-gate-hook.py is REMOVED on install ("1 stale hook(s)").
#   2. The replacement post-update-email-ask.py is ADDED from the template.
#   3. A SIBLING ai-brain-starter hook + the user's OWN custom hook survive
#      (negative control: retire is surgical, not a blanket wipe).
#   4. Idempotent: a second run removes 0 and does not duplicate the new hook.
#   5. No false retire: a settings.json with no retired hook removes 0.
#
# Self-contained; never writes outside its tmpdir.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"
HOOKS_SRC="$REPO_ROOT/hooks.json"

for f in "$INSTALLER" "$HOOKS_SRC"; do
  [ -f "$f" ] || { echo "ERROR: $f not found" >&2; exit 1; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
sandbox_home "$TMP"
SETTINGS="$TMP/settings.json"
CUSTOM='echo CUSTOM_HOOK_SENTINEL'
fail() { echo "FAIL: $1" >&2; exit 1; }

# An existing user with: the retired email-gate hook, a sibling ABS hook, and
# their own custom hook — all in one UserPromptSubmit group.
cat > "$SETTINGS" <<JSON
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [
        { "type": "command", "command": "python3 ~/.claude/skills/ai-brain-starter/hooks/log-skill-usage.py 2>/dev/null || echo '{}'" },
        { "type": "command", "command": "python3 ~/.claude/skills/ai-brain-starter/scripts/email-gate-hook.py 2>/dev/null || echo '{}'" },
        { "type": "command", "command": "$CUSTOM" }
      ] }
    ]
  }
}
JSON

# --- 1-3. install retires email-gate, adds replacement, preserves the rest ---
OUT="$(python3 "$INSTALLER" --hooks-source "$HOOKS_SRC" --settings "$SETTINGS" 2>&1)"
echo "$OUT" | grep -q "1 stale hook(s) removed" || { echo "$OUT"; fail "1: expected exactly 1 retired hook"; }
python3 -c "import json,sys; json.load(open('$SETTINGS'))" || fail "1: settings.json not valid JSON after write"
grep -q "email-gate-hook.py" "$SETTINGS" && fail "2: email-gate-hook.py STILL wired after install"
grep -q "post-update-email-ask.py" "$SETTINGS" || fail "2: replacement hook not added from template"
grep -q "log-skill-usage.py" "$SETTINGS" || fail "3: sibling ai-brain-starter hook was dropped"
grep -q "CUSTOM_HOOK_SENTINEL" "$SETTINGS" || fail "3: user's own custom hook was dropped"

# --- 4. idempotent: second run removes 0, no duplicate of the new hook ---
OUT2="$(python3 "$INSTALLER" --hooks-source "$HOOKS_SRC" --settings "$SETTINGS" 2>&1)"
echo "$OUT2" | grep -q "0 stale hook(s) removed" || { echo "$OUT2"; fail "4: second run should retire 0"; }
N="$(grep -c "post-update-email-ask.py" "$SETTINGS")"
[ "$N" = "1" ] || fail "4: replacement hook wired $N times (expected exactly 1)"

# --- 5. negative control: no retired hook present -> removes 0 ---
cat > "$SETTINGS" <<JSON
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [
        { "type": "command", "command": "python3 ~/.claude/skills/ai-brain-starter/scripts/post-update-email-ask.py 2>/dev/null || echo '{}'" },
        { "type": "command", "command": "$CUSTOM" }
      ] }
    ]
  }
}
JSON
OUT3="$(python3 "$INSTALLER" --hooks-source "$HOOKS_SRC" --settings "$SETTINGS" 2>&1)"
echo "$OUT3" | grep -q "0 stale hook(s) removed" || { echo "$OUT3"; fail "5: clean config must retire 0 (no false removal)"; }
grep -q "CUSTOM_HOOK_SENTINEL" "$SETTINGS" || fail "5: custom hook dropped on a clean config"

# --- 6. executable ADR-0003 invariant: the SHIPPED template never re-introduces
#        a retired every-session email gate, and keeps the gated replacement. ---
grep -q "email-gate-hook.py" "$HOOKS_SRC" && fail "6: retired email-gate-hook.py reappeared in hooks.json (ADR-0003 violated)"
grep -q "post-update-email-ask.py" "$HOOKS_SRC" || fail "6: the gated replacement hook is not wired in hooks.json"

echo "PASS: installer retires the dead email-gate hook, wires the replacement, preserves siblings + custom hooks, idempotent, no false retire, template stays gate-free (ADR-0003)"
