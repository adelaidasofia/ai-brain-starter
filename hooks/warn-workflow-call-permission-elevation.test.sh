#!/usr/bin/env bash
# Negative controls for warn-workflow-call-permission-elevation.py.
#
# A gate earns trust only by failing on the thing it catches. The load-bearing
# case is REVERSE (case 2): the real-world way this bug is born is editing the
# CALLEE to add a permission, which breaks a DIFFERENT file's pipeline. A
# forward-only implementation passes case 1 and silently fails case 2.
set -uo pipefail

HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/warn-workflow-call-permission-elevation.py"
pass=0; fail=0

check() { # name expect_warn actual_output
  local name="$1" expect="$2" out="$3"
  local got="quiet"
  printf '%s' "$out" | grep -q 'WORKFLOW PERMISSION ELEVATION' && got="warn"
  if [ "$got" = "$expect" ]; then
    printf '  ok   %-58s (%s)\n' "$name" "$got"; pass=$((pass+1))
  else
    printf '  FAIL %-58s expected %s, got %s\n' "$name" "$expect" "$got"; fail=$((fail+1))
    printf '%s\n' "$out" | sed 's/^/       /'
  fi
}

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
WF="$TMP/.github/workflows"; mkdir -p "$WF"

CALLER_TIGHT='name: release
on: {push: {tags: ["v*"]}}
permissions:
  contents: write
jobs:
  gate:
    uses: ./.github/workflows/smoke.yml
'
CALLER_GRANTS='name: release
on: {push: {tags: ["v*"]}}
permissions:
  contents: write
jobs:
  gate:
    permissions:
      contents: read
      pull-requests: read
    uses: ./.github/workflows/smoke.yml
'
CALLEE_SAFE='name: smoke
on: {workflow_call: null}
permissions:
  contents: read
jobs:
  w:
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
'
CALLEE_ELEVATED='name: smoke
on: {workflow_call: null}
permissions:
  contents: read
  pull-requests: read
jobs:
  w:
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
'

run_write() { # file_path content
  printf '{"tool_name":"Write","tool_input":{"file_path":"%s","content":%s}}' \
    "$1" "$(python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))' <<<"$2")" \
    | python3 "$HOOK" 2>/dev/null
}

# 1 FORWARD: writing a caller whose existing callee already asks for more.
printf '%s' "$CALLEE_ELEVATED" > "$WF/smoke.yml"
check "forward: caller written against elevated callee" warn \
  "$(run_write "$WF/release.yml" "$CALLER_TIGHT")"

# 2 REVERSE (the real incident shape): the caller is already on disk and fine;
#   editing the CALLEE to add a scope breaks it. Forward-only misses this.
printf '%s' "$CALLER_TIGHT" > "$WF/release.yml"
rm -f "$WF/smoke.yml"
check "reverse: callee edited to add an ungranted scope" warn \
  "$(run_write "$WF/smoke.yml" "$CALLEE_ELEVATED")"

# 3 POSITIVE: a correct per-job grant must stay quiet.
printf '%s' "$CALLER_GRANTS" > "$WF/release.yml"
check "caller grants it per-job -> quiet" quiet \
  "$(run_write "$WF/smoke.yml" "$CALLEE_ELEVATED")"

# 4 POSITIVE: a callee within the caller's grant must stay quiet.
printf '%s' "$CALLER_TIGHT" > "$WF/release.yml"
check "callee within caller's grant -> quiet" quiet \
  "$(run_write "$WF/smoke.yml" "$CALLEE_SAFE")"

# 5 SCOPE: a non-workflow file must never trigger it.
check "non-workflow path ignored" quiet \
  "$(run_write "$TMP/notes.yml" "$CALLEE_ELEVATED")"

# 6 BYPASS honoured.
printf '%s' "$CALLER_TIGHT" > "$WF/release.yml"
check "bypass env honoured" quiet \
  "$(WORKFLOW_PERMS_BYPASS=1 run_write "$WF/smoke.yml" "$CALLEE_ELEVATED")"

# 7 Callee that declares NO permissions inherits -> never an elevation.
check "callee with no permissions block -> quiet" quiet \
  "$(run_write "$WF/smoke.yml" 'name: smoke
on: {workflow_call: null}
jobs:
  w:
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
')"

# 8 Unparseable YAML must not break the user's edit.
check "unparseable yaml -> quiet, never breaks the edit" quiet \
  "$(run_write "$WF/smoke.yml" ':::not: [valid')"

# 9 EMISSION CHANNEL. Every hooks.json command in this repo ends in
#   `2>/dev/null || echo <allow>`, so stderr is discarded before Claude Code
#   sees it. A warning must therefore arrive on STDOUT as valid
#   hookSpecificOutput.additionalContext JSON. run_write already captures
#   stdout only (cases 1+2 are the behavioural half of this control); this
#   asserts the payload SHAPE, so a revert to a bare stderr write -- or to
#   plain unstructured text on stdout -- fails here instead of shipping mute.
printf '%s' "$CALLER_TIGHT" > "$WF/release.yml"
emit="$(run_write "$WF/smoke.yml" "$CALLEE_ELEVATED")"
if printf '%s' "$emit" | python3 -c '
import json,sys
d = json.load(sys.stdin)
ctx = d["hookSpecificOutput"]["additionalContext"]
assert d["hookSpecificOutput"]["hookEventName"] == "PreToolUse", "wrong hookEventName"
assert "WORKFLOW PERMISSION ELEVATION" in ctx, "diagnostic missing from additionalContext"
' 2>/dev/null; then
  printf '  ok   %-58s (%s)\n' "warns via stdout hookSpecificOutput JSON" "json"; pass=$((pass+1))
else
  printf '  FAIL %-58s %s\n' "warns via stdout hookSpecificOutput JSON" \
    "stdout is not additionalContext JSON -- the wiring discards stderr, so this ships MUTE"
  printf '%s\n' "$emit" | sed 's/^/       /'; fail=$((fail+1))
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "warn-workflow-call-permission-elevation: ${pass}/${pass} passed"
  exit 0
fi
echo "warn-workflow-call-permission-elevation: ${fail} FAILED, ${pass} passed"
exit 1
