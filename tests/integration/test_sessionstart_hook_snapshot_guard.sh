#!/usr/bin/env bash
# Regression test for hooks/sessionstart-hook-snapshot-guard.py
#
# Guards the de-noise fix: the SessionStart snapshot guard diffs SCRIPT IDENTITY
# (script basename + normalized args), NOT raw command strings. So a concurrent
# session's cosmetic reword of a still-wired hook -- python3 vs /usr/bin/python3,
# ~/ vs absolute path, an added `2>/dev/null || echo {...}` wrapper, or a
# `[ -f X ] &&` guard -- must NOT false-flag the hook as "missing". A genuinely
# removed script MUST still warn.
#
# Also guards the WINDOWS half (MYC-3880). The installer rewrites every hook into
# a launcher shim -- `py -3 "<abs>\scripts\hook_runner.py" --fallback silent
# "<abs>\hooks\<hook>.py"` -- which the old identity regex could not parse at all
# (no `~`, no `/`), so all 19 Windows commands fell through to a `c[:100]` text
# branch whose first 100 chars are the SAME launcher preamble for every hook.
# Measured on a real install: 25 wired commands -> 7 identities, 19 of them fused
# into one. Deleting any single one of those 19 could not be detected. Scenarios
# 8-10 below drive that end to end; the hook's own --self-test (scenario 0)
# carries the hermetic per-form controls.
#
# Fails on revert: if identity normalization, the v3 snapshot format, the
# pre-v3 re-baseline, legacy raw-string migration, the Windows shim resolution,
# or the --refresh flag regresses, an assertion flips and the script exits
# non-zero.
#
# Isolation: each scenario runs the guard under a throwaway $HOME so it reads a
# fake settings.json and writes a fake state file -- the real ~/.claude is never
# touched. Stdlib python3 + bash only; no network, no git, no ruff.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
GUARD="$REPO_ROOT/hooks/sessionstart-hook-snapshot-guard.py"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$SCRIPT_DIR/lib/sandbox_home.sh"

PASS=0
FAIL=0
TMPDIRS=()
cleanup() { for d in "${TMPDIRS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done; }
trap cleanup EXIT

ok()  { PASS=$((PASS+1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL  $1 :: $2"; }
assert_rc()     { [ "$RC" = "$2" ] && ok "$1" || bad "$1" "rc=$RC want $2 (err=${ERR:0:80})"; }
assert_warns()  { case "$OUT" in *WARNING*) ok "$1" ;; *) bad "$1" "no WARNING (out=${OUT:0:90})" ;; esac; }
assert_silent() { case "$OUT" in *WARNING*) bad "$1" "unexpected WARNING (out=${OUT:0:90})" ;; *) ok "$1" ;; esac; }
assert_has()    { case "$OUT" in *"$2"*) ok "$1" ;; *) bad "$1" "missing '$2' (out=${OUT:0:90})" ;; esac; }

newhome() { local d; d="$(mktemp -d)"; TMPDIRS+=("$d"); mkdir -p "$d/.claude"; echo "$d"; }
state_file() { echo "$1/.claude/state/sessionstart-hooks-snapshot.json"; }

# write_settings <home> <command...> -- one SessionStart hook per command arg.
write_settings() {
  local home="$1"; shift
  python3 - "$home/.claude/settings.json" "$@" <<'PY'
import json, sys
path, cmds = sys.argv[1], sys.argv[2:]
data = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": c} for c in cmds]}]}}
with open(path, "w") as f:
    f.write(json.dumps(data, indent=2))
PY
}

# write_legacy_snapshot <home> <rawcommand...> -- the pre-versioning format: a
# bare JSON list of raw command strings (no {"v":N} wrapper). Must normalize on read.
write_legacy_snapshot() {
  local home="$1"; shift
  mkdir -p "$home/.claude/state"
  python3 - "$home/.claude/state/sessionstart-hooks-snapshot.json" "$@" <<'PY'
import json, sys
path, cmds = sys.argv[1], sys.argv[2:]
with open(path, "w") as f:
    f.write(json.dumps(sorted(cmds), indent=2))
PY
}

