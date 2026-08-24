#!/usr/bin/env bash
# Negative-controlled test for the decode-class fail-open (MYC-4012).
#
# `UnicodeDecodeError` subclasses `ValueError`, not `OSError`. A guard that reads
# a file inside `try: ... except OSError:` therefore lets it straight through,
# `hook_runner` masks the traceback, and the hook answers
# `{"continue": true, "suppressOutput": true}` — a guard whose job is to REFUSE
# silently ALLOWS. Nothing anywhere says so.
#
# The proof has to be a real decode failure through a real wired guard, not the
# scanner agreeing with itself. pre-write-settings-lint.py is the sharpest case
# in the fleet: it is a PreToolUse BLOCKER, so its failure mode is letting a
# corrupt config be written. Exit 2 is deny; anything else is allow. The bytes
# are genuine cp1252 (0x93, a smart quote as Windows Notepad writes it), which is
# invalid UTF-8 on every host — so this reproduces without needing a Windows
# runner, and does not depend on the ambient locale.
#
# THE CONTROL THAT MATTERS is section 4: the same payload is run against a
# pre-fix COPY of the guard, and must NOT deny. Without it, section 3 would pass
# just as happily against a guard that never had the bug.
#
# Run: bash scripts/test-decode-safe-reads.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; fails=$((fails+1)); }

# A settings.json that is BOTH undecodable as UTF-8 and genuinely invalid
# (a duplicate top-level key). The guard must still say no.
write_cp1252_settings() {  # $1 = path
  P="$1" python3 -c '
import os
raw = (b"{\n"
       b"  \"permissions\": {\"allow\": [\"Bash(ls)\"]},\n"
       b"  \"note\": \"caf\x93 smart quote from Notepad\",\n"
       b"  \"permissions\": {\"deny\": []}\n"
       b"}\n")
open(os.environ["P"], "wb").write(raw)'
}

edit_payload() {  # $1 = settings path
  P="$1" python3 -c '
import json, os
print(json.dumps({
    "tool_name": "Edit",
    "tool_input": {
        "file_path": os.environ["P"],
        "old_string": "Bash(ls)",
        "new_string": "Bash(pwd)",
    },
}))'
}

# =============================================================================
echo "--- 1. the scanner discriminates ------------------------------------------"
if python3 "$ROOT/scripts/audit-decode-safe-reads.py" --selftest >/dev/null 2>&1; then
  pass "audit self-test: bites every defective shape, clean on every safe one"
else
  fail "audit self-test failed — the scanner cannot be trusted"
  python3 "$ROOT/scripts/audit-decode-safe-reads.py" --selftest 2>&1 | grep FAIL
fi

echo "--- 2. the shipped hook fleet is clean ------------------------------------"
if out="$(python3 "$ROOT/scripts/audit-decode-safe-reads.py" --all 2>&1)"; then
  pass "no shipped hook reads text under a handler that misses the decode class"
else
  fail "shipped hooks still carry the defect:"
  echo "$out" | grep FAIL | head -20
fi

echo "--- 3. a real cp1252 read through a real wired BLOCKER --------------------"
S="$TMP/.claude/settings.json"; mkdir -p "$TMP/.claude"
write_cp1252_settings "$S"
python3 -c 'import sys; sys.exit(0 if open(sys.argv[1],"rb").read().find(b"\x93") >= 0 else 1)' "$S" \
  && pass "fixture really contains an undecodable byte" \
  || fail "fixture is decodable — the control proves nothing"

PAYLOAD="$(edit_payload "$S")"
printf '%s' "$PAYLOAD" | python3 "$ROOT/hooks/pre-write-settings-lint.py" >/dev/null 2>&1; rc=$?
[ "$rc" = 2 ] \
  && pass "the blocker still DENIES a duplicate key in an undecodable settings.json (exit 2)" \
  || fail "the blocker did not deny (exit $rc) — the decode error made it fail open"

# ...and under an ASCII locale, which is what turns a plain read_text() into a
# decode error even for content that IS valid UTF-8.
printf '%s' "$PAYLOAD" | env -u PYTHONUTF8 LC_ALL=C LANG=C PYTHONCOERCECLOCALE=0 \
  python3 "$ROOT/hooks/pre-write-settings-lint.py" >/dev/null 2>&1; rc=$?
[ "$rc" = 2 ] && pass "still denies under an ASCII locale (LC_ALL=C)" \
              || fail "an ASCII locale made the blocker fail open (exit $rc)"

# a VALID settings.json must still be allowed — or section 3 passes on a guard
# that simply denies everything
GOOD="$TMP/good/.claude/settings.json"; mkdir -p "$TMP/good/.claude"
printf '{\n  "permissions": {"allow": ["Bash(ls)"]}\n}\n' > "$GOOD"
edit_payload "$GOOD" | python3 "$ROOT/hooks/pre-write-settings-lint.py" >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] && pass "a valid settings.json is still allowed (exit 0) — it is not deny-everything" \
              || fail "the blocker denied a valid config (exit $rc) — over-blocking"

echo "--- 4. THE CONTROL: the same test against the pre-fix guard ---------------"
# Rebuild the defect exactly as it shipped — a bare read_text() under
# `except OSError` — and prove this test goes red on it. A control that cannot
# fail proves nothing about the code that passes it.
mkdir -p "$TMP/prefix"
cp "$ROOT/hooks/pre-write-settings-lint.py" "$TMP/prefix/hook.py"
python3 - "$TMP/prefix/hook.py" <<'PY'
import io, sys
p = sys.argv[1]
s = io.open(p, encoding="utf-8").read()
old = 'current = p.read_text(encoding="utf-8", errors="replace")'
if old not in s:
    print("CONTROL-BROKEN: could not find the fixed read to revert", file=sys.stderr)
    raise SystemExit(1)
io.open(p, "w", encoding="utf-8").write(s.replace(old, "current = p.read_text()"))
PY
if [ $? -ne 0 ]; then
  fail "could not build the pre-fix control — the assertion below would be vacuous"
else
  printf '%s' "$PAYLOAD" | python3 "$TMP/prefix/hook.py" >/dev/null 2>&1; rc=$?
  [ "$rc" != 2 ] \
    && pass "pre-fix guard FAILS OPEN on the same input (exit $rc, not deny) — the bug was real" \
    || fail "pre-fix guard also denied — this test does not actually detect the defect"
fi

# and the scanner must flag that pre-fix source
python3 "$ROOT/scripts/audit-decode-safe-reads.py" --check "$TMP/prefix/hook.py" >/dev/null 2>&1; rc=$?
[ "$rc" = 1 ] && pass "the scanner flags the pre-fix source (exit 1)" \
             || fail "the scanner did NOT flag the known-defective source (exit $rc)"

echo "--- 5. the lint engine itself survives undecodable input ------------------"
out="$(python3 "$ROOT/hooks/lint-claude-settings.py" --paths "$S" 2>&1)"; rc=$?
case "$out" in
  *BLOCK*|*INVALID*|*duplicate*|*DUP*) pass "the lint engine still reports on an undecodable file" ;;
  *) if [ "$rc" -gt 1 ]; then
       fail "the lint engine crashed on an undecodable file (exit $rc): $out"
     else
       pass "the lint engine handled an undecodable file without crashing (exit $rc)"
     fi ;;
esac

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; exit 0; fi
echo "$fails FAILING"; exit 1
