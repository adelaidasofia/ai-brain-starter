#!/usr/bin/env bash
# Test: surface-stale-automation-failures.py -- launchd HOLLOW-job detection.
#
# Bug class: `launchctl list` column 2 (the last exit status) reads "0" for
# BOTH a genuinely healthy job (ran, exited clean) AND a job launchd has
# REGISTERED but has NEVER EXECUTED. Measured 2026-08-20 on a real install:
#
#     launchctl list        ->  -   0   <label>      # reads as CLEAN
#     launchctl print ...   ->  state = not running | runs = 0 | last exit code = (never exited)
#
# The pre-fix `launchd_failures()` read ONLY `launchctl list`, so a hollow job
# was indistinguishable from a healthy one and was never flagged -- a real job
# sat dead, unnoticed, for days behind exactly this blind spot. The fix adds a
# second, slower probe (`launchctl print gui/<uid>/<label>`) for the AMBIGUOUS
# status==0 case only, and reports a hollow job as a finding DISTINCT from a
# failing one, because the remedy differs: bootout+bootstrap (reload it) vs.
# reading the error log.
#
# `launchctl` is stubbed on PATH -- the real binary is never touched, and this
# suite must never load/bootstrap/kickstart/unload any real launchd job. One
# stub handles both subcommands the hook calls:
#   `launchctl list`                    -> a fixed PID/status/label table
#   `launchctl print gui/<uid>/<label>` -> per-label canned output, branching
#                                          on the label suffix so the uid
#                                          launchd_failures() computes at
#                                          runtime never has to be known here
# Same PATH-stub convention as test_hook_vault_root_per_target.sh case 8.
#
# Assertions:
#   1. PRE-FIX PROOF (the negative control that proves the fix). An
#      independent, literal reimplementation of the OLD decision rule --
#      "read only the `list` status column; flag iff it parses as a non-zero
#      int" -- applied to the SAME raw `launchctl list` stub rows, does NOT
#      flag the hollow-job label. This is deliberately NOT a diff against git
#      history: after this fix lands on main, "before" and "after" would be
#      the same file and a history-based comparison would stop proving
#      anything. A frozen, independent reimplementation keeps meaning the
#      same thing forever.
#   2-5. END-TO-END, one real hook invocation, four labels in one fixture:
#      2. hollow job      (list status 0, print runs=0)     -> flagged, distinctly
#      3. failing job     (list status 9)                   -> still flagged (no regression)
#      4. healthy job     (list status 0, print runs=42)    -> NOT flagged (no false positive)
#      5. flaky-print job (list status 0, print errors out) -> NOT flagged, hook does not crash
#
# Self-contained. Exit 0 = pass, exit 1 = fail.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/surface-stale-automation-failures.py"
# HOME alone does NOT sandbox on Windows: Path.home() reads USERPROFILE there,
# so a bare HOME= override runs against the developer's REAL ~/.claude (MYC-3536).
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
if [ ! -f "$HOOK" ]; then
  echo "FAIL: hook not found at $HOOK" >&2
  exit 1
fi

FAILURES=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1" >&2; FAILURES=$((FAILURES + 1)); }
show_streams() {
  echo "  --- stdout ---" >&2; sed 's/^/  /' "$OUT" >&2
  echo "  --- stderr ---" >&2; sed 's/^/  /' "$ERR" >&2
}

TMP="$(python3 -c 'import os,tempfile; print(os.path.realpath(tempfile.mkdtemp(prefix="abs-launchd-hollow-")).replace(chr(92), "/"))')"
if [ -z "$TMP" ] || [ ! -d "$TMP" ]; then
  echo "FAIL: could not create a tmpdir python and the shell both agree on" >&2
  exit 1
fi
trap 'rm -rf "$TMP"' EXIT
FIXTURE="$TMP/home"
STUB="$TMP/stub"
OUT="$TMP/out.txt"
ERR="$TMP/err.txt"
mkdir -p "$FIXTURE/Library/LaunchAgents" "$FIXTURE/.claude" "$STUB"

