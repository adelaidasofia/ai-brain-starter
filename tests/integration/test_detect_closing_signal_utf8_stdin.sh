#!/usr/bin/env bash
# Test: an accented close phrase fires the cascade even when the interpreter
# decodes stdin with a non-UTF-8 locale codepage.
#
# Bug class (read side of #314/#483): detect-closing-signal.py read its payload
# with text-mode `sys.stdin.read()`. Claude Code pipes the hook payload as
# UTF-8, but text-mode stdin decodes with the LOCALE codepage — cp1252 on a
# default Windows console. Every non-ASCII character was mangled before the
# patterns saw it, so "cerrar sesión" arrived as "cerrar sesiÃ³n" and matched
# nothing. The close cascade silently never fired for Spanish, Portuguese,
# French, German — any user whose close phrase carries an accent.
#
# It is the read-side twin of the write-side crash guarded at __main__ by #483:
# that guard reconfigures stdout/stderr, never stdin, so this path stayed broken.
# The failure is silent and total — no crash, no log line, just "no close signal"
# forever. It only looked healthy where PYTHONUTF8=1 happened to be set.
#
# Fix under test: read_hook_input() reads sys.stdin.buffer (raw bytes) and
# decodes UTF-8 explicitly, which is locale-independent on every OS.
#
# Control design: PYTHONIOENCODING=cp1252 forces the same text-mode decode on a
# POSIX stream that a Windows console does natively, so this reproduces on CI.
# The UTF-8 leg proves the assertion is not vacuous (it passed before the fix too).
#
# Self-contained: tmpdir fake vault, HOME redirected. Exit 0 = pass, 1 = fail.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/detect-closing-signal.py"
if [ ! -f "$HOOK" ]; then
  echo "ERROR: $HOOK not found" >&2
  exit 1
fi

# Guard the fix itself: if read_hook_input goes back to text-mode stdin, the
# assertions below would silently be testing the wrong code path on a machine
# that happens to run a UTF-8 locale.
if ! grep -q "stdin.buffer" "$HOOK"; then
  echo "ERROR: read_hook_input no longer reads sys.stdin.buffer (fix regressed)" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export HOME="$TMP/fake-home"
export USERPROFILE="$TMP/fake-home"
mkdir -p "$HOME/.claude"

VAULT="$TMP/vault"
mkdir -p "$VAULT/Meta/Sessions" "$VAULT/Meta/Decisions"

PAYLOAD="$TMP/payload.json"

# Write the payload as UTF-8 bytes, exactly as Claude Code pipes it.
write_payload() {
  python3 -c 'import json,sys,io
prompt, cwd, dest = sys.argv[1], sys.argv[2], sys.argv[3]
with io.open(dest, "w", encoding="utf-8") as fh:
    json.dump({"prompt": prompt, "session_id": "utf8-stdin-test", "cwd": cwd}, fh, ensure_ascii=False)
' "$1" "$VAULT" "$PAYLOAD"
}

# Run the hook with a forced stdin/stdout codec.
run_with_encoding() {
  local encoding="$1"
  VAULT_ROOT="$VAULT" \
  CLOSING_SIGNAL_LANGS="en,es,pt" \
  PYTHONIOENCODING="$encoding" \
    python3 "$HOOK" < "$PAYLOAD" 2>/dev/null || true
}

failed=0

# Accented Spanish close phrases: must fire under BOTH codecs.
for phrase in "cerrar sesión" "cerrá la sesión" "hasta mañana"; do
  write_payload "$phrase"
  for encoding in cp1252 utf-8; do
    if ! run_with_encoding "$encoding" | grep -qE "SESSION CLOSE|POSSIBLE SESSION CLOSE"; then
      echo "FAIL [PYTHONIOENCODING=$encoding, should fire]: $phrase" >&2
      failed=$((failed + 1))
    fi
  done
done

# ASCII close phrase: the codec was never the variable here. Proves the test
# harness itself is sound rather than passing for an unrelated reason.
write_payload "chao"
if ! run_with_encoding cp1252 | grep -qE "SESSION CLOSE|POSSIBLE SESSION CLOSE"; then
  echo "FAIL [PYTHONIOENCODING=cp1252, should fire]: chao" >&2
  failed=$((failed + 1))
fi

# A non-close prompt must still not fire under a mangling codec — the fix must
# not turn the decoder into a source of false positives.
write_payload "abrí una sesión nueva de depuración"
if run_with_encoding cp1252 | grep -qE "SESSION CLOSE|POSSIBLE SESSION CLOSE"; then
  echo "FAIL [should NOT fire]: abrí una sesión nueva de depuración" >&2
  failed=$((failed + 1))
fi

if [ "$failed" -gt 0 ]; then
  echo "FAIL: $failed assertion(s) failed" >&2
  exit 1
fi
echo "PASS: accented close phrases fire regardless of the stdin locale codec"
