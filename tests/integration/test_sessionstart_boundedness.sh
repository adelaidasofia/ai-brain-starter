#!/usr/bin/env bash
# CI lock for the SessionStart forward guard (MYC-571, parent incident MYC-570).
#
# scripts/audit-sessionstart-boundedness.py asserts that every SessionStart-wired
# hook which does a recursive / corpus-scale filesystem walk carries all three
# bounded-hook guards - single-instance flock + cooldown stamped-at-START +
# wall-clock deadline - OR a co-located `# sessionstart-walk-bounded: <reason>`
# exemption.
#
# This is the FORWARD half of the 2026-06-05 Mac-freeze fix. MYC-512 moved the
# corpus-walk secret scan off SessionStart, MYC-514 hardened it, and
# test_sessionstart_freeze_class_excluded.sh pins those two SPECIFIC hooks. But
# nothing stopped the NEXT new SessionStart hook from re-introducing the
# corpus-walk-on-cold-start class. This guard + test close that gap by making the
# bounded-hook property checkable by construction (it mechanizes the hand-written
# "Bound" column in docs/HOOK_FLEET_RESOURCE_GOVERNANCE.md).
#
# Asserts:
#   1. The detector's own pos/neg controls pass (--selftest).
#   2. The REAL shipped SessionStart fleet (this repo's hooks.json) is clean (--all).
#   3. NEGATIVE CONTROL: a synthetic hooks.json wiring an unguarded corpus-walk
#      hook on SessionStart trips --all (exit 1), and --check trips on that file.
#      Proves the guard BITES, not just passes.
#   4. POSITIVE CONTROL: a fully-guarded walk passes --check (exit 0).
#
# Stdlib python3 + bash only. No network, no git. Tmpdirs removed on exit.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
AUDIT="$REPO_ROOT/scripts/audit-sessionstart-boundedness.py"

