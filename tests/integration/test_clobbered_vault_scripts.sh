#!/usr/bin/env bash
# test_clobbered_vault_scripts.sh
#
# check-clobbered-vault-scripts.py is the AT-REST leg for the sync-clobber class.
# test_vault_script_sync.sh section 1b PREVENTS a manifest gap going forward; this
# detects vaults already damaged, plus the one case closure cannot see — a local
# patch silently overwritten by the sync (the file is present and its deps resolve,
# it is simply the wrong version).
#
# Every assertion is a NEGATIVE control: a fixture is planted with the real defect
# and the detector must FIRE, then the defect is removed and it must go clean. A
# detector that has never failed on the thing it catches is unproven.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CHECK="$REPO_ROOT/scripts/check-clobbered-vault-scripts.py"

FAILED=0
pass() { printf '  PASS: %s\n' "$1"; }
fail() { printf '  FAIL: %s\n' "$1"; FAILED=1; }

echo "test_clobbered_vault_scripts"

[ -f "$CHECK" ] || { echo "  FAIL: $CHECK not found"; exit 1; }

# Fixture: a vault with a git repo and a fake "repo scripts" source dir.
make_vault() {  # -> echoes <tmp>; <tmp>/vault and <tmp>/src exist
    local t; t=$(mktemp -d)
    mkdir -p "$t/vault/⚙️ Meta/scripts" "$t/vault/⚙️ Meta/Decisions" "$t/scripts"
    git -C "$t/vault" init --quiet
    git -C "$t/vault" config user.email t@e.com
    git -C "$t/vault" config user.name t
    echo "$t"
}
commit_all() { git -C "$1/vault" add -A >/dev/null 2>&1; git -C "$1/vault" commit --quiet -m base >/dev/null 2>&1; }
run_check() { python3 "$CHECK" --vault "$1/vault" --repo "$1" 2>&1; }

# --- 1. BROKEN-DEP, shell DIRECT form ---------------------------------------
T=$(make_vault)
printf '#!/bin/bash\nSCRIPT_DIR=x\n. "$SCRIPT_DIR/_guard.sh"\n' > "$T/vault/⚙️ Meta/scripts/w.sh"
commit_all "$T"
out=$(run_check "$T"); rc=$?
if [ $rc -eq 1 ] && printf '%s' "$out" | grep -q "BROKEN-DEP"; then
    pass "fires on a shell script sourcing an absent sibling (direct form)"
else
    fail "missed direct-form broken dep (rc=$rc): $out"
fi
printf '#!/bin/bash\n' > "$T/vault/⚙️ Meta/scripts/_guard.sh"
out=$(run_check "$T"); rc=$?
[ $rc -eq 0 ] && pass "goes clean once the sibling is present" \
              || fail "still firing after the sibling was added: $out"
rm -rf "$T"

# --- 2. BROKEN-DEP, shell INDIRECT form (the one that shipped the outage) ----
T=$(make_vault)
printf '#!/bin/bash\nSCRIPT_DIR=x\nG="$SCRIPT_DIR/_guard.sh"\nif [ -f "$G" ]; then . "$G"; fi\n' \
    > "$T/vault/⚙️ Meta/scripts/w.sh"
commit_all "$T"
out=$(run_check "$T"); rc=$?
if [ $rc -eq 1 ] && printf '%s' "$out" | grep -q "BROKEN-DEP"; then
    pass "fires on the INDIRECT assignment form (VAR=... then . \"\$VAR\")"
else
    fail "missed indirect-form broken dep — the real incident shape (rc=$rc): $out"
fi
rm -rf "$T"

# --- 3. BROKEN-DEP, python sibling import -----------------------------------
T=$(make_vault)
printf 'import _helper\n' > "$T/vault/⚙️ Meta/scripts/w.py"
commit_all "$T"
out=$(run_check "$T"); rc=$?
if [ $rc -eq 1 ] && printf '%s' "$out" | grep -q "BROKEN-DEP"; then
    pass "fires on a python script importing an absent sibling module"
else
    fail "missed python sibling import (rc=$rc): $out"
fi
rm -rf "$T"

# --- 4. REVERTED: local patch overwritten by the sync ------------------------
# HEAD holds the patched version; the working tree holds the shipped version.
# Closure cannot see this: the file is present and has no dependencies.
T=$(make_vault)
printf '#!/bin/bash\n# local fix v2\n' > "$T/vault/⚙️ Meta/scripts/w.sh"
commit_all "$T"
printf '#!/bin/bash\n# upstream v1\n' > "$T/scripts/w.sh"
cp "$T/scripts/w.sh" "$T/vault/⚙️ Meta/scripts/w.sh"      # <- the clobber
out=$(run_check "$T"); rc=$?
if [ $rc -eq 1 ] && printf '%s' "$out" | grep -q "REVERTED"; then
    pass "fires when a committed local patch was overwritten by the shipped copy"
else
    fail "missed the reverted-local-patch case (rc=$rc): $out"
fi
# and must NOT fire when the vault copy legitimately differs from shipped
printf '#!/bin/bash\n# local fix v2\n' > "$T/vault/⚙️ Meta/scripts/w.sh"
out=$(run_check "$T"); rc=$?
[ $rc -eq 0 ] && pass "silent when the vault copy legitimately differs from shipped" \
              || fail "false positive on a legitimately-diverged vault file: $out"
rm -rf "$T"

# --- 5. --surface is fail-open ----------------------------------------------
T=$(make_vault)
printf '#!/bin/bash\nSCRIPT_DIR=x\n. "$SCRIPT_DIR/_gone.sh"\n' > "$T/vault/⚙️ Meta/scripts/w.sh"
commit_all "$T"
python3 "$CHECK" --vault "$T/vault" --repo "$T" --surface >/dev/null 2>&1
[ $? -eq 0 ] && pass "--surface exits 0 even with findings (safe for cron)" \
             || fail "--surface did not fail open"
rm -rf "$T"

# --- 6. a vault with no Meta dir is a clean no-op, not a crash ---------------
T=$(mktemp -d); mkdir -p "$T/scripts"
python3 "$CHECK" --vault "$T" --repo "$T" >/dev/null 2>&1
[ $? -eq 0 ] && pass "no Meta dir is a clean no-op" || fail "crashed on a vault with no Meta dir"
rm -rf "$T"

[ $FAILED -eq 0 ] && echo "OK" || echo "FAILURES"
exit $FAILED