# write_v2_snapshot <home> <identity...> -- a v2 snapshot, the format shipped
# before the Windows shim fix. Its identities came from the OLD function, so they
# are NOT comparable to v3 ones and must trigger a silent re-baseline.
write_v2_snapshot() {
  local home="$1"; shift
  mkdir -p "$home/.claude/state"
  python3 - "$home/.claude/state/sessionstart-hooks-snapshot.json" "$@" <<'PY'
import json, sys
path, idents = sys.argv[1], sys.argv[2:]
with open(path, "w") as f:
    f.write(json.dumps({"v": 2, "identities": sorted(idents)}, indent=2))
PY
}

# win_cmd <hookbasename> -- exactly what install-hooks-user-level.py emits on
# Windows. Hermetic absolute paths (never this machine's home) so the assertion
# means the same thing on every runner.
WIN_RUNNER='C:\Users\dev\.claude\skills\ai-brain-starter\scripts\hook_runner.py'
WIN_HOOKS='C:\Users\dev\.claude\skills\ai-brain-starter\hooks'
win_cmd() { printf 'py -3 "%s" --fallback silent "%s\\%s"' "$WIN_RUNNER" "$WIN_HOOKS" "$1"; }

# run_guard <home> [args...] ; sets RC + OUT + ERR
run_guard() {
  local home="$1"; shift
  OUT="$(run_sandboxed "$home" python3 "$GUARD" "$@" 2>/tmp/_ss_err.$$)"
  RC=$?
  ERR="$(cat /tmp/_ss_err.$$ 2>/dev/null)"; rm -f /tmp/_ss_err.$$
  return 0
}

echo "=== precondition ==="
[ -f "$GUARD" ] && ok "guard exists" || bad "guard exists" "missing $GUARD"

echo "=== scenario 0: the guard's own hermetic identity controls ==="
# Per-form controls for _script_identity() + the snapshot version rules: every
# Windows launcher-shim flavour resolves to its TARGET basename, 19 distinct
# Windows commands yield 19 distinct identities (the pre-fix function yielded 1),
# identity keys are never truncated, and a pre-v3 snapshot re-baselines. Reads
# neither settings.json nor the state file, so it needs no sandbox.
SELFTEST_OUT="$(python3 "$GUARD" --self-test 2>&1)"; SELFTEST_RC=$?
[ "$SELFTEST_RC" = 0 ] && ok "guard --self-test passes" \
  || bad "guard --self-test passes" "rc=$SELFTEST_RC :: ${SELFTEST_OUT:0:400}"

echo "=== scenario 1: first run baselines silently, writes v3 ==="
H1="$(newhome)"
# literal ~ is the raw settings.json command text under test, not a path to expand
# shellcheck disable=SC2088
write_settings "$H1" \
  'python3 ~/.claude/hooks/foo.py' \
  '~/.claude/hooks/bar.sh' \
  'python3 ~/.claude/hooks/baz.py --flag'
run_guard "$H1"
assert_rc     "first run exits 0" 0
assert_silent "first run is silent (baseline)"
SF1="$(state_file "$H1")"
[ -f "$SF1" ] && ok "baseline writes snapshot" || bad "baseline writes snapshot" "absent"
grep -q '"v": 3' "$SF1" 2>/dev/null && ok "snapshot is v3 format" || bad "snapshot is v3 format" "$(head -1 "$SF1" 2>/dev/null)"
python3 -c "import json;d=json.load(open('$SF1'));assert sorted(d['identities'])==['bar.sh','baz.py||--flag','foo.py'],d" 2>/dev/null \
  && ok "baseline captured 3 script identities" || bad "baseline captured 3 script identities" "$(cat "$SF1" 2>/dev/null)"

echo "=== scenario 2: reworded variants -> ZERO false missing ==="
write_settings "$H1" \
  '/usr/bin/python3 /home/u/.claude/hooks/foo.py' \
  '[ -f ~/.claude/hooks/bar.sh ] && ~/.claude/hooks/bar.sh' \
  "python3 ~/.claude/hooks/baz.py --flag 2>/dev/null || echo '{\"continue\":true}'"
run_guard "$H1"
assert_rc     "reworded run exits 0" 0
assert_silent "reworded same-scripts -> no false missing"

echo "=== scenario 3: genuine removal -> warns, persists until reconciled ==="
write_settings "$H1" \
  '/usr/bin/python3 /home/u/.claude/hooks/foo.py' \
  '[ -f ~/.claude/hooks/bar.sh ] && ~/.claude/hooks/bar.sh'
