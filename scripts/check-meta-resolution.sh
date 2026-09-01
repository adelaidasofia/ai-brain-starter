#!/usr/bin/env bash
# exit-contract: ENFORCING

#
# scripts/check-meta-resolution.sh - ban the naive "*Meta" glob in shell scripts.
#
# The vault Meta folder MUST be resolved through scripts/_meta_resolver.py, which
# prefers the variant containing a known subfolder. It must NEVER be resolved with
# a sort-order glob like `for c in "$VAULT"/*Meta; do ...; break`: plain "Meta"
# (machine memory) sorts before the emoji "⚙️ Meta" (human memory), so the naive
# glob silently writes human session/traffic data into the machine folder.
# ai-brain-starter#176 fixed the five scripts that had it; this guard stops a
# sixth from reintroducing the bug class.
#
# Usage:
#   bash scripts/check-meta-resolution.sh         # scan tracked *.sh (CI + local)
#   bash scripts/check-meta-resolution.sh <dir>   # scan *.sh under <dir> (tests)
#
# Exit: 0 = clean, 1 = a banned glob was found.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# The banned construct: iterating a "<var>"/*Meta glob.
PATTERN='in[[:space:]]+"?\$[A-Za-z_][A-Za-z0-9_]*"?/\*Meta'

# This guard + its test legitimately NAME the pattern as data; never flag them.
SELF="$(basename "${BASH_SOURCE[0]}")"
skip_basename() {
  case "$1" in
    "$SELF"|test-meta-resolution-guard.sh|test_meta_resolution_guard.sh) return 0 ;;
    *) return 1 ;;
  esac
}

list_files() {
  if [ -n "${1:-}" ]; then
    find "$1" -name '*.sh' -type f 2>/dev/null
  else
    ( cd "$REPO_ROOT" && git ls-files '*.sh' ) \
      | while IFS= read -r rel; do printf '%s/%s\n' "$REPO_ROOT" "$rel"; done
  fi
}

offenders=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  skip_basename "$(basename "$f")" && continue
  # Match executable uses only: drop pure-comment lines (a doc comment that
  # NAMES the banned glob, e.g. in a test or a how-this-was-fixed note, is fine).
  if hits="$(grep -nE "$PATTERN" "$f" 2>/dev/null | grep -vE '^[0-9]+:[[:space:]]*#')"; then
    echo "::error file=$f::naive *Meta glob: resolve the Meta folder via scripts/_meta_resolver.py instead"
    printf '%s\n' "$hits" | sed 's/^/    /'
    offenders=$((offenders + 1))
  fi
done < <(list_files "${1:-}")

if [ "$offenders" -gt 0 ]; then
  echo "FAILED: $offenders shell file(s) use the banned naive *Meta glob."
  echo 'Fix: META_DIR="$(python3 "$SCRIPT_DIR/_meta_resolver.py" "$VAULT" Sessions Decisions)" || META_DIR="$VAULT/Meta"'
  exit 1
fi
echo "OK - no naive *Meta glob in shell scripts"