# --------------------------------------------------------------------------
# Fixture: four labels, one story each. `user_launchd_labels()` only reports
# labels with a matching plist in LaunchAgents/, so every label needs one --
# content is irrelevant, the glob only reads the filename stem.
# --------------------------------------------------------------------------
for label in com.example.hollow-job com.example.failing-job com.example.healthy-job com.example.flaky-print-job; do
  printf '<plist/>\n' > "$FIXTURE/Library/LaunchAgents/$label.plist"
done

# The fake `launchctl`. Quoted heredoc delimiter -- the $1/$2/case syntax below
# must reach the stub file literally, not be expanded by THIS shell.
cat > "$STUB/launchctl" <<'LAUNCHCTL_STUB'
#!/bin/sh
case "$1" in
  list)
    printf -- '-\t0\tcom.example.hollow-job\n'
    printf -- '-\t9\tcom.example.failing-job\n'
    printf -- '12345\t0\tcom.example.healthy-job\n'
    printf -- '-\t0\tcom.example.flaky-print-job\n'
    ;;
  print)
    case "$2" in
      */com.example.hollow-job)
        printf 'com.example.hollow-job = {\n'
        printf '\tactive count = 0\n'
        printf '\tstate = not running\n'
        printf '\truns = 0\n'
        printf '\tlast exit code = (never exited)\n'
        printf '}\n'
        ;;
      */com.example.healthy-job)
        printf 'com.example.healthy-job = {\n'
        printf '\tstate = not running\n'
        printf '\truns = 42\n'
        printf '\tlast exit code = 0\n'
        printf '}\n'
        ;;
      *)
        # com.example.failing-job never reaches `print` (its list status is
        # already unambiguous); com.example.flaky-print-job lands here on
        # purpose -- this is the probe-errors-out case.
        echo "Could not find service" >&2
        exit 1
        ;;
    esac
    ;;
  *)
    exit 1
    ;;
esac
LAUNCHCTL_STUB
chmod +x "$STUB/launchctl"

# --------------------------------------------------------------------------
# Assertion 1: PRE-FIX PROOF. See the file header for why this is a frozen,
# independent reimplementation rather than a git-history diff.
# --------------------------------------------------------------------------
python3 - <<'PY'
import sys

ROWS = [
    ("-", "0", "com.example.hollow-job"),
    ("-", "9", "com.example.failing-job"),
    ("12345", "0", "com.example.healthy-job"),
    ("-", "0", "com.example.flaky-print-job"),
]


def pre_fix_flagged(rows):
    """The OLD decision rule, frozen: read ONLY the `launchctl list` status
    column, flag iff it parses as a non-zero int. Verbatim shape of what
    launchd_failures() did before the hollow-job probe existed."""
    flagged = set()
    for _pid, status, label in rows:
        try:
            code = int(status)
        except ValueError:
            continue
        if code != 0:
            flagged.add(label)
    return flagged


flagged = pre_fix_flagged(ROWS)
failures = []
if "com.example.hollow-job" in flagged:
    failures.append(
        "fixture bug: the pre-fix rule already flags the hollow job "
        f"({flagged}) -- this control would be vacuous"
    )
if "com.example.failing-job" not in flagged:
    failures.append(
        "fixture bug: the pre-fix rule does not flag the failing job either "
        "-- check the ROWS fixture"
    )
if failures:
    for f in failures:
        print("FAIL: " + f, file=sys.stderr)
    sys.exit(1)
PY
if [ $? -eq 0 ]; then
  pass "pre-fix proof: reading only launchctl-list status reads the hollow job as clean (the blind spot the fix closes)"
else
  fail "pre-fix proof control did not hold -- see stderr above"
fi

