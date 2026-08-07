#!/usr/bin/env bash
# Structural regression — backfills the #301 sibling-miss.
#
# vault-backup.ps1 writes ~/.claude/.vault-backup.conf via Windows PowerShell 5.1
# `Set-Content -Encoding UTF8`, which prepends a UTF-8 BOM. A reader that decodes
# with plain read_text() raises "Unexpected UTF-8 BOM" (a ValueError). Every
# backup-status consumer swallows that in `except (OSError, ValueError)` and
# returns empty, so a configured, snapshotting, restore-verified backup reads as
# ABSENT / never-verified — a red /diagnose FAIL and a SessionStart nag that
# never self-corrects (the daily task re-writes the BOM nightly).
#
# #301 fixed one reader (scripts/check-vault-backup.py _read_conf), but a SIBLING
# reader in the same class (hooks/surface-backup-status.py _verify_age_days) read
# the same file with plain read_text() and was missed. This test locks the class
# invariant: EVERY reader of that config must decode utf-8-sig, so a future third
# reader cannot silently reintroduce the bug. Fails loud if zero readers are found
# (the config handle was renamed and this guard is now blind).
#
# Bash only, no network. exit 0 = every reader is BOM-tolerant.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

conf_readers() {
  grep -rnE 'CONF_PATH\.read_text\(' "$REPO/hooks" "$REPO/scripts" --include='*.py' 2>/dev/null
}

total=0
bad=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  total=$((total + 1))
  if ! printf '%s' "$line" | grep -q 'utf-8-sig'; then
    echo "FAIL  BOM-unsafe .vault-backup.conf reader (missing utf-8-sig):"
    echo "        ${line#"$REPO"/}"
    bad=$((bad + 1))
  fi
done < <(conf_readers)

if [ "$total" -eq 0 ]; then
  echo "FAIL  found ZERO CONF_PATH.read_text() readers — the config handle likely"
  echo "      renamed. Update this guard to track the new reader(s), or it is blind."
  exit 1
fi

if [ "$bad" -eq 0 ]; then
  echo "PASS  all $total .vault-backup.conf reader(s) decode utf-8-sig (BOM-tolerant)"
  exit 0
fi
echo
echo "FAIL  $bad of $total reader(s) would choke on a BOM'd config on Windows."
echo "      Fix: json.loads(CONF_PATH.read_text(encoding=\"utf-8-sig\"))"
exit 1