PASS=0
FAIL=0
TMPDIRS=()
cleanup() { for d in "${TMPDIRS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done; }
trap cleanup EXIT

ok()  { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1 :: $2"; }

echo "=== precondition ==="
if [ -f "$AUDIT" ]; then ok "detector present"; else bad "detector present" "missing $AUDIT"; fi

echo "=== 1. detector pos/neg controls (--selftest) ==="
if python3 "$AUDIT" --selftest >/dev/null 2>&1; then
  ok "--selftest passes"
else
  bad "--selftest passes" "run: python3 $AUDIT --selftest"
fi

echo "=== 2. shipped SessionStart fleet is clean (--all) ==="
if python3 "$AUDIT" --all >/dev/null 2>&1; then
  ok "real shipped fleet is guarded/declared-bounded"
else
  bad "real shipped fleet clean" "a wired hook is an unguarded corpus walk (run: python3 $AUDIT --all)"
fi

echo "=== 3. NEGATIVE CONTROL: an unguarded corpus-walk hook trips the guard ==="
NEG="$(mktemp -d)"
TMPDIRS+=("$NEG")
mkdir -p "$NEG/hooks"
cat > "$NEG/hooks/evil-walk.py" <<'PY'
#!/usr/bin/env python3
import os
def main():
    for _root, _dirs, _files in os.walk(os.path.expanduser("~")):  # unbounded corpus walk, no guards
        pass
PY
cat > "$NEG/hooks.json" <<'JSON'
{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/evil-walk.py"}]}]}}
JSON
if python3 "$AUDIT" --all --hooks-json "$NEG/hooks.json" --hooks-dir "$NEG/hooks" >/dev/null 2>&1; then
  bad "neg control --all trips" "an unguarded SessionStart corpus walk PASSED --all (the guard is asleep)"
else
  ok "neg control --all trips (exit 1)"
fi
if python3 "$AUDIT" --check "$NEG/hooks/evil-walk.py" >/dev/null 2>&1; then
  bad "neg control --check trips" "an unguarded corpus walk PASSED --check"
else
  ok "neg control --check trips (exit 1)"
fi

echo "=== 4. POSITIVE CONTROL: a fully-guarded walk passes --check ==="
POS="$(mktemp -d)"
TMPDIRS+=("$POS")
cat > "$POS/good-walk.py" <<'PY'
#!/usr/bin/env python3
import fcntl, time
from pathlib import Path
MARKER = Path("~/.last").expanduser()
def _stamp(): MARKER.write_text(str(time.time()))
def main():
    fh = open("/tmp/x.lock", "w"); fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _stamp()  # cooldown stamped BEFORE the walk
    deadline = time.time() + 60
    for p in Path("~/vault").expanduser().rglob("*.md"):
        if time.time() > deadline:
            break
PY
if python3 "$AUDIT" --check "$POS/good-walk.py" >/dev/null 2>&1; then
  ok "positive control (3 guards) passes --check"
else
  bad "positive control passes --check" "a fully-guarded walk was wrongly flagged"
fi

echo "=== 5. EFFECTIVE-SET audit (--settings): the real harm surface, MYC-1113 ==="
EFF="$(mktemp -d)"
TMPDIRS+=("$EFF")
# an unguarded corpus-walk hook at a resolvable absolute path
cat > "$EFF/evil-eff.py" <<'PY'
#!/usr/bin/env python3
import os
def main():
    for _r, _d, _f in os.walk(os.path.expanduser("~")):  # unbounded corpus walk
        pass
PY
# a bounded hook (single iterdir level)
cat > "$EFF/good-eff.py" <<'PY'
#!/usr/bin/env python3
from pathlib import Path
def main():
    for _p in Path("~/.claude/worktrees").expanduser().iterdir():
        pass
PY
# settings.json wiring BOTH on SessionStart (absolute command paths)
cat > "$EFF/settings_bad.json" <<JSON
{"hooks": {"SessionStart": [{"hooks": [
  {"type": "command", "command": "python3 $EFF/good-eff.py 2>/dev/null || true"},
  {"type": "command", "command": "python3 $EFF/evil-eff.py 2>/dev/null || true"}
]}]}}
JSON
cat > "$EFF/settings_ok.json" <<JSON
{"hooks": {"SessionStart": [{"hooks": [
  {"type": "command", "command": "python3 $EFF/good-eff.py 2>/dev/null || true"}
]}]}}
JSON
# NEGATIVE CONTROL: an unguarded walker in the effective set trips --settings (exit 1)
if python3 "$AUDIT" --settings "$EFF/settings_bad.json" >/dev/null 2>&1; then
  bad "neg control --settings trips" "an unguarded walker in the effective set PASSED --settings"
else
  ok "neg control --settings trips (exit 1)"
fi
# porcelain shape is parseable by diagnose.sh
if python3 "$AUDIT" --settings "$EFF/settings_bad.json" --porcelain 2>/dev/null | grep -q '^UNGUARDED:1:evil-eff.py'; then
  ok "porcelain emits UNGUARDED:<n>:<hooks> for diagnose.sh"
else
  bad "porcelain shape" "expected UNGUARDED:1:evil-eff.py"
fi
# POSITIVE CONTROL: a clean effective set passes (exit 0)
if python3 "$AUDIT" --settings "$EFF/settings_ok.json" >/dev/null 2>&1; then
  ok "positive control --settings passes (exit 0)"
else
  bad "positive control --settings passes" "a bounded effective set was flagged"
fi
# FAIL-LOUD: a missing settings.json exits 2 (never silently 'clean')
python3 "$AUDIT" --settings "$EFF/does-not-exist.json" >/dev/null 2>&1
if [ "$?" -eq 2 ]; then
  ok "fail-loud on missing settings.json (exit 2)"
else
  bad "fail-loud on missing settings.json" "expected exit 2"
fi

echo "=== 6. WINDOWS launcher-shim command form is audited end-to-end (MYC-3879) ==="
# On Windows the installer wires EVERY hook through a launcher shim:
#   py -3 "<abs>/scripts/hook_runner.py" --fallback silent "<abs>/<hook>.py"
# The pre-fix resolver matched no part of that shape, so the effective-set audit
# skipped 19 of 25 wired commands on a real box and still printed OK. Worse, where
# `py` is absent the installer falls back to a `python`/`python3` launcher, which
# the old matcher DID resolve -- to the LAUNCHER. hook_runner.py is walk-free, so
# an unguarded corpus walker behind it read as a clean hook. Both legs below.
RUNNER="$REPO_ROOT/scripts/hook_runner.py"
cat > "$EFF/settings_win_bad.json" <<JSON
{"hooks": {"SessionStart": [{"hooks": [
  {"type": "command", "command": "py -3 \"$RUNNER\" --fallback silent \"$EFF/evil-eff.py\""}
]}]}}
JSON
cat > "$EFF/settings_win_py3.json" <<JSON
{"hooks": {"SessionStart": [{"hooks": [
  {"type": "command", "command": "python3 \"$RUNNER\" --fallback silent \"$EFF/evil-eff.py\""}
]}]}}
JSON
cat > "$EFF/settings_win_ok.json" <<JSON
{"hooks": {"SessionStart": [{"hooks": [
  {"type": "command", "command": "py -3 \"$RUNNER\" --fallback silent \"$EFF/good-eff.py\""}
]}]}}
JSON
# The walker is BEHIND the shim. Resolving to the shim (or to nothing) yields
# OK:*; only resolving to the TARGET can produce UNGUARDED. One assertion, both
# failure modes.
for leg in win_bad win_py3; do
  if python3 "$AUDIT" --settings "$EFF/settings_$leg.json" --porcelain 2>/dev/null \
     | grep -q '^UNGUARDED:1:evil-eff.py'; then
    ok "shim form ($leg): unguarded walker behind the launcher is CAUGHT"
  else
    bad "shim form ($leg) caught" \
      "audit resolved the launcher or nothing, not the target hook (MYC-3879 regression)"
  fi
done
# ...and a bounded hook behind the shim resolves and passes, with 0 unresolved:
# a resolver that skipped the whole form would report OK:0:0:1 here.
if python3 "$AUDIT" --settings "$EFF/settings_win_ok.json" --porcelain 2>/dev/null \
   | grep -q '^OK:1:0:0'; then
  ok "shim form: bounded hook resolves (0 unresolved)"
else
  bad "shim form resolves" "expected OK:1:0:0 (a skipped shim form would be OK:0:0:1)"
fi

echo
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
