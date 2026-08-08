#!/usr/bin/env bash
# Test: build-journal-index.py finds the journal folder on NON-English vaults.
#
# Bug: --journal-dir defaulted to the hardcoded English "Journals", but Phase 3
# creates a LOCALIZED folder on a non-English install ("📓 Diario" on es), and
# insights/SKILL.md invokes the script with NO arguments:
#     /usr/bin/python3 "[VAULT_PATH]/Meta/scripts/build-journal-index.py"
# so that default was the only thing ever consulted. /weekly and /monthly died
# with "journal directory not found" on every Spanish/Portuguese vault, while
# the Meta folder next to it WAS auto-detected (find_meta_dir handles "⚙️ Meta").
#
# Asserts:
#   1. es vault ("📓 Diario")   -> index built
#   2. pt vault ("📓 Diário")   -> index built
#   3. en vault ("📓 Journals") -> index built (no regression)
#   4. en vault ("Journals")    -> index built (plain, no emoji)
#   5. explicit --journal-dir still wins over auto-detection
#   6. NEGATIVE CONTROL: a vault with NO journal folder still fails loud
#      (exit 1) — auto-detection must not invent a folder.
#
# Self-contained: tmpdir vaults. Exit 0 = pass, 1 = fail.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/build-journal-index.py"
if [ ! -f "$SCRIPT" ]; then
  echo "ERROR: $SCRIPT not found" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

failed=0

# make_vault <dir-name> -> echoes the vault root
make_vault() {
  local journal_name="$1"
  local v; v="$(mktemp -d "$TMP/vault.XXXXXX")"
  mkdir -p "$v/⚙️ Meta"
  if [ -n "$journal_name" ]; then
    mkdir -p "$v/$journal_name"
    printf -- '---\ncreationDate: 2026-04-11\nfloor: Courage\n---\n\nentry\n' \
      > "$v/$journal_name/2026-04-11.md"
  fi
  echo "$v"
}

assert_builds() {
  local label="$1" journal_name="$2"; shift 2
  local v; v="$(make_vault "$journal_name")"
  if python3 "$SCRIPT" --vault-root "$v" "$@" >/dev/null 2>&1 \
     && [ -f "$v/⚙️ Meta/journal-index.json" ] \
     && python3 -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))['total']==1 else 1)" \
          "$v/⚙️ Meta/journal-index.json"; then
    echo "  ok   - $label"
  else
    echo "  FAIL - $label (index not built from '$journal_name')" >&2
    failed=$((failed+1))
  fi
}

assert_fails_loud() {
  local label="$1"
  local v; v="$(make_vault "")"          # no journal folder at all
  if python3 "$SCRIPT" --vault-root "$v" >/dev/null 2>&1; then
    echo "  FAIL - $label (exited 0 with no journal folder)" >&2
    failed=$((failed+1))
  elif [ -f "$v/⚙️ Meta/journal-index.json" ]; then
    echo "  FAIL - $label (wrote an index anyway)" >&2
    failed=$((failed+1))
  else
    echo "  ok   - $label"
  fi
}

echo "==> build-journal-index localized journal-dir detection"
assert_builds "es vault: 📓 Diario"    "📓 Diario"
assert_builds "pt vault: 📓 Diário"    "📓 Diário"
assert_builds "en vault: 📓 Journals"  "📓 Journals"
assert_builds "en vault: Journals"     "Journals"
assert_builds "explicit --journal-dir still wins" "Cuaderno" --journal-dir "Cuaderno"
assert_fails_loud "no journal folder -> fails loud, invents nothing"

if [ "$failed" -gt 0 ]; then
  echo "journal-index-localized-dir: $failed failed" >&2
  exit 1
fi
echo "journal-index-localized-dir: 6 passed, 0 failed"
