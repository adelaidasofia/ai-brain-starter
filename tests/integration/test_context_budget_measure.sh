#!/usr/bin/env bash
# CI integration gate — context-budget-measure.py (MYC-619).
# Runs the hook's built-in --self-test (positive over-ceiling fires, negative lean
# stays silent, drift warns, tolerance absorbs tiny adds, e2e temp-home detection),
# then a black-box check that hook-mode is SILENT on a healthy synthetic HOME and
# WARNS on an over-ceiling synthetic global CLAUDE.md. Self-contained; no real ~/.claude.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
HOOK="$ROOT/hooks/context-budget-measure.py"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$HERE/lib/sandbox_home.sh"

[[ -f "$HOOK" ]] || { echo "FAIL: hook not found: $HOOK"; exit 1; }

# 1. Built-in self-test (positive + negative + drift + tolerance + e2e).
python3 "$HOOK" --self-test

# 2. Black-box: silent on a healthy temp HOME (no over-ceiling file, fresh baseline).
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/.claude"
printf 'tiny global rules\n' > "$TMP/.claude/CLAUDE.md"
OUT="$(printf '{"cwd":"%s"}' "$TMP" | run_sandboxed "$TMP" env CLAUDE_PROJECT_DIR="$TMP" python3 "$HOOK")"
if echo "$OUT" | grep -q 'additionalContext'; then
  echo "FAIL: healthy HOME should be silent, got: $OUT"; exit 1
fi
echo "OK: silent on healthy HOME"

# 3. Black-box: warns on an over-ceiling global CLAUDE.md.
python3 -c "open('$TMP/.claude/CLAUDE.md','w').write('x'*41000)"
rm -f "$TMP/.claude/.context-budget-baseline.json" "$TMP/.claude/.context-budget-last-warn"
OUT="$(printf '{"cwd":"%s"}' "$TMP" | run_sandboxed "$TMP" env CLAUDE_PROJECT_DIR="$TMP" python3 "$HOOK")"
if echo "$OUT" | grep -q 'over the'; then
  echo "OK: warns on over-ceiling global CLAUDE.md"
else
  echo "FAIL: over-ceiling global should warn, got: $OUT"; exit 1
fi

echo "PASS: test_context_budget_measure"
