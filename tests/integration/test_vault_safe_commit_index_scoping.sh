#!/usr/bin/env bash
# test_vault_safe_commit_index_scoping.sh
#
# vault-safe-commit.sh is the ONLY sanctioned route past the raw-git block guard,
# so if it commits the whole index instead of the named paths, that guard provides
# zero real scoping while looking fully enforced.
#
# Measured on a live vault before the fix: two consecutive calls that each named ONE
# path produced commits of 1,191 files / 598,702 insertions, sweeping other live
# sessions' data into an unrelated message.
#
# POSITIVE control: naming one path commits exactly that path.
# NEGATIVE control: the pre-fix commit line (bare `git commit -m`) is re-run against
#   the same fixture and MUST sweep the sibling file — proving this test can actually
#   fail, rather than passing because the scenario never reproduces.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WRAPPER="$REPO_ROOT/scripts/vault-safe-commit.sh"

FAILED=0
pass() { printf '  PASS: %s\n' "$1"; }
fail() { printf '  FAIL: %s\n' "$1"; FAILED=1; }

make_fixture() {
    # Echoes a fresh temp repo path with: ours.md (modified) + sibling.md (STAGED by
    # a "concurrent session"). Both tracked, both dirty in the index.
    local d
    d=$(mktemp -d)
    git -C "$d" init --quiet
    git -C "$d" config user.email test@example.com
    git -C "$d" config user.name  test
    printf 'v1\n' > "$d/ours.md"
    printf 'v1\n' > "$d/sibling.md"
    git -C "$d" add ours.md sibling.md
    git -C "$d" commit --quiet -m "baseline"
    # our edit (unstaged) + the sibling session's edit (already staged in shared index)
    printf 'v2-ours\n'    > "$d/ours.md"
    printf 'v2-sibling\n' > "$d/sibling.md"
    git -C "$d" add sibling.md
    echo "$d"
}

files_in_head() { git -C "$1" show --name-only --format="" HEAD | sed '/^$/d' | sort | tr '\n' ' '; }

echo "test_vault_safe_commit_index_scoping"

# --- POSITIVE: the shipped wrapper must commit ONLY ours.md ---------------------
REPO=$(make_fixture)
out=$(cd "$REPO" && VAULT_ROOT="$REPO" bash "$WRAPPER" "scoped: ours only" "ours.md" 2>&1)
rc=$?
got=$(files_in_head "$REPO")
if [ $rc -ne 0 ]; then
    fail "wrapper exited $rc: $out"
elif [ "$got" = "ours.md " ]; then
    pass "wrapper committed only the named path (got: $got)"
else
    fail "wrapper swept extra files — expected 'ours.md ', got '$got'"
fi
# the sibling's staged work must still be pending, not consumed
if git -C "$REPO" diff --cached --quiet -- sibling.md; then
    fail "sibling.md staged work was consumed by our commit"
else
    pass "sibling's staged work left intact for its own session"
fi
rm -rf "$REPO"

# --- NEGATIVE control: pre-fix commit line MUST sweep the sibling ---------------
# Proves the fixture genuinely reproduces the bug, so a green POSITIVE means the
# `--only` scoping is doing the work (not that the scenario never happens).
REPO=$(make_fixture)
(cd "$REPO" && git add -- ours.md && git commit --quiet -m "pre-fix: bare commit") >/dev/null 2>&1
got=$(files_in_head "$REPO")
if [ "$got" = "ours.md sibling.md " ]; then
    pass "negative control reproduced the bug (pre-fix swept: $got)"
else
    fail "negative control did NOT reproduce — fixture is not exercising the bug (got: '$got')"
fi
rm -rf "$REPO"

# --- guard: the scoping flags must actually be present in the shipped script ----
# grep, not rg: CI runners do not ship ripgrep.
if grep -q -- '--only' "$WRAPPER" && grep -qF 'diff --cached --quiet -- "${PATHS[@]}"' "$WRAPPER"; then
    pass "shipped script carries --only and scoped staged-check"
else
    fail "shipped script is missing --only and/or the scoped staged-check"
fi

[ $FAILED -eq 0 ] && echo "OK" || echo "FAILURES"
exit $FAILED
