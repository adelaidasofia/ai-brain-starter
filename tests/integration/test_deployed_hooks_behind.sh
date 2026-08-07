#!/usr/bin/env bash
# CI lock + negative control for the client-side deployed==committed hook drift
# surfacer (MYC-2507, hooks/surface-deployed-hooks-behind.py).
#
# WHY: "a guard earns trust only by failing on the thing it catches" (fail-loud-
# not-silent-noop). The auto-update's deploy step (install-hooks-user-level.py)
# fails OPEN: on a silent failure the checkout is current but ~/.claude/settings.json
# falls behind hooks.json, and the client gets ZERO signal. This surfacer closes
# that gap; this test proves it fires on a stale deploy and stays silent on a
# healthy one — both directions.
#
# Asserts:
#   1. NEG-CONTROL: the detector is SILENT on exactly what the REAL installer
#      produces from the REAL hooks.json (ties detection to the deploy's real
#      output — no cry-wolf, and no drift between "committed" and "deployed").
#   2. MISSING: a committed ABS hook removed from settings.json -> FIRES, names it,
#      gives the one-command fix.
#   3. RETIRED: a retired hook still wired in settings.json -> FIRES, names it.
#   4. FAIL-OPEN: missing settings.json / missing hooks.json -> SILENT (never crash).
#   5. VAULT-EXCLUSION (the key false-positive guard): a committed VAULT hook
#      absent from a no-vault deploy -> SILENT (the deploy never wires vault hooks;
#      /setup-brain does). A non-vault hook missing in the same fixture -> FIRES,
#      and it must NOT name the vault hook.
#
# Stdlib python3 + bash only. No network, no git. Tmpdir removed on exit.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
HOOK="$REPO_ROOT/hooks/surface-deployed-hooks-behind.py"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"

PASS=0; FAIL=0
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
ok()  { PASS=$((PASS + 1)); echo "  PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL  $1 :: ${2:-}"; }

# The hook now surfaces TWO things (MYC-2507 deployed-hook drift + MYC-3076
# skill-content drift), so its test must pin BOTH inputs. Default the skill-copy
# install root to an EMPTY dir so the hook-drift cases below can never be polluted
# by whatever bare skill copies happen to live in the real ~/.claude/skills.
EMPTY_INSTALL="$TMP/empty-install"; mkdir -p "$EMPTY_INSTALL"

# run_hook SKILL_DIR SETTINGS_JSON [INSTALL_DIR] — drive the detector with an
# explicit committed hooks.json source (SKILL_DIR/hooks.json), deployed
# settings.json path, and (for the skill-drift cases) a bare-copy install root.
run_hook() { OUT="$(ABS_SKILL_DIR="$1" ABS_SETTINGS_JSON="$2" ABS_SYNC_INSTALL_DIR="${3:-$EMPTY_INSTALL}" python3 "$HOOK" <<<'{}' 2>/dev/null)"; }
fired()       { printf '%s' "$OUT" | grep -q 'additionalContext'; }
mentions()    { printf '%s' "$OUT" | grep -q "$1"; }
notmentions() { ! printf '%s' "$OUT" | grep -q "$1"; }

# strip_hook FILE BASENAME — remove every hook command referencing BASENAME.
strip_hook() {
  python3 - "$1" "$2" <<'PY'
import json, sys
f, bn = sys.argv[1], sys.argv[2]
cfg = json.load(open(f))
for ev, groups in (cfg.get("hooks") or {}).items():
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if bn not in h.get("command", "")]
json.dump(cfg, open(f, "w"), indent=2)
PY
}

# inject_hook FILE EVENT COMMAND — append a hook command onto EVENT.
inject_hook() {
  python3 - "$1" "$2" "$3" <<'PY'
import json, sys
f, ev, cmd = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = json.load(open(f))
cfg.setdefault("hooks", {}).setdefault(ev, [{"hooks": []}])
cfg["hooks"][ev][0].setdefault("hooks", []).append({"type": "command", "command": cmd})
json.dump(cfg, open(f, "w"), indent=2)
PY
}

