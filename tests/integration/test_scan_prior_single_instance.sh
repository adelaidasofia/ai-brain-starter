#!/usr/bin/env bash
# Test: scan-prior-sessions-for-secrets.py — the single-instance / no-pile-up
# regression for the 2026-06-05 freeze.
#
# Bug class (PRE-fix): the hook stamped its 6h cooldown marker AFTER the slow
# corpus scan. So every session that started DURING the scan window saw no
# fresh marker and launched its OWN full scan. N concurrent multi-minute scans
# pegged the CPU until the machine froze (load 36). There was no single-instance
# lock, no incremental baseline, no wall-clock budget.
#
# The fix this test guards: a flock single-instance lock + stamp-at-START. A
# second concurrent invocation must back off IMMEDIATELY instead of starting a
# second corpus scan — that is what "a fresh install on a multi-session machine
# cannot pile up a corpus scan" means.
#
# Hermetic: the hook + its _lib are copied into a temp dir, so HOOK_DIR (and
# therefore the lock + markers) live in the temp dir. HOME is pointed at the
# temp dir too, so the scanned corpus is a tiny fixture we control. Nothing
# touches the real ~/.claude or the repo working tree.
#
# Assertions:
#   POSITIVE control (lock free):
#     1. A free run completes, emits valid continue JSON, and PRIMES the
#        incremental baseline (writes .last-secret-scan-full).
#   NEGATIVE control / the regression (lock held by a concurrent "scan"):
#     2. With the lock held, a second invocation backs off: it does NOT
#        re-create the full-pass marker (it never reached the scan), and it
#        still emits valid continue JSON. This is the no-pile-up guarantee.
#
# Exit 0 = pass, exit 1 = fail.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC_HOOK="$REPO_ROOT/hooks/scan-prior-sessions-for-secrets.py"
SRC_LIB="$REPO_ROOT/hooks/_lib"
if [ ! -f "$SRC_HOOK" ] || [ ! -d "$SRC_LIB" ]; then
  echo "FAIL: scan hook or _lib not found under $REPO_ROOT/hooks" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Hermetic hooks dir: HOOK_DIR := $TMP/hooks  (so lock + markers land here).
HOOKS="$TMP/hooks"
mkdir -p "$HOOKS/_lib"
cp "$SRC_HOOK" "$HOOKS/scan-prior-sessions-for-secrets.py"
# safe_read.py joined this list when the hook adopted the cloud-safe read
# primitive (MYC-3537). The hermetic _lib is an explicit allow-list, so a new
# hook dependency that is NOT copied here makes the hook die at import inside
# the fixture only -- green everywhere else, silent here. Pure stdlib, no
# further _lib deps of its own.
cp "$SRC_LIB/__init__.py" "$SRC_LIB/secret_patterns.py" "$SRC_LIB/safe_read.py" "$HOOKS/_lib/"
HOOK="$HOOKS/scan-prior-sessions-for-secrets.py"

# Tiny innocuous corpus under the fake HOME (~/.claude/projects).
mkdir -p "$TMP/.claude/projects/proj"
printf '{"type":"user","content":"hello world, nothing secret here"}\n' \
  > "$TMP/.claude/projects/proj/session.jsonl"

FULL_MARKER="$HOOKS/.last-secret-scan-full"
MARKER="$HOOKS/.last-secret-scan"
LOCK="$HOOKS/.secret-scan.lock"

# shellcheck disable=SC2120  # run_hook optionally takes extra env assignments; some callers pass none
run_hook() {  # args: extra env assignments
  printf '{}' | env HOME="$TMP" SCAN_PRIOR_AUTO_SCRUB_BYPASS=1 "$@" python3 "$HOOK" 2>/dev/null
}

# --- Assertion 1: POSITIVE control — a free run primes the baseline -------
rm -f "$MARKER" "$FULL_MARKER" "$LOCK"
OUT="$(run_hook)"
echo "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("continue") is True, d' \
  || { echo "FAIL: free run did not emit valid continue JSON (got: $OUT)" >&2; exit 1; }
if [ ! -f "$FULL_MARKER" ]; then
  echo "FAIL: free run did not prime the incremental baseline (.last-secret-scan-full missing)" >&2
  exit 1
fi
echo "PASS: free run completes + primes incremental baseline"

# --- Assertion 2: REGRESSION — second concurrent run cannot pile up -------
# Remove the markers so neither the cooldown fast-path nor a primed baseline
# can be what stops the second run. The ONLY thing that may stop it is the
# single-instance lock, held below by a stand-in "concurrent scan".
rm -f "$MARKER" "$FULL_MARKER"

HOME="$TMP" LOCK="$LOCK" HOOK="$HOOK" FULL_MARKER="$FULL_MARKER" python3 - <<'PY'
import fcntl, json, os, subprocess, sys

lock_path = os.environ["LOCK"]
hook = os.environ["HOOK"]
full_marker = os.environ["FULL_MARKER"]
home = os.environ["HOME"]

# Stand-in for a concurrent scan already running: hold the exclusive lock.
fh = open(lock_path, "w")
fcntl.flock(fh, fcntl.LOCK_EX)

env = dict(os.environ, HOME=home, SCAN_PRIOR_AUTO_SCRUB_BYPASS="1")
proc = subprocess.run(["python3", hook], input="{}", capture_output=True, text=True, env=env)

out = proc.stdout.strip()
try:
    d = json.loads(out)
except Exception as exc:
    print(f"FAIL: locked run did not emit valid JSON: {exc} (got: {out!r})", file=sys.stderr)
    sys.exit(1)
if d.get("continue") is not True:
    print(f"FAIL: locked run JSON missing continue:true (got: {d})", file=sys.stderr)
    sys.exit(1)
# The decisive no-pile-up assertion: the backed-off run never reached the scan,
# so it must NOT have written the full-pass marker.
if os.path.exists(full_marker):
    print("FAIL: locked run wrote the full-pass marker — it ran a second scan "
          "instead of backing off (pile-up not prevented)", file=sys.stderr)
    sys.exit(1)
print("PASS: second concurrent run backed off (no second scan, no pile-up)")
fcntl.flock(fh, fcntl.LOCK_UN)
fh.close()
PY

echo
echo "All assertions passed. scan single-instance / no-pile-up invariant holds."
