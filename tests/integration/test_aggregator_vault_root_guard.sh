#!/usr/bin/env bash
# Test: the vault aggregators never clobber a FOREIGN vault on an inherited
# VAULT_ROOT, and they back up the target before overwriting it (MYC-2439).
#
# Bug class this guards: a globally-exported VAULT_ROOT (a shell-profile
# export is a real, common case) beat the scripts' own autodetect, so ANY
# copy run WITHOUT an explicit per-invocation VAULT_ROOT= silently operated
# on — and overwrote, with NO backup — the WRONG vault's Last Session.md /
# Decision Log.md / Current Priorities.md. Silent data-loss + cross-vault-leak.
#
# The fix has two legs, both asserted here against REAL copies of the scripts:
#   (a) _resolve_vault_root() prefers the script's OWN vault; a mismatched
#       ambient VAULT_ROOT is ignored (with a stderr warning), so a foreign
#       vault is never touched. NEGATIVE CONTROL: VAULT_ROOT_FORCE=1 proves the
#       env var CAN still redirect when explicitly opted in — so the unforced
#       protection is real, not vacuous.
#   (b) _backup_before_write() snapshots the target before every overwrite, so
#       even a forced run can't destroy the prior good file.
#
# Self-contained: tmpdir fake vaults, copies of the real scripts. Exit 0 = pass.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SESSIONS_SRC="$REPO_ROOT/scripts/aggregate-sessions.py"
DECISIONS_SRC="$REPO_ROOT/scripts/aggregate-decisions.py"
OUTCOME_SRC="$REPO_ROOT/scripts/decision-outcome-check.py"
for s in "$SESSIONS_SRC" "$DECISIONS_SRC" "$OUTCOME_SRC"; do
  [ -f "$s" ] || { echo "ERROR: $s not found" >&2; exit 1; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SENTINEL="SENTINEL_DO_NOT_CLOBBER_$$"
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1" >&2; [ -n "${2:-}" ] && sed 's/^/  /' "$2" >&2; exit 1; }
newest_bak() {  # dir stem  ->  path of newest "<stem>.bak-*.md", or ""
  find "$1" -maxdepth 1 -name "$2.bak-*.md" -print 2>/dev/null | sort | tail -1
}

# --- Build two fake vaults -------------------------------------------------
# vault_a: the vault the script COPIES physically live in (their own vault).
# vault_b: a DIFFERENT vault, standing in for the personal vault that a global
#          VAULT_ROOT points at.
make_session() {  # vault_dir label
  cat > "$1/⚙️ Meta/Sessions/2026-07-01T1200-$2.md" <<EOF
---
creationDate: 2026-07-01
type: session
worktree: main
session_date: 2026-07-01
session_label: "$2 session"
---
# Session — $2
$2 body line
EOF
}
make_decision() {  # vault_dir label
  cat > "$1/⚙️ Meta/Decisions/2026-07-01-$2.md" <<EOF
---
creationDate: 2026-07-01
type: decision
decision_date: 2026-07-01
outcome: pending
---
# Decision $2
$2 decision body
EOF
}

for v in vault_a vault_b; do
  mkdir -p "$TMP/$v/⚙️ Meta/scripts" "$TMP/$v/⚙️ Meta/Sessions" "$TMP/$v/⚙️ Meta/Decisions"
done

# The script COPIES live under vault_a → vault_a is their "own" vault.
cp "$SESSIONS_SRC"  "$TMP/vault_a/⚙️ Meta/scripts/aggregate-sessions.py"
cp "$DECISIONS_SRC" "$TMP/vault_a/⚙️ Meta/scripts/aggregate-decisions.py"
cp "$OUTCOME_SRC"   "$TMP/vault_a/⚙️ Meta/scripts/decision-outcome-check.py"
# decision-outcome-check.py imports a sibling module — copy it too.
cp "$REPO_ROOT/scripts/_meta_resolver.py" "$TMP/vault_a/⚙️ Meta/scripts/_meta_resolver.py"
A_SESSIONS="$TMP/vault_a/⚙️ Meta/scripts/aggregate-sessions.py"
A_DECISIONS="$TMP/vault_a/⚙️ Meta/scripts/aggregate-decisions.py"
A_OUTCOME="$TMP/vault_a/⚙️ Meta/scripts/decision-outcome-check.py"

make_session  "$TMP/vault_a" a
make_session  "$TMP/vault_b" b
make_decision "$TMP/vault_a" a
make_decision "$TMP/vault_b" b

A_META="$TMP/vault_a/⚙️ Meta"
B_META="$TMP/vault_b/⚙️ Meta"
A_LAST="$A_META/Last Session.md"

# Sentinels in vault_b's OUTPUT files — these must survive a foreign run.
printf '%s last-session\n'      "$SENTINEL" > "$B_META/Last Session.md"
printf '%s decision-log\n'      "$SENTINEL" > "$B_META/Decision Log.md"
# decision-outcome-check reads Decision Log.md and writes Current Priorities.md.
printf '# Decision Log\n\n### 2026-01-01 — Old\n- **Outcome:**\n' > "$A_META/Decision Log.md"
printf '# Current Priorities\n\n*live*\n'                          > "$A_META/Current Priorities.md"
printf '%s current-priorities\n' "$SENTINEL"                       > "$B_META/Current Priorities.md"

# --- 1. No-clobber: aggregate-sessions with a mismatched VAULT_ROOT ---------
err="$TMP/err1.txt"
VAULT_ROOT="$B_META/.." python3 "$A_SESSIONS" >/dev/null 2>"$err"
grep -q "$SENTINEL" "$B_META/Last Session.md" \
  || fail "aggregate-sessions clobbered the FOREIGN vault's Last Session.md" "$err"
grep -q "<!-- aggregate-sessions:BEGIN -->" "$B_META/Last Session.md" \
  && fail "aggregate-sessions rewrote the FOREIGN vault's Last Session.md (should be untouched)"
pass "aggregate-sessions leaves a foreign (mismatched VAULT_ROOT) vault untouched"
[ -f "$A_LAST" ] || fail "aggregate-sessions did not write its OWN vault's Last Session.md" "$err"
grep -q "$SENTINEL" "$A_LAST" && fail "own-vault output unexpectedly contains the foreign sentinel"
pass "aggregate-sessions wrote its OWN vault instead"
grep -q "WARNING: VAULT_ROOT env points at" "$err" \
  || fail "no stderr warning when a mismatched VAULT_ROOT is ignored" "$err"
pass "aggregate-sessions warns on stderr when it ignores a mismatched VAULT_ROOT"

# --- 2. No-clobber: aggregate-decisions with a mismatched VAULT_ROOT --------
VAULT_ROOT="$B_META/.." python3 "$A_DECISIONS" >/dev/null 2>/dev/null
grep -q "$SENTINEL" "$B_META/Decision Log.md" \
  || fail "aggregate-decisions clobbered the FOREIGN vault's Decision Log.md"
pass "aggregate-decisions leaves a foreign (mismatched VAULT_ROOT) vault untouched"

# --- 3. No-clobber: decision-outcome-check with a mismatched VAULT_ROOT -----
VAULT_ROOT="$B_META/.." python3 "$A_OUTCOME" >/dev/null 2>/dev/null
grep -q "$SENTINEL" "$B_META/Current Priorities.md" \
  || fail "decision-outcome-check clobbered the FOREIGN vault's Current Priorities.md"
pass "decision-outcome-check leaves a foreign (mismatched VAULT_ROOT) vault untouched"

# --- 4. Backup-before-write in the script's OWN vault ----------------------
before="$(cat "$A_LAST")"
make_session "$TMP/vault_a" a2          # change the input so the re-run differs
python3 "$A_SESSIONS" >/dev/null 2>/dev/null   # auto-detect, own vault
bak="$(newest_bak "$A_META" "Last Session")"
[ -n "$bak" ] || fail "aggregate-sessions did not back up Last Session.md before overwriting"
[ "$(cat "$bak")" = "$before" ] || fail "backup does not contain the prior good content"
pass "aggregate-sessions backs up the prior Last Session.md before overwriting"

# --- 5. NEGATIVE CONTROL: FORCE operates cross-vault BUT still backs up -----
# Proves the unforced protection above is real (the env var CAN redirect when
# opted in) AND that even a forced cross-vault write preserves the prior file.
VAULT_ROOT_FORCE=1 VAULT_ROOT="$B_META/.." python3 "$A_SESSIONS" >/dev/null 2>/dev/null
b_bak="$(newest_bak "$B_META" "Last Session")"
[ -n "$b_bak" ] || fail "forced cross-vault run did not back up the target vault's Last Session.md"
grep -q "$SENTINEL" "$b_bak" || fail "forced-run backup does not preserve the prior sentinel content"
grep -q "<!-- aggregate-sessions:BEGIN -->" "$B_META/Last Session.md" \
  || fail "forced run did not rewrite the target vault's Last Session.md (FORCE ignored?)"
pass "VAULT_ROOT_FORCE=1 operates cross-vault but backs up the target first (no destructive clobber)"

echo
echo "All assertions passed. Aggregators honor the vault-root guard + backup-before-write (MYC-2439)."
