#!/usr/bin/env bash
# test_windows_platformize.sh — gate for the Windows hook-wiring path.
#
# WHY: on native Windows, Claude Code runs hook commands under PowerShell 5.1 /
# 7 / cmd.exe / Git Bash depending on version — POSIX one-liners (`||`,
# `[ -f ]`, `2>/dev/null`, `python3`) fail in some or all of them, which made
# every session on a Windows install surface hook errors, and made the drift
# surfacer fire a scary "deploy failed" note every session (the checkout was
# current but settings.json held commands Windows could never run or heal).
#
# The fix: install-hooks-user-level.py rewrites template commands on Windows
# (ABS_FORCE_WINDOWS=1 makes POSIX CI take that path hermetically) into
#   <launcher> "<abs>/scripts/hook_runner.py" --fallback <kind> "<abs>/<hook>.py"
# and hook_runner.py reproduces the shell forms' masking semantics in Python.
#
#   T1  Windows install -> every owned command is runner-form (no shell-isms)
#   T2  auto-update wired through the runner too (heals itself on Windows)
#   T3  existing POSIX-form install migrates in place, no duplicates
#   T4  bash-only vault hooks are OMITTED on Windows (even with --vault-path)
#   T5  drift surfacer stays SILENT on a Windows-form install (the false-alarm
#       loop this whole fix kills — the critical negative control)
#   T6  hook_runner: missing script -> silent JSON, exit 0
#   T7  hook_runner: exit-2 block propagates (stderr + exit 2)
#   T8  hook_runner: crash (exit 1) -> fallback JSON, exit 0
#   T9  hook_runner: --fallback allow emits the PreToolUse allow form
#   T10 sync-skills.py: creates, backs up before overwrite, skips .git forks
#
# Run: bash tests/integration/test_windows_platformize.sh  (0 = pass, 1 = fail)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"
RUNNER="$REPO_ROOT/scripts/hook_runner.py"
SYNC="$REPO_ROOT/scripts/sync-skills.py"
SURFACER="$REPO_ROOT/hooks/surface-deployed-hooks-behind.py"
for f in "$INSTALLER" "$RUNNER" "$SYNC" "$SURFACER"; do
  [ -f "$f" ] || { echo "ERROR: $f not found" >&2; exit 1; }
done

PASS=0; FAIL=0
ok(){ printf '  PASS: %s\n' "$1"; PASS=$((PASS+1)); }
no(){ printf '  FAIL: %s\n' "$1"; FAIL=$((FAIL+1)); }
TMPROOT="$(mktemp -d)"; trap 'rm -rf "$TMPROOT"' EXIT

WIN_ENV=(ABS_FORCE_WINDOWS=1 ABS_WIN_LAUNCHER="py -3")

# ---- T1 + T2: fresh Windows install -> runner-form everywhere ----------------
S="$TMPROOT/win.json"
run_sandboxed "$TMPROOT/home" env "${WIN_ENV[@]}" python3 "$INSTALLER" \
  --hooks-source "$REPO_ROOT/hooks.json" --settings "$S" --quiet >/dev/null 2>&1
audit=$(python3 - "$S" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
bad, runner, autoupd = [], 0, 0
for ev, groups in d.get("hooks", {}).items():
    for g in groups:
        for h in g.get("hooks", []):
            c = h.get("command", "")
            if "hook_runner.py" in c:
                runner += 1
                if "ai-brain-auto-update.py" in c:
                    autoupd += 1
                for ism in ("||", "[ -f", "2>/dev/null", "/dev/null", "&&"):
                    if ism in c:
                        bad.append(f"{ev}: {ism} in {c[:60]}")
            elif "ai-brain-starter" in c:
                bad.append(f"{ev}: non-runner owned cmd {c[:60]}")
print(json.dumps({"bad": bad, "runner": runner, "autoupd": autoupd}))
PY
)
bad_n=$(printf '%s' "$audit" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["bad"]))')
runner_n=$(printf '%s' "$audit" | python3 -c 'import json,sys; print(json.load(sys.stdin)["runner"])')
autoupd_n=$(printf '%s' "$audit" | python3 -c 'import json,sys; print(json.load(sys.stdin)["autoupd"])')
if [ "$bad_n" = "0" ] && [ "$runner_n" -gt 10 ]; then
  ok "T1: $runner_n commands runner-form, zero shell-isms"