echo "=== 1. NEG-CONTROL: healthy deploy (real installer + real hooks.json) -> silent ==="
H1="$TMP/case1"; mkdir -p "$H1/.claude"
run_sandboxed "$H1" python3 "$INSTALLER" --hooks-source "$REPO_ROOT/hooks.json" \
  --settings "$H1/.claude/settings.json" --quiet >/dev/null 2>&1
run_hook "$REPO_ROOT" "$H1/.claude/settings.json"
if ! fired; then ok "silent on a real, correct deploy (no cry-wolf)"; else bad "cry-wolf on a healthy deploy" "$(printf '%s' "$OUT" | head -c 300)"; fi

echo "=== 2. MISSING: a committed ABS hook removed from settings.json -> FIRES ==="
cp "$H1/.claude/settings.json" "$TMP/stale.json"
strip_hook "$TMP/stale.json" "dev-hub-refresh-on-session-start.py"
run_hook "$REPO_ROOT" "$TMP/stale.json"
if fired && mentions 'dev-hub-refresh-on-session-start.py'; then ok "fired + named the missing hook"; else bad "missing hook not surfaced" "$(printf '%s' "$OUT" | head -c 300)"; fi
if mentions 'ai-brain-starter update check'; then ok "carries the drift headline tag"; else bad "missing headline tag"; fi
if mentions 'install-hooks-user-level.py'; then ok "carries the one-command fix"; else bad "missing fix command"; fi

echo "=== 3. RETIRED: a retired hook still wired -> FIRES ==="
cp "$H1/.claude/settings.json" "$TMP/retired.json"
inject_hook "$TMP/retired.json" "UserPromptSubmit" \
  "python3 ~/.claude/skills/ai-brain-starter/scripts/email-gate-hook.py 2>/dev/null || true"
run_hook "$REPO_ROOT" "$TMP/retired.json"
if fired && mentions 'email-gate-hook.py' && mentions 'No longer shipped'; then ok "fired + named the retired hook"; else bad "retired hook not surfaced" "$(printf '%s' "$OUT" | head -c 300)"; fi

echo "=== 4. FAIL-OPEN: missing settings.json / missing hooks.json -> silent ==="
run_hook "$REPO_ROOT" "$TMP/does-not-exist.json"
if ! fired; then ok "silent when settings.json is absent"; else bad "fired without a deployed target"; fi
mkdir -p "$TMP/empty-skill"
run_hook "$TMP/empty-skill" "$H1/.claude/settings.json"
if ! fired; then ok "silent when hooks.json is absent"; else bad "fired without a committed source"; fi

echo "=== 5. VAULT-EXCLUSION: committed vault hook absent from a no-vault deploy -> silent ==="
FX="$TMP/fx-skill"; mkdir -p "$FX"
cat > "$FX/hooks.json" <<'JSON'
{ "hooks": { "UserPromptSubmit": [ { "hooks": [
  { "type": "command", "command": "bash '[VAULT_PATH]/⚙️ Meta/scripts/graph-context-hook.sh'" },
  { "type": "command", "command": "python3 ~/.claude/skills/ai-brain-starter/hooks/log-skill-usage.py 2>/dev/null || echo '{}'" }
] } ] } }
JSON
# Deployed as a no-vault client would have it: the non-vault hook, NOT the vault one.
cat > "$TMP/novault-settings.json" <<'JSON'
{ "hooks": { "UserPromptSubmit": [ { "hooks": [
  { "type": "command", "command": "python3 ~/.claude/skills/ai-brain-starter/hooks/log-skill-usage.py 2>/dev/null || echo '{}'" }
] } ] } }
JSON
run_hook "$FX" "$TMP/novault-settings.json"
if ! fired; then ok "vault hook not demanded on a no-vault deploy (no false positive)"; else bad "false-fired on an unwired vault hook" "$(printf '%s' "$OUT" | head -c 300)"; fi