run_guard "$H1"
assert_rc    "removal run exits 0" 0
assert_warns "genuine removal warns"
assert_has   "names the dropped script" "baz.py"
run_guard "$H1"
assert_warns "warning persists (snapshot not silently updated)"

echo "=== scenario 4: --refresh force-rewrites + clears the warning ==="
run_guard "$H1" --refresh
assert_rc  "--refresh exits 0" 0
assert_has "--refresh confirms" "refreshed"
run_guard "$H1"
assert_silent "after --refresh, no stale warning"

echo "=== scenario 5: legacy raw-string snapshot normalizes on read ==="
H2="$(newhome)"
# literal ~ is the raw settings.json command text under test, not a path to expand
# shellcheck disable=SC2088
write_legacy_snapshot "$H2" \
  'python3 ~/.claude/hooks/foo.py' \
  '~/.claude/hooks/bar.sh' \
  'python3 ~/.claude/hooks/baz.py --flag'
write_settings "$H2" \
  '/usr/bin/python3 /home/u/.claude/hooks/foo.py' \
  '[ -f ~/.claude/hooks/bar.sh ] && ~/.claude/hooks/bar.sh' \
  "python3 ~/.claude/hooks/baz.py --flag 2>/dev/null || echo '{}'"
run_guard "$H2"
assert_rc     "legacy migrate run exits 0" 0
assert_silent "legacy list + reworded scripts -> no false missing (migration-free)"

echo "=== scenario 6: legacy snapshot still catches a real drop ==="
H3="$(newhome)"
write_legacy_snapshot "$H3" \
  'python3 ~/.claude/hooks/foo.py' \
  'python3 ~/.claude/hooks/baz.py --flag'
write_settings "$H3" 'python3 ~/.claude/hooks/foo.py'
run_guard "$H3"
assert_warns "legacy comparison catches a genuinely removed script"
assert_has   "names dropped script (legacy path)" "baz.py"

echo "=== scenario 7: additions absorbed silently ==="
H4="$(newhome)"
write_settings "$H4" 'python3 ~/.claude/hooks/foo.py'
run_guard "$H4"                         # baseline
write_settings "$H4" \
  'python3 ~/.claude/hooks/foo.py' \
  'python3 ~/.claude/hooks/new.py'
run_guard "$H4"
assert_rc     "additions run exits 0" 0
assert_silent "new hook added -> absorbed silently"
SF4="$(state_file "$H4")"
python3 -c "import json;d=json.load(open('$SF4'));assert 'new.py' in d['identities'],d" 2>/dev/null \
  && ok "addition recorded in snapshot" || bad "addition recorded in snapshot" "$(cat "$SF4" 2>/dev/null)"

echo "=== scenario 8: Windows shim form -> one identity PER HOOK, not per launcher ==="
# The MYC-3880 regression, end to end. Before the fix these three commands shared
# a single identity (the 100-char launcher preamble), so the baseline recorded
# ONE hook where three are wired.
H5="$(newhome)"
write_settings "$H5" \
  "$(win_cmd session-start-context.py)" \
  "$(win_cmd heal-journal-guard.py)" \
  "$(win_cmd lint-claude-settings.py)"
run_guard "$H5"
assert_rc     "windows baseline exits 0" 0
assert_silent "windows baseline is silent"
SF5="$(state_file "$H5")"
python3 -c "import json;d=json.load(open('$SF5'));assert sorted(d['identities'])==['heal-journal-guard.py','lint-claude-settings.py','session-start-context.py'],d" 2>/dev/null \
  && ok "windows form -> 3 TARGET basenames (not 1 launcher)" \
  || bad "windows form -> 3 TARGET basenames (not 1 launcher)" "$(cat "$SF5" 2>/dev/null)"
grep -q 'hook_runner' "$SF5" 2>/dev/null \
  && bad "identity is the target, never the launcher" "snapshot names hook_runner: $(cat "$SF5")" \
  || ok "identity is the target, never the launcher"

echo "=== scenario 9: dropping ONE of the windows hooks warns (was undetectable) ==="
# The whole point. Pre-fix, the two survivors kept the shared identity alive and
# the guard stayed silent; it could only fire if ALL of them vanished at once.
write_settings "$H5" \
  "$(win_cmd session-start-context.py)" \
  "$(win_cmd lint-claude-settings.py)"