else
  no "T1: bad=$bad_n runner=$runner_n ($(printf '%s' "$audit" | head -c 300))"
fi
if [ "$autoupd_n" = "1" ]; then
  ok "T2: auto-update wired through the runner (self-healing on Windows)"
else
  no "T2: expected 1 runner-form auto-update entry, got $autoupd_n"
fi

# ---- T3: existing POSIX-form install migrates in place, no duplicates --------
S3="$TMPROOT/migrate.json"
python3 - "$S3" <<'PY'
import json, sys
settings = {"hooks": {"UserPromptSubmit": [{"hooks": [
    {"type": "command", "command": "python3 ~/.claude/skills/ai-brain-starter/hooks/log-skill-usage.py 2>/dev/null || echo '{}'"},
    {"type": "command", "command": "echo user-owned-unrelated-hook"},
]}]}}
json.dump(settings, open(sys.argv[1], "w"), indent=2)
PY
run_sandboxed "$TMPROOT/home" env "${WIN_ENV[@]}" python3 "$INSTALLER" \
  --hooks-source "$REPO_ROOT/hooks.json" --settings "$S3" --quiet >/dev/null 2>&1
counts=$(python3 - "$S3" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
log_n = runner_log_n = user_n = 0
for groups in d.get("hooks", {}).values():
    for g in groups:
        for h in g.get("hooks", []):
            c = h.get("command", "")
            if "log-skill-usage.py" in c:
                log_n += 1
                if "hook_runner.py" in c:
                    runner_log_n += 1
            if "user-owned-unrelated-hook" in c:
                user_n += 1
print(f"{log_n} {runner_log_n} {user_n}")
PY
)
read -r log_n runner_log_n user_n <<< "$counts"
if [ "$log_n" = "1" ] && [ "$runner_log_n" = "1" ] && [ "$user_n" = "1" ]; then
  ok "T3: POSIX entry migrated to runner-form in place, no dupes, user hook kept"
else
  no "T3: log=$log_n runner_log=$runner_log_n user=$user_n (want 1 1 1)"
fi

# ---- T4: bash-only vault hooks omitted on Windows even with a vault path -----
S4="$TMPROOT/vault.json"
mkdir -p "$TMPROOT/fakevault"
run_sandboxed "$TMPROOT/home" env "${WIN_ENV[@]}" python3 "$INSTALLER" \
  --hooks-source "$REPO_ROOT/hooks.json" --settings "$S4" \
  --vault-path "$TMPROOT/fakevault" --quiet >/dev/null 2>&1
sh_hits=$(python3 - "$S4" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
n = 0
for groups in d.get("hooks", {}).values():
    for g in groups:
        for h in g.get("hooks", []):
            c = h.get("command", "")
            if any(s in c for s in ("graph-context-hook.sh", "session-end-hook.sh", "write-hook.sh")):
                n += 1
print(n)
PY
)
if [ "$sh_hits" = "0" ]; then
  ok "T4: bash-only vault hooks omitted on Windows (no dead commands wired)"
else
  no "T4: $sh_hits bash vault hook(s) wired on Windows"
fi

# ---- T5: drift surfacer SILENT on a Windows-form install ---------------------
OUT=$(run_sandboxed "$TMPROOT/home" env ABS_SKILL_DIR="$REPO_ROOT" ABS_SETTINGS_JSON="$S" \
  python3 "$SURFACER" </dev/null 2>/dev/null)