echo "=== 5b. same fixture, non-vault hook ALSO missing -> FIRES, but NOT for the vault hook ==="
cat > "$TMP/novault-broken.json" <<'JSON'
{ "hooks": { "UserPromptSubmit": [ { "hooks": [
  { "type": "command", "command": "SOMETHING_ELSE --unrelated" }
] } ] } }
JSON
run_hook "$FX" "$TMP/novault-broken.json"
if fired && mentions 'log-skill-usage.py'; then ok "fired for the genuinely-missing non-vault hook"; else bad "real non-vault drift went silent" "$(printf '%s' "$OUT" | head -c 300)"; fi
if notmentions 'graph-context-hook.sh'; then ok "never names the vault hook (correctly out of scope)"; else bad "REGRESSION: demanded a vault hook"; fi

echo "=== 6. ESCAPE HATCH: a pinned install stays silent even with real drift ==="
# Same stale settings.json that FIRED in case 2, but with the auto-update pin next
# to it. A pinned user opted out of the auto-update machinery -> no nag.
PINDIR="$TMP/pinned"; mkdir -p "$PINDIR"
cp "$TMP/stale.json" "$PINDIR/settings.json"
run_hook "$REPO_ROOT" "$PINDIR/settings.json"
if fired; then ok "control: drift fires without the pin (case 2 confirmed reproducible)"; else bad "stale.json unexpectedly clean"; fi
touch "$PINDIR/.ai-brain-starter-pinned"
run_hook "$REPO_ROOT" "$PINDIR/settings.json"
if ! fired; then ok "pinned install stays silent (escape hatch honored)"; else bad "nagged a pinned install" "$(printf '%s' "$OUT" | head -c 300)"; fi

echo "=== 7. SKILL-CONTENT DRIFT (MYC-3076): a bare copy behind the clone FIRES; synced is SILENT ==="
# A clone with a skills/ tree but NO hooks.json (so the hook-drift half stays
# silent and only the skill-drift half can speak), plus a bare install copy that
# lacks a section the clone has.
SKILLCLONE="$TMP/skillclone"; INST="$TMP/skillinst"
mkdir -p "$SKILLCLONE/skills/daily-journal" "$INST/daily-journal"
printf '## Setup\nx\n### Step 7\ny\n' > "$INST/daily-journal/SKILL.md"
printf '## Setup\nx\n## Crisis protocol\nsafety\n### Step 7\ny\n' > "$SKILLCLONE/skills/daily-journal/SKILL.md"
run_hook "$SKILLCLONE" "/nonexistent/settings.json" "$INST"
if fired && mentions 'skill-content check' && mentions 'daily-journal' && mentions 'Crisis protocol'; then
  ok "skill-content drift fires, names the skill + the missing section"
else bad "skill-content drift went silent or unnamed" "$(printf '%s' "$OUT" | head -c 300)"; fi
# NEG-CONTROL: make the bare copy identical -> silent.
cp "$SKILLCLONE/skills/daily-journal/SKILL.md" "$INST/daily-journal/SKILL.md"
run_hook "$SKILLCLONE" "/nonexistent/settings.json" "$INST"
if ! fired; then ok "a synced bare copy stays silent (skill-drift neg-control)"; else bad "cried wolf on a synced copy" "$(printf '%s' "$OUT" | head -c 300)"; fi
# AHEAD: a bare copy AHEAD of canonical is a trapped improvement -> FIRES as
# "upstream this so clients get it", and must NOT tell the user to sync it down.
printf '## Setup\nx\n## Local Only Section\nmine\n### Step 7\ny\n' > "$INST/daily-journal/SKILL.md"
printf '## Setup\nx\n### Step 7\ny\n' > "$SKILLCLONE/skills/daily-journal/SKILL.md"
run_hook "$SKILLCLONE" "/nonexistent/settings.json" "$INST"
if fired && mentions 'Ahead of canonical' && mentions 'daily-journal'; then
  ok "a copy AHEAD of canonical fires as upstream-this (not silent)"
else bad "an ahead copy went silent or unnamed" "$(printf '%s' "$OUT" | head -c 300)"; fi
if printf '%s' "$OUT" | grep -qi 'upstream'; then ok "ahead message frames it as upstream, so the improvement reaches clients"; else bad "ahead message did not say upstream"; fi

echo
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
