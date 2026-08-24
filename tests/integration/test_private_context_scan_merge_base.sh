#!/usr/bin/env bash
# Pins the private-context gate to a MERGE-BASE diff and to its delegation.
#
# The bug this exists to prevent (measured 2026-08-23, PR #519): lint.yml scanned
# `git diff origin/main..HEAD` -- two dots, which compares TIPS. On a branch 52
# commits behind, every line main had changed came back as a `+` on the PR side.
# The gate reported 165 files and 10 private-token hits on a PR that touches 3
# files and adds none. ~15 PRs sat blocked on that false positive for weeks.
#
# A false positive on a SECURITY gate is not a cosmetic bug: it is the failure
# mode that teaches an operator to reach for --admin.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCANNER="$ROOT/scripts/check-private-context.sh"
WF="$ROOT/.github/workflows/lint.yml"
PASS=0 FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

echo "== the scanner exists and its own negative controls hold =="
[ -f "$SCANNER" ] && pass "scripts/check-private-context.sh present" \
  || fail "scripts/check-private-context.sh missing"
if bash "$SCANNER" --self-test >/dev/null 2>&1; then
  pass "scanner self-test green (catches a real token, ignores a stale base)"
else
  echo "--- scanner self-test output ---"; bash "$SCANNER" --self-test || true
  fail "scanner self-test FAILED"
fi

echo "== the workflow delegates instead of re-inlining a private copy =="
if grep -q 'bash scripts/check-private-context.sh "origin/\$BASE_REF"' "$WF"; then
  pass "lint.yml calls the shared scanner"
else
  fail "lint.yml no longer delegates to scripts/check-private-context.sh"
fi
if grep -q 'check-private-context.sh --self-test' "$WF"; then
  pass "lint.yml runs the negative control in CI"
else
  fail "lint.yml dropped the negative-control step"
fi

echo "== no two-dot diff against the base survives anywhere in the gate =="
# `"$BASE"..HEAD` is the exact defect. `...` must never regress to `..`.
if grep -nE '"\$BASE"\.\.[^.]' "$SCANNER" "$WF" 2>/dev/null; then
  fail "a TWO-DOT diff against the base is back -- stale branches will false-positive again"
else
  pass "no two-dot base diff in the scanner or the workflow"
fi

echo "== the base fetch stays deep enough to HAVE a merge base =="
# A depth-limited fetch re-shallows the checkout; on a PR older than that many
# commits the merge base falls outside the boundary and the diff cannot compute.
if grep -nE 'git fetch origin "\$BASE_REF".*--depth' "$WF" 2>/dev/null; then
  fail "base fetch is depth-limited again -- old PRs will lose their merge base"
else
  pass "base fetch is unbounded (merge base always reachable)"
fi

echo "=== $(basename "$0"): $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
