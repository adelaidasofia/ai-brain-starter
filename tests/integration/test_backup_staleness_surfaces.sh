#!/usr/bin/env bash
# Structural regression — the sibling-miss class, recurring.
#
# check-vault-backup.py emits BACKED_UP:vault-backup:<age_days>. The age is the
# whole point of the token: an archive that EXISTS is not the same as a backup
# that RUNS. hooks/surface-backup-status.py honours that — it compares the age
# against VAULT_BACKUP_STALE_DAYS (default 3) and nags past the threshold.
#
# Both /diagnose surfaces (scripts/diagnose.ps1, scripts/diagnose.sh) parsed the
# same token, INTERPOLATED the age into their message, and then reported OK
# unconditionally. A vault whose scheduled backup had not fired in 25 days
# printed "OK  Off-machine backup present (vault-backup, ~25.2 days old)" — the
# number was right there in the green line. Observed on Windows, where a task
# created with DisallowStartIfOnBatteries never fires on a laptop on battery
# (last result 0x800710E0), so the schedule dies silently and /diagnose blesses
# it. That is strictly worse than no check: it converts a dead backup into a
# reassurance, and it disagreed with the SessionStart hook on the same machine.
#
# This is the same shape as the #301 BOM miss that test_vault_backup_conf_bom.sh
# backfills: one consumer of a shared backup signal was fixed, its siblings were
# not. So lock the CLASS invariant instead of the instance — every consumer of
# the age token must threshold it, so a fourth surface cannot re-green a dead
# backup. Fails loud if zero consumers are found (the token was renamed and this
# guard is now blind).
#
# Bash only, no network. exit 0 = every consumer thresholds the age.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Consumers only. Excluded on purpose:
#   check-vault-backup.py  — the PRODUCER; it computes the age, it does not judge it.
#   test-*                 — harnesses that fabricate the token as fixture input.
#   __pycache__/           — COMPILED BYTECODE, not source. `grep -rl` matches
#                            binaries too, and the token survives into a .pyc's
#                            string table, so `check-vault-backup.cpython-*.pyc`
#                            slipped past the exclusion above: that exclusion is
#                            by exact filename, and the compiled twin has a
#                            different one. Bytecode cannot be inspected for a
#                            threshold, so it failed unconditionally. Worse, the
#                            __pycache__ is created by THIS gate — ci.sh step (a)
#                            py_compiles every tracked *.py before the
#                            integration tests run — so the gate manufactured the
#                            artifact that then failed it.
age_consumers() {
  grep -rl 'BACKED_UP:vault-backup' "$REPO/hooks" "$REPO/scripts" 2>/dev/null \
    | grep -v '/__pycache__/' \
    | grep -v '/check-vault-backup\.py$' \
    | grep -v '/test-'
}

total=0
bad=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  total=$((total + 1))
  # A real threshold means naming the shared budget. Printing the age is not
  # thresholding it — that was precisely the bug.
  if ! grep -q 'VAULT_BACKUP_STALE_DAYS\|STALE_DAYS' "$f"; then
    echo "FAIL  backup-age consumer never thresholds the age it prints:"
    echo "        ${f#"$REPO"/}"
    bad=$((bad + 1))
  fi
done < <(age_consumers)

if [ "$total" -eq 0 ]; then
  echo "FAIL  found ZERO BACKED_UP:vault-backup consumers — the token likely"
  echo "      renamed. Update this guard to track it, or it is blind."
  exit 1
fi

if [ "$bad" -eq 0 ]; then
  echo "PASS  all $total backup-age consumer(s) threshold the age (stale != healthy)"
  exit 0
fi
echo
echo "FAIL  $bad of $total consumer(s) would report a long-dead backup as healthy."
echo "      Fix: compare the age against VAULT_BACKUP_STALE_DAYS (default 3)"
echo "      before reporting OK, as hooks/surface-backup-status.py does."
exit 1
