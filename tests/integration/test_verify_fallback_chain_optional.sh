#!/usr/bin/env bash
# Regression test for install-hooks-user-level.py --fail-on-missing on
# ||-fallback chains (MYC-2558).
#
# Bug: verify_paths_on_disk treated EVERY script path referenced in a hook
# command as REQUIRED. The detect-closing-signal hook is a fallback chain
#   python3 '<vault-copy>' 2>/dev/null || python3 <home-copy> 2>/dev/null || echo {...}
# On a healthy vault install the vault-side copy legitimately does NOT exist
# (only the ~/.claude home copy does). The verifier flagged the missing vault
# copy as REQUIRED -> --fail-on-missing exit 1 -> the auto-update told the user
# "the hook re-install didn't finish cleanly" every ~6-day cycle, forever, on
# otherwise-healthy macOS/Linux vault installs.
#
# Two defects are fixed, both exercised here:
#   1. Same-basename siblings in ONE command: when >=1 copy exists on disk the
#      chain is satisfied at runtime, so the missing sibling(s) are OPTIONAL,
#      not REQUIRED. Scoped to a single command; never across commands.
#   2. Quoting the first path used to leave `2>/dev/null` captured as a bogus
#      "missing required path" (the path-extraction regex grabbed the redirect
#      once the quoted script was stripped). It is no longer captured as a path.
#
# Asserts:
#   (a) a detect-closing-signal-shaped `||` chain where ONLY the ~/.claude home
#       copy exists on disk -> --fail-on-missing exits 0.  (the primary fix)
#   (b) NEGATIVE CONTROL: neither copy exists -> --fail-on-missing exits 1 and
#       the FAIL block names the missing script.  (proves the gate still bites)
#   (c) `2>/dev/null` never appears as a flagged path in the (a) report.
#
# Self-contained. Exit 0 = pass.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"

fail() { echo "FAIL: $1" >&2; exit 1; }

[[ -f "$INSTALLER" ]] || fail "installer missing at $INSTALLER"

# Throwaway HOME so we never touch the real ~/.claude/settings.json.
TMP_HOME="$(mktemp -d)"
trap 'rm -rf "$TMP_HOME"' EXIT

# Fake skill repo. is_abs_owned() matches the substring
# "ai-brain-starter/hooks/detect-closing-signal.py", so we mirror that layout.
SKILL_DIR="$TMP_HOME/.claude/skills/ai-brain-starter"
mkdir -p "$SKILL_DIR/hooks"

# The two copies the detect-closing-signal fallback chain references:
#   HOME_COPY  — ~/.claude/skills/... — guaranteed present on every install.
#   VAULT_COPY — inside the vault — legitimately absent on a vault install.
HOME_COPY="$SKILL_DIR/hooks/detect-closing-signal.py"
VAULT_COPY="$TMP_HOME/MyVault/.claude/skills/ai-brain-starter/hooks/detect-closing-signal.py"

# Only the home copy exists on disk — the healthy-vault-install state.
cat > "$HOME_COPY" <<'EOF'
#!/usr/bin/env python3
print('{"continue": true}')
EOF

# The real production command shape: quoted vault path + 2>/dev/null + `||`
# home path + `||` echo fallback. Keep the vault path quoted — that is what
# used to leave `2>/dev/null` captured as a bogus missing path, so the test
# bites defect #2 as well as #1. Inlined in the heredoc (not via a shell var)
# so the `\"` JSON escapes survive: an unquoted heredoc only treats
# $ ` \ newline as special, so `\"` is preserved verbatim while $VAULT_COPY /
# $HOME_COPY expand and the single quotes stay literal.
cat > "$SKILL_DIR/hooks.json" <<EOF
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 '$VAULT_COPY' 2>/dev/null || python3 $HOME_COPY 2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'"
          }
        ]
      }
    ]
  }
}
EOF

SETTINGS_PATH="$TMP_HOME/.claude/settings.json"

# --- (a) only the home copy exists -> exit 0 -------------------------------
echo "{}" > "$SETTINGS_PATH"
set +e
OUT=$(python3 "$INSTALLER" --hooks-source "$SKILL_DIR/hooks.json" \
        --settings "$SETTINGS_PATH" --fail-on-missing 2>&1)
RC=$?
set -e
[[ $RC -eq 0 ]] || fail "(a) healthy vault install (home copy present, vault copy absent) should exit 0, got $RC. Output:
$OUT"

# --- (c) 2>/dev/null must never be captured as a script path --------------
if echo "$OUT" | grep -qE 'dev/null'; then
    fail "(c) '2>/dev/null' was captured as a path in the verification report:
$OUT"
fi

# --- (b) NEGATIVE CONTROL: neither copy exists -> exit 1 ------------------
rm -f "$HOME_COPY"
echo "{}" > "$SETTINGS_PATH"
set +e
OUT=$(python3 "$INSTALLER" --hooks-source "$SKILL_DIR/hooks.json" \
        --settings "$SETTINGS_PATH" --fail-on-missing 2>&1)
RC=$?
set -e
[[ $RC -eq 1 ]] || fail "(b) NEGATIVE CONTROL: neither copy present should exit 1, got $RC. Output:
$OUT"
echo "$OUT" | grep -q "FAIL.*hook(s) — script not on disk" || \
    fail "(b) expected FAIL line naming the missing script. Got:
$OUT"
echo "$OUT" | grep -qF "detect-closing-signal.py" || \
    fail "(b) expected the missing detect-closing-signal.py path in the FAIL block. Got:
$OUT"

echo "PASS: ||-fallback chain optional-classification (MYC-2558) — home-only install rc 0; neither-copy negative control rc 1; no 2>/dev/null false-capture"
