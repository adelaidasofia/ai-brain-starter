#!/usr/bin/env bash
# Run the vault-safety guard self-tests in CI so they cannot bit-rot.
#
# These guards each ship a negative control (they assert the guard FIRES on the
# thing it catches AND stays silent otherwise) — but a self-test that only ever
# runs by hand is one refactor away from silently going stale. Wiring them into
# the canonical `scripts/ci.sh` gate is what makes "a guard earns trust only by
# failing on the thing it catches" durable instead of aspirational.
#
# Covers:
#   - scripts/test-cloud-sync-guard.sh           (vault-in-cloud-sync detector, #159)
#   - scripts/test-detect-cloud-sync-mirror.sh   (shared detect_cloud_sync + install guard: Drive Mirror roots, MYC-1130)
#   - scripts/test-sync-folder-machinery-guard.sh (machinery-in-synced-folder + Drive Mirror roots, MYC-705)
#   - scripts/test-vault-backup-guard.sh         (no-off-machine-backup detector)
#   - scripts/test-vault-backup-roundtrip.sh     (one-command backup: real restore loop)
#   - scripts/test-vault-backup-schedule.sh      (daily schedule actually installed, MYC-3528)
#
# CI-safe: every guard test is hermetic (temp dirs, isolated config via env),
# needs no network, and the round-trip test skips its encrypted leg when neither
# gpg nor openssl is present. The schedule test stubs launchctl/crontab on PATH
# and overrides HOME, so it never touches the runner's real launchd or crontab.
set -u
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
fails=0

run_guard() { # <relative-script-path>
  local rel="$1" path="$REPO_ROOT/$1"
  if [ ! -f "$path" ]; then
    echo "FAIL  missing guard self-test: $rel"; fails=$((fails+1)); return
  fi
  echo "--- $rel"
  if bash "$path"; then
    echo "OK    $rel"
  else
    echo "FAIL  $rel exited non-zero"; fails=$((fails+1))
  fi
}

run_guard "scripts/test-cloud-sync-guard.sh"
run_guard "scripts/test-detect-cloud-sync-mirror.sh"
run_guard "scripts/test-sync-folder-machinery-guard.sh"
run_guard "scripts/test-vault-backup-guard.sh"
run_guard "scripts/test-vault-backup-roundtrip.sh"
run_guard "scripts/test-vault-backup-schedule.sh"

echo
if [ "$fails" -gt 0 ]; then
  echo "VAULT-SAFETY GUARDS FAILED: $fails"
  exit 1
fi
echo "All vault-safety guard self-tests passed."
