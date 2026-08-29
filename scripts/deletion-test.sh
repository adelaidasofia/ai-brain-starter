#!/usr/bin/env bash
# exit-contract: ADVISORY -- an Ousterhout heuristic offered as a structural
#   signal for a human, explicitly not an architectural verdict

# deletion-test.sh — Ousterhout's deletion test for a candidate file.
# Usage:
#   bash deletion-test.sh <file-path> [search-root]
#
# Example:
#   bash deletion-test.sh ./src/lib/some-helper.ts .
#
# Outputs a verdict on whether the module is DEEP (worth keeping) or SHALLOW (candidate for deletion):
#   - Counts callers (rg for imports/requires of the file)
#   - Reports LOC of the file
#   - Reports total LOC of caller files (rough proxy for distributed complexity)
#   - Verdict: PASS if file LOC < 0.5 * caller LOC sum (concentrates complexity);
#              FAIL if file LOC > 1.5 * caller LOC sum (just moves complexity)
#
# Heuristic, not a proof. Reads as "structural signal," not architectural verdict.

set -euo pipefail

FILE="${1:-}"
ROOT="${2:-$(dirname "$FILE")}"

if [[ -z "$FILE" || ! -f "$FILE" ]]; then
  echo "Usage: bash deletion-test.sh <file-path> [search-root]" >&2
  exit 2
fi

# Derive a stem to search for (filename without extension)
BASENAME=$(basename "$FILE")
STEM="${BASENAME%.*}"

# Find caller files: anything that imports or requires this file's stem.
# Use POSIX grep -r (always in PATH; rg may be a shell-function alias not visible to scripts).
# Search common code extensions, excluding obvious non-source dirs.
CALLERS=$(grep -rl \
  --include='*.ts' --include='*.tsx' --include='*.js' --include='*.jsx' \
  --include='*.mjs' --include='*.cjs' --include='*.py' --include='*.go' --include='*.rb' \
  --exclude-dir=node_modules --exclude-dir=dist --exclude-dir=.next --exclude-dir=build --exclude-dir=.git \
  -e "${STEM}" \
  "$ROOT" 2>/dev/null | grep -v "^${FILE}$" || true)
# Narrow to files that actually IMPORT (not just mention) the stem.
CALLERS=$(echo "$CALLERS" | while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  if grep -qE "(import|require|from)[[:space:]].*['\"\`].*${STEM}" "$f" 2>/dev/null; then
    echo "$f"
  fi
done)

CALLER_COUNT=$(echo "$CALLERS" | grep -c . || true)
FILE_LOC=$(wc -l < "$FILE" | tr -d ' ')

if [[ "$CALLER_COUNT" -eq 0 ]]; then
  echo "deletion-test: $FILE"
  echo "  callers:    0"
  echo "  verdict:    DEAD CODE — no callers found. Safe to delete (or it is a top-level entry point)."
  exit 0
fi

CALLER_LOC=0
while IFS= read -r caller; do
  [[ -z "$caller" ]] && continue
  CL=$(wc -l < "$caller" | tr -d ' ')
  CALLER_LOC=$((CALLER_LOC + CL))
done <<< "$CALLERS"

# Compute ratio (file_loc / caller_loc_sum)
# Use python for clean math
RATIO=$(python3 -c "print(f'{$FILE_LOC / max($CALLER_LOC, 1):.3f}')")

echo "deletion-test: $FILE"
echo "  file LOC:           $FILE_LOC"
echo "  callers:            $CALLER_COUNT"
echo "  total caller LOC:   $CALLER_LOC"
echo "  ratio (file/calls): $RATIO"
echo

# Verdict
RATIO_PASS=$(FL=$FILE_LOC CL=$CALLER_LOC python3 - <<'PY'
import os
print(1 if int(os.environ["FL"]) < 0.5 * int(os.environ["CL"]) else 0)
PY
)
RATIO_FAIL=$(FL=$FILE_LOC CL=$CALLER_LOC python3 - <<'PY'
import os
print(1 if int(os.environ["FL"]) > 1.5 * int(os.environ["CL"]) else 0)
PY
)

if [[ "$RATIO_PASS" -eq 1 ]]; then
  echo "  verdict:    DEEP (PASS) — deleting would concentrate complexity across $CALLER_COUNT callers."
  echo "              Module is earning its keep. Keep it."
elif [[ "$RATIO_FAIL" -eq 1 ]]; then
  echo "  verdict:    SHALLOW (FAIL) — file LOC > 1.5x caller sum suggests pass-through."
  echo "              Candidate for deletion or deepening per architecture-pass."
else
  echo "  verdict:    AMBIGUOUS — ratio is $RATIO. Apply judgment, not heuristic."
  echo "              Read the actual interface and call sites before deciding."
fi

echo
echo "Caller files:"
echo "$CALLERS" | sed 's/^/  /'
