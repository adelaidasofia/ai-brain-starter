#!/usr/bin/env bash
# test_installer_replaces_auto_update.sh — proves the substrate auto-update hook
# swap (MYC-720) leaves EXACTLY ONE auto-update entry on an EXISTING install, not
# two firing at once.
#
# The current hooks.json runs the cross-platform script (scripts/ai-brain-auto-
# update.py), which shares NO fingerprint with the old inline blob NOR the
# interim bash script form. merge_hooks() alone would ADD the new entry and
# leave the old one -> the auto-updater would fire TWICE on every machine that
# already had it. The fix retires the old forms (unique substring
# ".ai-brain-starter-last-update" for the blob; the scripts/ai-brain-auto-
# update.sh path for the bash era) so merge-then-retire yields a single entry.
# This gate is the negative control for that fix.
#
# Three prior variants are exercised, all real:
#   A. pre-pin deployed blob   (no ".ai-brain-starter-pinned" — the HARDEST
#      case: shares nothing with the new command).
#   B. pinned committed blob   (has ".ai-brain-starter-pinned").
#   D. bash script-call era    (bash .../ai-brain-auto-update.sh || echo ...).
# Plus C: idempotent re-run stays at one entry.
#
# Run: bash tests/integration/test_installer_replaces_auto_update.sh  (0=pass,1=fail)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"
HOOKS_SRC="$REPO_ROOT/hooks.json"
[ -f "$INSTALLER" ] || { echo "ERROR: $INSTALLER not found" >&2; exit 1; }
[ -f "$HOOKS_SRC" ] || { echo "ERROR: $HOOKS_SRC not found" >&2; exit 1; }

PASS=0; FAIL=0
ok(){ printf '  PASS: %s\n' "$1"; PASS=$((PASS+1)); }
no(){ printf '  FAIL: %s\n' "$1"; FAIL=$((FAIL+1)); }
TMPROOT="$(mktemp -d)"; trap 'rm -rf "$TMPROOT"' EXIT

# Seed a settings.json whose UserPromptSubmit array holds an old-style inline
# auto-update command ($2) plus one unrelated user hook (must be preserved).
seed_settings() {  # $1=path $2=old_command
  OLD="$2" python3 - "$1" <<'PY'
import json, os, sys
old = os.environ["OLD"]
settings = {"hooks": {"UserPromptSubmit": [{"hooks": [
    {"type": "command", "command": old},
    {"type": "command", "command": "echo user-owned-unrelated-hook"},
]}]}}
json.dump(settings, open(sys.argv[1], "w"), indent=2)
PY
}

# Count UserPromptSubmit commands matching a substring.
count_cmds() {  # $1=settings $2=needle
  NEEDLE="$2" python3 - "$1" <<'PY'
import json, os, sys
needle = os.environ["NEEDLE"]
d = json.load(open(sys.argv[1]))
n = 0
for g in d.get("hooks", {}).get("UserPromptSubmit", []):
    for h in g.get("hooks", []):
        if needle in h.get("command", ""):
            n += 1
print(n)
PY
}

install() { run_sandboxed "$TMPROOT/home" python3 "$INSTALLER" --hooks-source "$HOOKS_SRC" --settings "$1" --quiet >/dev/null 2>&1; }

# A faithful-enough stand-in for each old inline blob: contains the retire
# fingerprint + the delegated-install instruction, NOT the new script path.
OLD_PREPIN='LAST=~/.claude/.ai-brain-starter-last-update; if [ ! -f "$LAST" ] || [ -n "$(find "$LAST" -mtime +6)" ]; then touch "$LAST" && cd ~/.claude/skills/ai-brain-starter && git pull --quiet origin main && bash ~/.claude/skills/ai-brain-starter/scripts/sync-skills.sh && echo "{\"hookSpecificOutput\":{\"additionalContext\":\"run install-hooks-user-level.py\"}}"; fi'
OLD_PINNED='if [ -f ~/.claude/.ai-brain-starter-pinned ]; then echo "{}"; exit 0; fi; '"$OLD_PREPIN"

mkdir -p "$TMPROOT/home/.claude"

# ---- A. pre-pin deployed blob -> replaced by exactly one script-call entry ----
S="$TMPROOT/a.json"; seed_settings "$S" "$OLD_PREPIN"; install "$S"
new_n=$(count_cmds "$S" "ai-brain-auto-update.py"); old_n=$(count_cmds "$S" ".ai-brain-starter-last-update"); user_n=$(count_cmds "$S" "user-owned-unrelated-hook")
if [ "$new_n" = "1" ] && [ "$old_n" = "0" ] && [ "$user_n" = "1" ]; then
  ok "A: pre-pin blob -> exactly 1 new entry, 0 old, user hook preserved"
else
  no "A: expected new=1 old=0 user=1, got new=$new_n old=$old_n user=$user_n"
fi

# ---- B. pinned committed blob -> same clean single entry ----------------------
S="$TMPROOT/b.json"; seed_settings "$S" "$OLD_PINNED"; install "$S"
new_n=$(count_cmds "$S" "ai-brain-auto-update.py"); old_n=$(count_cmds "$S" ".ai-brain-starter-last-update")
if [ "$new_n" = "1" ] && [ "$old_n" = "0" ]; then
  ok "B: pinned blob -> exactly 1 new entry, 0 old"
else
  no "B: expected new=1 old=0, got new=$new_n old=$old_n"
fi

# ---- C. idempotent re-run -> still exactly one -------------------------------
install "$S"   # second run over the already-migrated settings
new_n=$(count_cmds "$S" "ai-brain-auto-update.py"); old_n=$(count_cmds "$S" ".ai-brain-starter-last-update")
if [ "$new_n" = "1" ] && [ "$old_n" = "0" ]; then
  ok "C: idempotent re-install stays at exactly 1 entry"
else
  no "C: re-run drifted to new=$new_n old=$old_n"
fi

# ---- D. bash script-call era -> migrated to exactly one .py entry ------------
OLD_SH='if [ -f ~/.claude/.ai-brain-starter-pinned ]; then echo "{}"; exit 0; fi; bash ~/.claude/skills/ai-brain-starter/scripts/ai-brain-auto-update.sh || echo "{}"'
S="$TMPROOT/d.json"; seed_settings "$S" "$OLD_SH"; install "$S"
py_n=$(count_cmds "$S" "ai-brain-auto-update.py"); sh_n=$(count_cmds "$S" "ai-brain-auto-update.sh"); user_n=$(count_cmds "$S" "user-owned-unrelated-hook")
if [ "$py_n" = "1" ] && [ "$sh_n" = "0" ] && [ "$user_n" = "1" ]; then
  ok "D: bash-era script call -> exactly 1 .py entry, 0 .sh, user hook preserved"
else
  no "D: expected py=1 sh=0 user=1, got py=$py_n sh=$sh_n user=$user_n"
fi

echo
echo "test_installer_replaces_auto_update: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