if printf '%s' "$OUT" | grep -q "additionalContext"; then
  no "T5: drift surfacer FIRED on a healthy Windows install: $(printf '%s' "$OUT" | head -c 200)"
else
  ok "T5: drift surfacer silent on a healthy Windows-form install (no false alarm)"
fi

# ---- T6-T9: hook_runner semantics ---------------------------------------------
OUT=$(python3 "$RUNNER" --fallback silent "$TMPROOT/does-not-exist.py" </dev/null 2>/dev/null); RC=$?
if [ "$RC" = "0" ] && printf '%s' "$OUT" | grep -q suppressOutput; then
  ok "T6: missing script -> silent JSON, exit 0"
else
  no "T6: rc=$RC out=$OUT"
fi

cat > "$TMPROOT/blocker.py" <<'PY'
import sys
print("blocked for a reason", file=sys.stderr)
sys.exit(2)
PY
ERR=$(python3 "$RUNNER" --fallback silent "$TMPROOT/blocker.py" </dev/null 2>&1 >/dev/null); RC=$?
if [ "$RC" = "2" ] && printf '%s' "$ERR" | grep -q "blocked for a reason"; then
  ok "T7: exit-2 block propagates (stderr + exit 2)"
else
  no "T7: rc=$RC err=$ERR"
fi

cat > "$TMPROOT/crasher.py" <<'PY'
raise RuntimeError("boom")
PY
OUT=$(python3 "$RUNNER" --fallback silent "$TMPROOT/crasher.py" </dev/null 2>/dev/null); RC=$?
if [ "$RC" = "0" ] && printf '%s' "$OUT" | grep -q suppressOutput; then
  ok "T8: crash -> fallback JSON, exit 0 (no visible hook error)"
else
  no "T8: rc=$RC out=$OUT"
fi

OUT=$(python3 "$RUNNER" --fallback allow "$TMPROOT/crasher.py" </dev/null 2>/dev/null); RC=$?
if [ "$RC" = "0" ] && printf '%s' "$OUT" | grep -q '"permissionDecision":"allow"'; then
  ok "T9: --fallback allow emits the PreToolUse allow form"
else
  no "T9: rc=$RC out=$OUT"
fi

# ---- T10: sync-skills.py create / backup / fork-skip --------------------------
SRC="$TMPROOT/starter"; DST="$TMPROOT/installed"
mkdir -p "$SRC/skills/alpha" "$SRC/skills/forked/.git" "$DST/alpha" "$DST/forked"
printf 'v2\n' > "$SRC/skills/alpha/SKILL.md"
printf 'v1-customized\n' > "$DST/alpha/SKILL.md"
printf 'new-file\n' > "$SRC/skills/alpha/extra.md"
mkdir -p "$DST/forked/.git"
printf 'upstream\n' > "$SRC/skills/forked/SKILL.md"
printf 'their-fork\n' > "$DST/forked/SKILL.md"
OUT=$(env ABS_SYNC_STARTER_DIR="$SRC" ABS_SYNC_INSTALL_DIR="$DST" python3 "$SYNC" 2>&1); RC=$?
bak_n=0
for f in "$DST/alpha/"SKILL.md.bak-*; do [ -e "$f" ] && bak_n=$((bak_n + 1)); done
if [ "$RC" = "0" ] && [ "$(cat "$DST/alpha/SKILL.md")" = "v2" ] && [ "$bak_n" = "1" ] \
   && [ -f "$DST/alpha/extra.md" ] && [ "$(cat "$DST/forked/SKILL.md")" = "their-fork" ]; then
  ok "T10: sync updates + backs up, creates new files, skips .git forks"
else
  no "T10: rc=$RC alpha=$(cat "$DST/alpha/SKILL.md" 2>/dev/null) bak=$bak_n forked=$(cat "$DST/forked/SKILL.md" 2>/dev/null)"
fi

echo
echo "test_windows_platformize: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
