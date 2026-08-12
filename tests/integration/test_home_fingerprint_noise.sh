#!/usr/bin/env bash
# Controls for real_home_fingerprint() in scripts/ci.sh.
#
# The guard exists to red-flag a test that escapes its sandbox and writes into
# the real ~/.claude. It watches deployed CODE. It must NOT react to the logs,
# lock files and append-only state that ordinary session machinery writes into
# those same directories continuously — reacting to those produced a RED the
# harness could not support (MYC-3507).
#
# Both directions are asserted. A fingerprint that ignores everything is as
# useless as one that fires on everything, so every "must not react" case is
# paired with a "must still react" case.
#
# Tests the REAL function, extracted from ci.sh rather than reimplemented, so
# the assertions cannot drift from the implementation they describe.
set -u
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CI="$REPO_ROOT/scripts/ci.sh"
[ -f "$CI" ] || { echo "FAIL: scripts/ci.sh not found"; exit 1; }

TD=$(mktemp -d); trap 'rm -rf "$TD"' EXIT
PASS=0; FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS+1)); }
no()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

# Extract just the function so the whole gate does not run.
FN="$TD/fn.sh"
sed -n '/^real_home_fingerprint()/,/^}/p' "$CI" > "$FN"
grep -q "real_home_fingerprint" "$FN" || { echo "FAIL: could not extract the function"; exit 1; }

FAKE="$TD/home"
mkdir -p "$FAKE/.claude/hooks/sync.demo.lock" \
         "$FAKE/.claude/state" \
         "$FAKE/.claude/skills/ai-brain-starter/hooks" \
         "$FAKE/.claude/skills/ai-brain-starter/scripts"
printf '{"hooks":{}}\n'      > "$FAKE/.claude/settings.json"
printf 'print("deployed")\n' > "$FAKE/.claude/hooks/real-hook.py"
printf 'log line\n'          > "$FAKE/.claude/hooks/cwd-changed.log"
printf 'sync line\n'         > "$FAKE/.claude/hooks/sync-my-skills.log"
printf '4242\n'              > "$FAKE/.claude/hooks/sync.demo.lock/pid"
printf '{"e":1}\n'           > "$FAKE/.claude/state/settings-hook-integrity.jsonl"

fp() { HOME="$FAKE" bash -c "source '$FN'; real_home_fingerprint"; }

BASE=$(fp)
[ -n "$BASE" ] || { echo "FAIL: empty fingerprint"; exit 1; }

echo "=== MUST NOT react: runtime noise the session machinery writes ==="
for f in ".claude/hooks/cwd-changed.log" \
         ".claude/hooks/sync-my-skills.log" \
         ".claude/hooks/sync.demo.lock/pid" \
         ".claude/state/settings-hook-integrity.jsonl"; do
  printf 'churn %s\n' "$RANDOM" >> "$FAKE/$f"
  touch "$FAKE/$f"
  if [ "$(fp)" = "$BASE" ]; then ok "ignores $(basename "$f")"; else no "reacted to $(basename "$f") (false RED source)"; fi
done

echo
echo "=== MUST still react: the escapes this guard exists to catch ==="
printf 'print("TAMPERED")\n' >> "$FAKE/.claude/hooks/real-hook.py"
if [ "$(fp)" != "$BASE" ]; then ok "catches a rewritten deployed hook (.py)"; else no "MISSED a rewritten deployed hook"; fi
BASE=$(fp)

printf 'print("x")\n' > "$FAKE/.claude/skills/ai-brain-starter/scripts/new-script.py"
if [ "$(fp)" != "$BASE" ]; then ok "catches a new file in the skill scripts tree"; else no "MISSED a new skill script"; fi
BASE=$(fp)

printf '{"hooks":{"tampered":1}}\n' > "$FAKE/.claude/settings.json"
if [ "$(fp)" != "$BASE" ]; then ok "catches a settings.json rewrite (the catastrophic one)"; else no "MISSED a settings.json rewrite"; fi
BASE=$(fp)

touch "$FAKE/.claude/settings.json.bak-20260101"
if [ "$(fp)" != "$BASE" ]; then ok "catches a new installer .bak-* (write-then-rollback)"; else no "MISSED a new .bak-*"; fi

echo
echo "test_home_fingerprint_noise: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ] || exit 1
