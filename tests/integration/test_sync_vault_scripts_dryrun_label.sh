#!/usr/bin/env bash
# test_sync_vault_scripts_dryrun_label.sh
#
# sync-vault-scripts.sh labelled its log header with `${DRY_RUN:+ (dry-run)}`.
# That parameter expansion tests NON-EMPTY, and DRY_RUN is initialised to `0`,
# which IS non-empty — so EVERY real run was recorded as "(dry-run)".
#
# The write-guards use the correct `[ "$DRY_RUN" -eq 1 ]`, so behaviour was
# always right and only the audit trail lied. That is the dangerous half: on
# 2026-08-18 a real run overwrote two patched vault scripts and logged
# "(dry-run) … Updated: 2", so reading the log to find the culprit said nothing
# had been written.
#
# NEGATIVE CONTROL: the old expansion is evaluated directly and asserted to
# mislabel a real run, so a green positive proves the fix rather than a
# scenario that never reproduces.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SYNC="$REPO_ROOT/scripts/sync-vault-scripts.sh"

FAILED=0
pass() { printf '  PASS: %s\n' "$1"; }
fail() { printf '  FAIL: %s\n' "$1"; FAILED=1; }

echo "test_sync_vault_scripts_dryrun_label"

# --- NEGATIVE CONTROL: the old form mislabels a real run --------------------
old_label() { local DRY_RUN="$1"; printf '%s' "${DRY_RUN:+ (dry-run)}"; }
if [ -n "$(old_label 0)" ]; then
    pass "negative control: old \${DRY_RUN:+...} DOES mislabel a real run (DRY_RUN=0)"
else
    fail "negative control did not reproduce — the bug shape is wrong"
fi

# --- the shipped script must no longer use that expansion in CODE -----------
# Comment lines are excluded on purpose: the fix documents the old expansion in
# a comment, and matching that would fail forever on a correctly-fixed script.
if grep -vE '^\s*#' "$SYNC" | grep -q 'DRY_RUN:+'; then
    fail "shipped script still uses \${DRY_RUN:+...} in code"
else
    pass "shipped script no longer uses the non-empty expansion in code"
fi

# --- POSITIVE ---------------------------------------------------------------
# Note the asymmetry, which is the bug's real shape: a dry run prints its
# summary to STDOUT and never touches the log, while a real run writes the log.
# So a "(dry-run)" header inside .vault-script-sync.log was unreachable except
# as a mislabel — every one ever written there was, by construction, a real run.
sync_run() {  # sync_run <extra-args...> -> echoes "<stdout header>|<log header>"
    local vault; vault=$(mktemp -d)
    mkdir -p "$vault/⚙️ Meta/scripts"
    local out; out=$(bash "$SYNC" --vault "$vault" "$@" 2>/dev/null)
    local log="$vault/⚙️ Meta/scripts/.vault-script-sync.log"
    local loghdr=""
    [ -f "$log" ] && loghdr=$(grep '^=== sync-vault-scripts.sh @' "$log" | tail -1)
    printf '%s|%s' "$(printf '%s' "$out" | grep '^=== sync-vault-scripts.sh @' | tail -1)" "$loghdr"
    rm -rf "$vault"
}

real_log_hdr=$(sync_run | sed 's/^[^|]*|//')
case "$real_log_hdr" in
    *"(dry-run)"*) fail "REAL run's LOG header still says (dry-run): $real_log_hdr" ;;
    "")            echo "  SKIP: real run produced no log header in this environment" ;;
    *)             pass "real run's log header is not labelled dry-run" ;;
esac

dry_stdout_hdr=$(sync_run --dry-run | sed 's/|.*//')
case "$dry_stdout_hdr" in
    *"(dry-run)"*) pass "dry run's stdout header IS labelled dry-run" ;;
    "")            echo "  SKIP: dry run produced no stdout header" ;;
    *)             fail "dry run header missing the (dry-run) label: $dry_stdout_hdr" ;;
esac

[ $FAILED -eq 0 ] && echo "OK" || echo "FAILURES"
exit $FAILED
