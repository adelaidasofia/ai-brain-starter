#!/usr/bin/env bash
# Test: scripts/check-close-phase-contract.py holds the session-close rule and
# the injected cascade to one phase-number contract.
#
# History this locks (all real, all shipped):
#   #400 added a `/goal clear` reminder to the shipped rule labelled "Phase 4b",
#        borrowing the cascade's numbering. Under the rule's own numbering Phase 4
#        was the automatic step ("you do nothing"), so the label announced that the
#        goal clears itself — the opposite of the truth.
#   #401 removed the label.
#   #415 aligned the rule's numbers to the cascade's.
#   Then the SAME defect appeared in an installed, customised copy of the rule:
#        a "Phase 4b" with no Phase 4 anywhere in the file. A guard that only ran
#        against this repo's own copy could not see it — hence a checker that
#        takes a --rule PATH.
#
# The checker is two-tier on purpose: STRUCTURAL checks (duplicate numbers,
# orphan sub-phases) run on any rule file and cannot false-positive; MEANING pins
# run only with --strict-meaning, on the canonical file, because an installed
# rule is customised prose and a checker that fails on legitimate customisation
# is one people switch off.
#
# Assertions:
#   1. The shipped template passes structural + meaning.
#   2. The #401 regression: no bare "Phase 4b" label in the template.
#   3. NEGATIVE — a duplicate phase number fails.
#   4. NEGATIVE — an orphan sub-phase (4b with no 4) fails.
#   5. NEGATIVE — a meaning swap (Phase 3/4 traded, i.e. the #415 state) fails
#      under --strict-meaning.
#   6. POSITIVE control on the exemption — the 0-series (0a/0b with no bare
#      Phase 0) must NOT fail; it is convention in both files, and flagging it
#      would teach bypass.
#   7. FAIL LOUD — an unreadable or phase-less rule exits 2, never 0.
#
# Exit 0 = pass, 1 = fail.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECKER="$REPO_ROOT/scripts/check-close-phase-contract.py"
RULE="$REPO_ROOT/templates/rules/session-close.md"
CASCADE="$REPO_ROOT/hooks/detect-closing-signal.py"

for f in "$CHECKER" "$RULE" "$CASCADE"; do
  [ -f "$f" ] || { echo "ERROR: $f not found" >&2; exit 1; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

failed=0
fail() { echo "FAIL: $*" >&2; failed=1; }
ok() { echo "ok   $*"; }

run_checker() {  # <rule-file> [extra args...] -> echoes exit code
  local rule="$1"; shift
  python3 "$CHECKER" --rule "$rule" --cascade "$CASCADE" "$@" >/dev/null 2>&1
  echo $?
}

# --- 1. The shipped template passes both tiers ------------------------------
rc="$(run_checker "$RULE" --strict-meaning)"
[ "$rc" = "0" ] && ok "shipped template passes structural + meaning" \
  || fail "shipped template violates the phase contract (exit $rc) — run: python3 $CHECKER --strict-meaning"

# --- 2. #401 regression: no cross-numbered label in the template ------------
if grep -q "Phase 4b" "$RULE"; then
  fail "template reintroduced a 'Phase 4b' label (#401 — the cascade owns that number)"
else
  ok "template carries no cross-numbered 'Phase 4b' label"
fi

# --- 3. NEGATIVE: duplicate phase number ------------------------------------
cat > "$TMP/dupe.md" <<'EOF'
## Phase 1 — Scan the conversation
body
## Phase 4 — Goodbye
body
## Phase 4 — Automatic finalization
body
EOF
rc="$(run_checker "$TMP/dupe.md")"
[ "$rc" = "1" ] && ok "duplicate phase number is rejected" \
  || fail "duplicate phase number NOT caught (exit $rc)"

# --- 4. NEGATIVE: orphan sub-phase (the installed-rule defect) --------------
cat > "$TMP/orphan.md" <<'EOF'
## Phase 1 — Scan the conversation
body
## Phase 3 — Verification
body
## Phase 4b — Clear the session goal
body
EOF
rc="$(run_checker "$TMP/orphan.md")"
[ "$rc" = "1" ] && ok "orphan sub-phase (4b with no 4) is rejected" \
  || fail "orphan sub-phase NOT caught (exit $rc)"

# --- 5. NEGATIVE: the #415 state — Phase 3/4 meanings traded ---------------
cat > "$TMP/swapped.md" <<'EOF'
## Phase 0b — Unfinished business
body
## Phase 1 — Scan the conversation
body
## Phase 2 — Write to the vault
body
## Phase 2b — Commit what you wrote
body
## Phase 3 — Goodbye
body
## Phase 4 — Automatic finalization
body
EOF
rc="$(run_checker "$TMP/swapped.md" --strict-meaning)"
[ "$rc" = "1" ] && ok "swapped Phase 3/4 meanings rejected under --strict-meaning" \
  || fail "the original #415 collision NOT caught (exit $rc)"
# ...and the SAME file must pass structurally: it is internally consistent, which
# is precisely why the collision went unnoticed. If this fails, the structural
# tier has become over-strict.
rc="$(run_checker "$TMP/swapped.md")"
[ "$rc" = "0" ] && ok "that same file passes structurally (tiers are genuinely separate)" \
  || fail "structural tier over-reached into meaning (exit $rc)"

# --- 6. POSITIVE control: the 0-series exemption ---------------------------
cat > "$TMP/zeroes.md" <<'EOF'
## Phase 0a — Run the canonical runner
body
## Phase 0b — Unfinished business
body
## Phase 1 — Scan the conversation
body
EOF
rc="$(run_checker "$TMP/zeroes.md")"
[ "$rc" = "0" ] && ok "0-series (0a/0b with no bare Phase 0) is not flagged" \
  || fail "0-series wrongly flagged as orphans (exit $rc) — this teaches bypass"

# --- 7. FAIL LOUD on unusable input ----------------------------------------
rc="$(run_checker "$TMP/does-not-exist.md")"
[ "$rc" = "2" ] && ok "missing rule file exits 2 (cannot-check, not pass)" \
  || fail "missing rule file returned $rc, expected 2"

printf 'no phase headings here\n' > "$TMP/empty.md"
rc="$(run_checker "$TMP/empty.md")"
[ "$rc" = "2" ] && ok "phase-less rule exits 2 (cannot-check, not pass)" \
  || fail "phase-less rule returned $rc, expected 2"

if [ "$failed" -ne 0 ]; then
  echo "FAILED: close phase contract" >&2
  exit 1
fi
echo "PASS: close phase contract holds, and the checker rejects every known defect"
