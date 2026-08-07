#!/usr/bin/env bash
# Negative-control gate for scripts/check-hookify-template-capabilities.py.
#
# That gate exists because the OFFICIAL hookify engine silently returns False for
# an operator it does not implement and None for a field it cannot resolve: a
# template using a capability the official engine lacks LOADS FINE AND NEVER
# FIRES. A safety rule that never fires reads as protection that is not there.
#
# A guard earns trust only by failing on the thing it catches, so this proves:
#   1. GREEN on the real shipped templates (never ship a known-red gate).
#   2. RED on an unsupported operator in a non-allowlisted template.
#   3. RED on an unsupported field for the template's event.
#   4. RED on a STALE allowlist entry (the debt ledger self-cleans when the
#      upstream fix lands and the violation disappears).
#   5. GREEN for an allowlisted template that still violates (fix in flight).
#
# Stdlib python3 + bash only (no PyYAML, no network, no git). Tmpdir on exit.
# Run: bash tests/integration/test_hookify_template_capabilities.sh  (0 = pass)
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
GATE="$REPO_ROOT/scripts/check-hookify-template-capabilities.py"
TPL_DIR="$REPO_ROOT/templates/hookify-rules"
ALLOWLISTED="hookify.warn-rotation-push-on-local-only-leak.local.md"

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1 :: ${2:-}"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

[ -f "$GATE" ] || { echo "FAIL  gate script missing: $GATE"; exit 1; }

# Build a throwaway repo layout the gate can resolve (script at <root>/scripts).
stage() { # $1 = dest root
  mkdir -p "$1/scripts" "$1/templates/hookify-rules"
  cp "$GATE" "$1/scripts/"
  cp "$TPL_DIR"/hookify.*.local.md "$1/templates/hookify-rules/"
}

run_gate() { python3 -S "$1/scripts/$(basename "$GATE")" 2>&1; }

# --- 1. green on the real repo -------------------------------------------------
if out="$(python3 -S "$GATE" 2>&1)"; then
  ok "real shipped templates pass the gate (green baseline)"
else
  bad "real shipped templates pass the gate" "$(echo "$out" | tail -3)"
fi

# --- 2. red on an unsupported operator -----------------------------------------
stage "$TMP/op"
cat > "$TMP/op/templates/hookify-rules/hookify.zz-bad-operator.local.md" <<'EOF'
---
name: zz-bad-operator
enabled: true
event: file
action: warn
conditions:
  - field: file_path
    operator: regex_not_match
    pattern: \.md$
---
should be caught
EOF
out="$(run_gate "$TMP/op")"; rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -q "zz-bad-operator"; then
  ok "unsupported operator in a new template is caught (exit $rc)"
else
  bad "unsupported operator is caught" "rc=$rc"
fi

# --- 3. red on an unsupported field --------------------------------------------
stage "$TMP/fld"
cat > "$TMP/fld/templates/hookify-rules/hookify.zz-bad-field.local.md" <<'EOF'
---
name: zz-bad-field
enabled: true
event: stop
action: warn
conditions:
  - field: assistant_response
    operator: contains
    pattern: whatever
---
should be caught
EOF
out="$(run_gate "$TMP/fld")"; rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -q "zz-bad-field"; then
  ok "unsupported field for the event is caught (exit $rc)"
else
  bad "unsupported field is caught" "rc=$rc"
fi

# --- 4. red on a stale allowlist entry -----------------------------------------
stage "$TMP/stale"
rm -f "$TMP/stale/templates/hookify-rules/$ALLOWLISTED"
out="$(run_gate "$TMP/stale")"; rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -qi "stale"; then
  ok "stale allowlist entry is caught (ledger self-cleans, exit $rc)"
else
  bad "stale allowlist entry is caught" "rc=$rc"
fi

# --- 5. allowlisted violation stays green --------------------------------------
stage "$TMP/alw"
out="$(run_gate "$TMP/alw")"; rc=$?
if [ $rc -eq 0 ] && echo "$out" | grep -q "KNOWN upstream gaps"; then
  ok "allowlisted in-flight violation reported but not fatal"
else
  bad "allowlisted violation stays green" "rc=$rc"
fi

echo
echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