# --------------------------------------------------------------------------
# Assertions 2-5: END-TO-END against the real (post-fix) hook.
# --------------------------------------------------------------------------
run_hook() {  # <stdin-json> -> sets RC, writes $OUT / $ERR
  printf '%s' "$1" | env -u VAULT_ROOT -u SURFACE_STALE_AUTOMATION_BYPASS -u CLAUDE_CWD \
    HOME="$FIXTURE" USERPROFILE="$(_sandbox_native_path "$FIXTURE")" HOMEDRIVE="" HOMEPATH="" \
    PATH="$STUB:$PATH" \
    python3 "$HOOK" >"$OUT" 2>"$ERR"
  RC=$?
}

verdict() {  # <out-file> -> prints the systemMessage text, or EMPTY/UNPARSEABLE(...)
  python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path

raw = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
if not raw:
    print("EMPTY")
    raise SystemExit(0)
try:
    msg = json.loads(raw).get("systemMessage", "")
except Exception as exc:
    print(f"UNPARSEABLE({exc})")
    raise SystemExit(0)
print(msg)
PY
}

PAYLOAD="$(python3 -c 'import json,sys; print(json.dumps({"cwd":sys.argv[1]}))' "$TMP")"
run_hook "$PAYLOAD"
MSG="$(verdict "$OUT")"

if [ "$RC" -ne 0 ]; then
  fail "hook exited $RC (expected 0 -- this hook is fail-open by design on every path)"
  show_streams
else
  pass "hook exits 0 across all four fixture labels"
fi

if grep -qi "Traceback" "$ERR"; then
  fail "hook printed a Python traceback to stderr -- the print-probe is not fail-open"
  sed 's/^/  /' "$ERR" >&2
else
  pass "no crash / traceback across all four fixture labels (fail-open holds end to end)"
fi

if printf '%s' "$MSG" | grep -q "com.example.hollow-job" && printf '%s' "$MSG" | grep -q "NEVER RUN"; then
  pass "hollow job (runs=0) is flagged as hollow -- the fix's core claim"
else
  fail "hollow job (runs=0) was not flagged as hollow"
  echo "  systemMessage=[$MSG]" >&2
fi

if printf '%s' "$MSG" | grep -q "com.example.failing-job" && printf '%s' "$MSG" | grep -q "exited 9"; then
  pass "a genuinely failing job (non-zero exit) is still flagged -- no regression"
else
  fail "the failing job's non-zero-exit finding regressed"
  echo "  systemMessage=[$MSG]" >&2
fi

# The hollow and failing findings must read as DISTINCT states -- that's the
# whole point of the fix (different remedies). Only the plain non-zero-exit
# finding uses the "(launchctl status)" suffix; the hollow finding legitimately
# says "never exited" as diagnostic detail, so the bare word "exited" is not a
# safe discriminator -- the literal "(launchctl status)" suffix is.
if printf '%s' "$MSG" | grep -F "com.example.hollow-job" | grep -qF "(launchctl status)"; then
  fail "the hollow-job finding uses the plain-failure '(launchctl status)' phrasing -- not distinct"
  echo "  systemMessage=[$MSG]" >&2
else
  pass "the hollow finding is worded distinctly from a generic exit-status failure"
fi

if printf '%s' "$MSG" | grep -q "com.example.healthy-job"; then
  fail "a healthy job (runs>=1, exit 0) was flagged -- false positive"
  echo "  systemMessage=[$MSG]" >&2
else
  pass "a healthy job (runs>=1, exit 0) is not flagged -- no false positive"
fi

if printf '%s' "$MSG" | grep -q "com.example.flaky-print-job"; then
  fail "a label whose print probe errors out was flagged -- should degrade silently"
  echo "  systemMessage=[$MSG]" >&2
else
  pass "a label whose print probe errors out degrades silently (not flagged, no crash)"
fi

if [ "$FAILURES" -eq 0 ]; then
  echo "All assertions passed. Launchd hollow-job detection holds (pos+neg control)."
  exit 0
else
  echo "$FAILURES assertion(s) failed." >&2
  exit 1
fi
