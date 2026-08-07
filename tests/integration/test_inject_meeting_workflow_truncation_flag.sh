#!/usr/bin/env bash
# Regression test for inject-meeting-workflow-on-trigger.py truncation flag.
#
# Bug: when the customized meeting-workflow.md exceeds MAX_RULE_CHARS,
# the hook used to append `...[truncated — read full file at <path>]`
# at the END of the injected content. The header before it said "Run
# the FULL cascade below" — so the model started executing the cascade
# against the truncated first 8K chars without knowing late steps
# (Decision Log, CRM updates, humanizer pass, backlinks verify, final
# report) had been dropped. The model would either skip those steps or
# only notice the truncation marker AFTER it had already taken irreversible
# actions like writing a partial meeting note.
#
# This test asserts:
#   1. Cap is now 16000 (raised from 8000).
#   2. Below-cap rules inject normally (no TRUNCATED flag).
#   3. Above-cap rules get a prominent TRUNCATED flag at the TOP of
#      additionalContext, BEFORE the cascade-instruction.
#   4. The TRUNCATED flag names the rule-file path and tells the model
#      to read the full file before running.
#   5. The truncation flag warns about the silent-drop categories
#      (late steps) explicitly so the model knows what's at risk.
#   6. The end-of-content tail still has a remainder-omitted marker
#      (defense in depth — if the model reads only the bottom, it
#      still knows truncation happened).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/inject-meeting-workflow-on-trigger.py"

# Hermeticity: an ambient VAULT_ROOT (set machine-wide on dev boxes) wins over
# this test's fixture inside the hook, making the hook read the REAL vault's
# rule file — the test then fails locally while passing on CI. Always unset.
unset VAULT_ROOT

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

[[ -f "$HOOK" ]] || fail "hook missing at $HOOK"

# 1. Cap raised. Grep the source — the constant is a small surface.
grep -q "^MAX_RULE_CHARS = 16000" "$HOOK" || \
    fail "MAX_RULE_CHARS should be 16000 (was 8000); the canonical template is 4.8K and customized rules typically run 8-12K"

# Throwaway vault for both cases.
TMP_VAULT=$(mktemp -d)
trap 'rm -rf "$TMP_VAULT"' EXIT
mkdir -p "$TMP_VAULT/Meta/rules"
touch "$TMP_VAULT/Meta/Current Priorities.md"

# Helper: run hook from inside the temp vault and emit the
# additionalContext string only.
run_hook() {
    local prompt="$1"
    cd "$TMP_VAULT"
    echo "{\"prompt\":\"$prompt\"}" | python3 "$HOOK" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d['hookSpecificOutput']['additionalContext'])
"
}

# 2. Below-cap rule: no TRUNCATED flag.
printf '# Short rule\n\nDoes not exceed the cap.\n' > "$TMP_VAULT/Meta/rules/meeting-workflow.md"
OUT=$(run_hook "I just had a meeting with the team")
if echo "$OUT" | grep -q "TRUNCATED"; then
    fail "below-cap rule should NOT contain TRUNCATED flag. Got:\n$OUT"
fi
echo "$OUT" | grep -q "meeting-workflow auto-injected" || \
    fail "below-cap rule should still inject the standard header. Got:\n$OUT"

# 3-6. Above-cap rule: TRUNCATED flag at top + remainder marker at tail.
# Generate ~20K of content — well over the 16K cap.
python3 -c "
import sys
sys.stdout.write('# Long rule\n\n## Section A\n\n')
for i in range(2500):
    sys.stdout.write(f'Step {i}. Lorem ipsum dolor sit amet, consectetur adipiscing elit.\n')
" > "$TMP_VAULT/Meta/rules/meeting-workflow.md"
SIZE=$(wc -c < "$TMP_VAULT/Meta/rules/meeting-workflow.md")
[[ $SIZE -gt 16000 ]] || fail "test fixture should be >16000 chars, got $SIZE"

OUT=$(run_hook "I just had a meeting with the team")

# 3. TRUNCATED flag at the top — must appear BEFORE the cascade-instruction.
FLAG_POS=$(echo "$OUT" | grep -bF "TRUNCATED" | head -1 | cut -d: -f1)
CASCADE_POS=$(echo "$OUT" | grep -bF "Run the FULL cascade" | head -1 | cut -d: -f1)
[[ -n "$FLAG_POS" ]] || fail "TRUNCATED flag should appear in above-cap output. Got first 800 chars:\n$(echo "$OUT" | head -c 800)"
[[ -n "$CASCADE_POS" ]] || fail "cascade-instruction should appear in output"
[[ "$FLAG_POS" -lt "$CASCADE_POS" ]] || \
    fail "TRUNCATED flag must come BEFORE the cascade-instruction. Got flag at $FLAG_POS, cascade at $CASCADE_POS"

# 4. TRUNCATED flag names the rule file path.
echo "$OUT" | head -c 600 | grep -qF "$TMP_VAULT/Meta/rules/meeting-workflow.md" || \
    fail "TRUNCATED flag should name the rule-file path in the first 600 chars. Got:\n$(echo "$OUT" | head -c 600)"

# 5. TRUNCATED flag warns about silent-drop categories explicitly.
TOP=$(echo "$OUT" | head -c 800)
echo "$TOP" | grep -qiE "decision log|crm|humanizer|backlinks|late step" || \
    fail "TRUNCATED flag should warn about the silent-drop categories (Decision Log / CRM / humanizer / backlinks / late steps). Got top 800 chars:\n$TOP"

# 6. Remainder-omitted marker at the tail (defense in depth).
TAIL=$(echo "$OUT" | tail -c 300)
echo "$TAIL" | grep -qiE "remainder.*omitted|truncated.*full file" || \
    fail "remainder-omitted marker should appear at the tail. Got tail:\n$TAIL"

echo "PASS: inject-meeting-workflow-on-trigger.py TRUNCATED flag now precedes cascade-instruction; cap raised to 16K; silent-drop categories named"
