#!/usr/bin/env bash
# Test: hooks/validate-handoff-frontmatter.py — the handoff `consumes_when:` guard.
#
# Covers issue #375 bug 3. The guard shipped for months but was never registered,
# and even when hand-wired it was inert: VAULT_ROOT defaulted to ~/vault, so
# in_vault() was False for every real vault not literally named "vault".
#
# The controls below FAIL against the pre-fix hook:
#   - vault-autodetected-*  : pre-fix resolved ~/vault, so it never fired
#   - deny-is-json-*        : pre-fix wrote stderr + exit 2, which hooks.json's
#                             `|| echo {allow}` wrapper rewrites into an ALLOW
#   - multiedit-*           : pre-fix ignored MultiEdit entirely
set -uo pipefail

# HERMETIC: the running account's real $VAULT_ROOT must not leak into the
# autodetection controls (it points at a DIFFERENT vault and silently answered
# for every case while this test was being written).
unset VAULT_ROOT

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/validate-handoff-frontmatter.py"
PASS=0; FAIL=0
ok()  { echo "PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "FAIL  $1 (expected: $2, got: $3)"; FAIL=$((FAIL+1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# A vault is a dir containing a Meta-suffixed folder. Deliberately NOT named
# "vault", which is the whole point of the bug.
VAULT="$TMP/NotCalledVault"
mkdir -p "$VAULT/⚙️ Meta/Handoffs"
# A code repo: has CLAUDE.md but NO Meta dir. Must never be treated as a vault.
REPO="$TMP/some-code-repo"
mkdir -p "$REPO"; echo "# code" > "$REPO/CLAUDE.md"

# decision(): run the hook and print allow / deny / crash.
decision() {
  local out rc
  out="$(printf '%s' "$1" | python3 "$HOOK" 2>/dev/null)"; rc=$?
  [ $rc -ne 0 ] && { echo "crash"; return; }
  printf '%s' "$out" | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["hookSpecificOutput"]["permissionDecision"])
except Exception: print("nojson")'
}

write_payload() {  # $1 path  $2 content
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Write","tool_input":{"file_path":sys.argv[1],"content":sys.argv[2]}}))' "$1" "$2"
}

BAD_HANDOFF='---
type: handoff
consumes_when:
---
body'
GOOD_HANDOFF='---
type: handoff
consumes_when: PR #402 merged to main
---
body'

echo "=== 1. the bug: a real vault NOT named 'vault' is now detected ==="
r=$(decision "$(write_payload "$VAULT/⚙️ Meta/Handoffs/h.md" "$BAD_HANDOFF")")
[ "$r" = "deny" ] && ok "vault-autodetected-denies-missing-consumes_when" \
  || bad "vault-autodetected-denies-missing-consumes_when" deny "$r"

r=$(decision "$(write_payload "$VAULT/⚙️ Meta/Handoffs/h.md" "$GOOD_HANDOFF")")
[ "$r" = "allow" ] && ok "vault-autodetected-allows-valid-consumes_when" \
  || bad "vault-autodetected-allows-valid-consumes_when" allow "$r"

echo "=== 2. deny speaks JSON on stdout (exit 0), not stderr+exit 2 ==="
out="$(write_payload "$VAULT/⚙️ Meta/Handoffs/h.md" "$BAD_HANDOFF" | python3 "$HOOK" 2>/dev/null)"; rc=$?
[ $rc -eq 0 ] && ok "deny-is-json-exit-0" || bad "deny-is-json-exit-0" 0 "$rc"
printf '%s' "$out" | grep -q '"permissionDecision": *"deny"' \
  && ok "deny-is-json-payload" || bad "deny-is-json-payload" "deny json" "$out"

echo "=== 3. scoping: a code repo with CLAUDE.md but no Meta dir is NOT a vault ==="
r=$(decision "$(write_payload "$REPO/some-handoff.md" "$BAD_HANDOFF")")
[ "$r" = "allow" ] && ok "code-repo-not-treated-as-vault" \
  || bad "code-repo-not-treated-as-vault" allow "$r"

echo "=== 4. fail OPEN when no vault can be identified ==="
r=$(decision "$(write_payload "$TMP/loose-handoff.md" "$BAD_HANDOFF")")
[ "$r" = "allow" ] && ok "no-vault-fails-open" || bad "no-vault-fails-open" allow "$r"

echo "=== 5. \$VAULT_ROOT is the FALLBACK for a vault with no Meta folder ==="
FLAT="$TMP/FlatVault"; mkdir -p "$FLAT"
r=$(VAULT_ROOT="$FLAT" decision "$(write_payload "$FLAT/some-handoff.md" "$BAD_HANDOFF")")
[ "$r" = "deny" ] && ok "vault-root-fallback-honored" || bad "vault-root-fallback-honored" deny "$r"

# ...and detection WINS over a $VAULT_ROOT naming a different vault, so a
# multi-vault machine still guards every vault, not just the one in the env.
OTHER="$TMP/OtherVault"; mkdir -p "$OTHER/⚙️ Meta"
r=$(VAULT_ROOT="$OTHER" decision "$(write_payload "$VAULT/⚙️ Meta/Handoffs/h.md" "$BAD_HANDOFF")")
[ "$r" = "deny" ] && ok "detection-beats-mismatched-vault-root" \
  || bad "detection-beats-mismatched-vault-root" deny "$r"

echo "=== 6. MultiEdit is covered (was a free bypass) ==="
TARGET="$VAULT/⚙️ Meta/Handoffs/existing.md"
printf '%s' "$GOOD_HANDOFF" > "$TARGET"
ME=$(python3 -c 'import json,sys; print(json.dumps({"tool_name":"MultiEdit","tool_input":{"file_path":sys.argv[1],"edits":[{"old_string":"consumes_when: PR #402 merged to main","new_string":"consumes_when:"}]}}))' "$TARGET")
r=$(decision "$ME")
[ "$r" = "deny" ] && ok "multiedit-denies-blanking-consumes_when" \
  || bad "multiedit-denies-blanking-consumes_when" deny "$r"

echo "=== 7. non-handoff files in the vault are untouched ==="
r=$(decision "$(write_payload "$VAULT/⚙️ Meta/notes.md" '---
type: decision
---
body')")
[ "$r" = "allow" ] && ok "non-handoff-allowed" || bad "non-handoff-allowed" allow "$r"

echo "=== 8. bypass env ==="
r=$(HANDOFF_FRONTMATTER_BYPASS=1 decision "$(write_payload "$VAULT/⚙️ Meta/Handoffs/h.md" "$BAD_HANDOFF")")
[ "$r" = "allow" ] && ok "bypass-env-allows" || bad "bypass-env-allows" allow "$r"

echo
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