run_guard "$H5"
assert_rc    "windows drop run exits 0" 0
assert_warns "dropping 1 of 3 windows hooks warns"
assert_has   "names the dropped TARGET hook" "heal-journal-guard.py"
# A surviving sibling must NOT be reported missing -- that is the cry-wolf the
# identity diff exists to prevent.
case "$OUT" in
  *lint-claude-settings.py*) bad "survivors not false-flagged" "reported a wired hook missing: ${OUT:0:120}" ;;
  *) ok "survivors not false-flagged" ;;
esac

echo "=== scenario 10: a v2 snapshot re-baselines silently (no 19-hooks-missing) ==="
# v2 identities came from the OLD function; the Windows ones were a collapsed
# launcher preamble. Comparing or re-normalizing them against v3 identities would
# hand every Windows user a fleet-wide false alarm on the first run after the fix.
H6="$(newhome)"
write_v2_snapshot "$H6" \
  'py -3 "C:\Users\dev\.claude\skills\ai-brain-starter\scripts\hook_runner.py" --fallback silent "C:\' \
  'check-claude-code-version.sh'
write_settings "$H6" \
  "$(win_cmd session-start-context.py)" \
  "$(win_cmd heal-journal-guard.py)"
run_guard "$H6"
assert_rc     "v2 upgrade run exits 0" 0
assert_silent "v2 snapshot -> silent re-baseline, no false 'missing'"
SF6="$(state_file "$H6")"
grep -q '"v": 3' "$SF6" 2>/dev/null && ok "v2 snapshot rewritten as v3" || bad "v2 snapshot rewritten as v3" "$(head -3 "$SF6" 2>/dev/null)"
python3 -c "import json;d=json.load(open('$SF6'));assert sorted(d['identities'])==['heal-journal-guard.py','session-start-context.py'],d" 2>/dev/null \
  && ok "re-baseline captures current identities" || bad "re-baseline captures current identities" "$(cat "$SF6" 2>/dev/null)"
# And the NEXT drop after the re-baseline still warns -- the migration absorbs
# one diff, it does not disarm the guard.
write_settings "$H6" "$(win_cmd session-start-context.py)"
run_guard "$H6"
assert_warns "post-migration drop still warns"
assert_has   "post-migration warning names the hook" "heal-journal-guard.py"

echo "=== scenario 11: a POSIX -> Windows rewire is NOT a disappearance ==="
# Running install-hooks-user-level.py on Windows platformizes EVERY wired command
# in one shot. So the two wirings of one hook must yield the SAME identity, or that
# single upgrade reads as the whole fleet vanishing at once -- the fleet-wide false
# alarm scenario 10 guards against for v2 snapshots, reached by a different route
# and on an install whose snapshot is already current.
#
# _self_test() pins this parity per command string; this pins it end to end, across
# settings.json -> identities -> snapshot diff, which is where an integration-level
# regression (in _current_identities or extract_sessionstart_commands) would land
# without touching _script_identity at all.
H7="$(newhome)"
# literal ~ is the raw settings.json command text under test, not a path to expand
# shellcheck disable=SC2088
write_settings "$H7" \
  'python3 ~/.claude/hooks/session-start-context.py 2>/dev/null || echo "{}"' \
  'python3 ~/.claude/hooks/lint-claude-settings.py'
run_guard "$H7"
assert_rc     "posix baseline exits 0" 0
assert_silent "posix baseline is silent"
write_settings "$H7" \
  "$(win_cmd session-start-context.py)" \
  "$(win_cmd lint-claude-settings.py)"
run_guard "$H7"
assert_rc     "rewire run exits 0" 0
assert_silent "platformizing the SAME hooks -> no false missing"
# ...and the snapshot must still hold ONE identity per hook, not one per wiring.
SF7="$(state_file "$H7")"
python3 -c "import json;d=json.load(open('$SF7'));assert sorted(d['identities'])==['lint-claude-settings.py','session-start-context.py'],d" 2>/dev/null \
  && ok "rewire leaves one identity per hook" \
  || bad "rewire leaves one identity per hook" "$(cat "$SF7" 2>/dev/null)"

echo ""
echo "=== SUMMARY: $PASS passed, $FAIL failed ==="
[ "$FAIL" = 0 ]
