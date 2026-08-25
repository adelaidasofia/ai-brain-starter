#!/usr/bin/env bash
# Test: detect-closing-signal.py must never end a session in SILENCE, and must
# say so when the cascade will write into a DIFFERENT vault than the one the
# session is working in.
#
# Two bug classes, both in the SILENT-NO-OP family:
#
#  (1) UNSCAFFOLDED VAULT — when the resolved vault root has no Meta dir,
#      verify_meta_dir() correctly refuses to emit paths (a phantom meta_dir
#      would swallow the model's writes). But it refused by calling log_debug()
#      — gated behind CLOSING_SIGNAL_DEBUG=1 — and then emit_passthrough().
#      With debug off (every real session), the user said "bye", the cascade
#      did nothing, and NOTHING anywhere said why. A silent no-op and a healthy
#      close are indistinguishable from the outside. Reported 2026-08-18 by a
#      student whose client folder closed "fine" for weeks writing nothing.
#
#  (2) HETEROTOPIC RESOLUTION — VAULT_ROOT is normally set once, globally, in
#      settings.json. A session in a folder that does NOT declare its own
#      cascade therefore resolves to that OTHER vault and pre-builds its
#      session file there. The emitted block printed a "Vault root:" that read
#      perfectly normal, so one project's session notes land in another
#      project's vault with no signal. The RESOLUTION is intended and is left
#      unchanged (see test_detect_closing_signal_repo_aware_vault.sh assertion
#      4, which pins that fallback); only the silence is fixed.
#
# Assertions:
#   1. No Meta dir + no VAULT_ROOT: emits a visible CANNOT-RUN notice naming
#      the reason and the remediation — NOT a bare passthrough.
#   2. That notice does not claim the cascade ran.
#   3. Offsite resolution (bare cwd, VAULT_ROOT elsewhere): cascade emits AND
#      carries the heterotopic warning.
#   4. NEGATIVE CONTROL — a folder declaring its own cascade (Meta/ + a
#      "## Session End" CLAUDE.md): cascade emits with NO warning.
#   5. NEGATIVE CONTROL — cwd IS the configured vault root: no warning.
#   6. NEGATIVE CONTROL — a worktree inside a self-declaring vault: no warning
#      (the collapse must happen before the comparison, or every worktree
#      close would cry wolf).
#
# Negative controls 4-6 exist because a warning that always fires is worth
# nothing: the first draft of this fix raised NameError on collapse_worktree,
# which the hook's catch-all turned into a passthrough — every case looked
# "quiet and fine" and only the negative controls exposed it.
#
# Self-contained: tmpdir fake vaults, HOME sandboxed. Exit 0 = pass.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
HOOK="$REPO_ROOT/hooks/detect-closing-signal.py"
if [ ! -f "$HOOK" ]; then
  echo "ERROR: $HOOK not found" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
sandbox_home "$TMP/home"

mkdir -p "$TMP/bare"
mkdir -p "$TMP/default/⚙️ Meta"
mkdir -p "$TMP/own/Meta" "$TMP/own/.claude/worktrees/slug"
printf '# Own\n\n## Session End\n\nits own cascade\n' > "$TMP/own/CLAUDE.md"

fails=0

# Emit the hook's additionalContext ("" when it passed through).
ctx() { # $1 = cwd, $2 = VAULT_ROOT ("" to unset)
  local cwd="$1" vr="${2:-}"
  local payload
  payload="$(printf '{"prompt":"ok bye","session_id":"t","cwd":"%s"}' "$cwd")"
  if [ -z "$vr" ]; then
    printf '%s' "$payload" | env -u VAULT_ROOT python3 "$HOOK"
  else
    printf '%s' "$payload" | VAULT_ROOT="$vr" python3 "$HOOK"
  fi | python3 -c 'import json,sys; print(json.load(sys.stdin).get("hookSpecificOutput",{}).get("additionalContext",""))'
}

check() { # $1=label $2=haystack $3=needle $4=expect(yes|no)
  local got="no"
  case "$2" in *"$3"*) got="yes";; esac
  if [ "$got" != "$4" ]; then
    echo "FAIL: $1 — expected '$3' present=$4, got present=$got" >&2
    fails=$((fails + 1))
  fi
}

# 1 + 2. Unscaffolded, no VAULT_ROOT — must be loud, must not claim success.
out="$(ctx "$TMP/bare" "")"
if [ -z "$out" ]; then
  echo "FAIL: unscaffolded vault produced a silent passthrough (the bug)" >&2
  fails=$((fails + 1))
fi
check "unscaffolded notice"     "$out" "cascade CANNOT RUN here" yes
check "notice names remediation" "$out" "## Session End"          yes
check "notice does not run cascade" "$out" "PHASE 0b"             no

# 3. Offsite resolution must warn.
out="$(ctx "$TMP/bare" "$TMP/default")"
check "offsite cascade emitted" "$out" "PHASE 0b"                yes
check "offsite warns"           "$out" "HETEROTOPIC RESOLUTION"  yes

# 4. Negative control: folder owns its cascade.
out="$(ctx "$TMP/own" "$TMP/default")"
check "own-vault cascade emitted" "$out" "PHASE 0b"               yes
check "own-vault does NOT warn"   "$out" "HETEROTOPIC RESOLUTION" no

# 5. Negative control: cwd is the vault root itself.
out="$(ctx "$TMP/default" "$TMP/default")"
check "vault-root cwd does NOT warn" "$out" "HETEROTOPIC RESOLUTION" no

# 6. Negative control: worktree inside a self-declaring vault.
out="$(ctx "$TMP/own/.claude/worktrees/slug" "$TMP/default")"
check "worktree does NOT warn" "$out" "HETEROTOPIC RESOLUTION" no

if [ "$fails" -gt 0 ]; then
  echo "FAILED: $fails assertion(s)" >&2
  exit 1
fi
echo "PASS: close cascade fails loud on an unscaffolded vault, and flags offsite resolution"
