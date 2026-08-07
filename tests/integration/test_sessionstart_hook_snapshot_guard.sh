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
# Fails on revert: if identity normalization, the v2 snapshot format, legacy
# raw-string migration, or the --refresh flag regresses, an assertion flips and
# the script exits non-zero.
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

# write_legacy_snapshot <home> <rawcommand...> -- the pre-fix format: a bare JSON
# list of raw command strings (no {"v":2} wrapper). Must normalize on read.
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

echo "=== scenario 1: first run baselines silently, writes v2 ==="
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
grep -q '"v": 2' "$SF1" 2>/dev/null && ok "snapshot is v2 format" || bad "snapshot is v2 format" "$(head -1 "$SF1" 2>/dev/null)"
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

echo ""
echo "=== SUMMARY: $PASS passed, $FAIL failed ==="
[ "$FAIL" = 0 ]
