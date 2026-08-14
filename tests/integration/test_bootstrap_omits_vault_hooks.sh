#!/usr/bin/env bash
# Test scripts/install-hooks-user-level.py does NOT wire the vault-content hooks
# into ~/.claude/settings.json when there is no vault yet (no --vault-path).
#
# The bug (MYC-739, surfaced by the 2026-06-09 install workshop): with no
# --vault-path, normalize_path_substitutions() rewrote every "[VAULT_PATH]" to
# "$HOME", turning the three vault-content hooks
#     bash '[VAULT_PATH]/⚙️ Meta/scripts/graph-context-hook.sh'
#     bash '[VAULT_PATH]/⚙️ Meta/scripts/session-end-hook.sh'
#     bash '[VAULT_PATH]/⚙️ Meta/scripts/write-hook.sh'
# into  bash '$HOME/⚙️ Meta/scripts/*.sh'  — paths that DO NOT EXIST until the
# vault is created. They then error on every prompt / write / session-end and
# dump a "how do you want to remove these?" decision on a non-technical user.
# phase-00-install.md line ~79 already says "Bootstrap does NOT touch Hooks
# (vault-path-dependent; installed by /setup-brain proper)"; /setup-brain
# phase-05 wires these three with the REAL vault path. So at bootstrap time
# (no vault) they must simply be OMITTED.
#
# Asserts:
#   1. With NO --vault-path: NO wired hook command references "⚙️ Meta/scripts/"
#      (none of the three vault-content hooks were written). Read from the
#      PARSED commands, not the raw file — see hook_commands_matching below.
#   2. With NO --vault-path: the hooks that resolve correctly without a vault
#      (detect-closing-signal.py, which has a ~/.claude fallback) ARE still
#      wired — the omission is surgical, not a blanket skip of UserPromptSubmit.
#   3. NEGATIVE CONTROL — WITH --vault-path: all three vault-content hooks ARE
#      written, substituted to the real vault path (proves they are deferred,
#      not deleted, and that the path substitution still works).
#   4. settings.json is valid JSON after every write.
#
# Self-contained; never writes outside its tmpdir.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"
HOOKS_SRC="$REPO_ROOT/hooks.json"

for f in "$INSTALLER" "$HOOKS_SRC"; do
  [ -f "$f" ] || { echo "ERROR: $f not found" >&2; exit 1; }
done

# Sanity: the template really does carry the three vault-content hooks we are
# asserting about. If hooks.json is refactored to drop them, this guard makes
# the test fail loudly instead of passing vacuously.
for marker in graph-context-hook.sh session-end-hook.sh write-hook.sh; do
  grep -q "$marker" "$HOOKS_SRC" || { echo "ERROR: hooks.json no longer references $marker — test premise stale" >&2; exit 1; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
sandbox_home "$TMP"
fail() { echo "FAIL: $1" >&2; exit 1; }

# Ask the PARSED hook commands, never the file's raw bytes.
#
# settings.json is written ASCII-escaped (\uXXXX) so that no Windows code page
# can corrupt a path stored in it (#397) — which means a raw
# `grep "⚙️ Meta/scripts/"` over the file can no longer match. Assert 1 below is
# a check that must be ABLE to fail, so a grep that silently never matches would
# turn this whole guard into a no-op reporting green forever. These assertions
# were always about what got WIRED, not about how the file encodes it.
#
# Prints every matching command; exits 0 if any matched, 1 if none, so it reads
# like `grep -q` at the call sites. Exercised in BOTH directions in this file
# (assert 1 needs no match, assert 3 needs one), so it cannot go quietly dead.
hook_commands_matching() {
  python3 - "$1" "$2" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
needle, hits = sys.argv[2], []
for groups in (doc.get("hooks") or {}).values():
    for group in groups:
        for hook in group.get("hooks", []):
            command = hook.get("command", "")
            if needle in command:
                hits.append(command)
for hit in hits:
    print(hit)
sys.exit(0 if hits else 1)
PY
}

# ── 1 + 2. No --vault-path: vault-content hooks OMITTED, fallback hooks KEPT ──
SETTINGS_NOVAULT="$TMP/settings-novault.json"
echo '{}' > "$SETTINGS_NOVAULT"
# A fresh workshop machine has NO VAULT_ROOT in the environment — the installer's
# --vault-path defaults to os.environ.get("VAULT_ROOT"). Clear it so this run
# faithfully reproduces the no-vault bootstrap case (otherwise an ambient
# VAULT_ROOT on the developer's box masks the bug).
OUT="$(env -u VAULT_ROOT python3 "$INSTALLER" --hooks-source "$HOOKS_SRC" --settings "$SETTINGS_NOVAULT" 2>&1)" \
  || { echo "$OUT"; fail "installer (no --vault-path) exited non-zero"; }

python3 -c "import json; json.load(open('$SETTINGS_NOVAULT'))" \
  || { echo "$OUT"; fail "4: settings.json not valid JSON after no-vault install"; }

if OFFENDING="$(hook_commands_matching "$SETTINGS_NOVAULT" "⚙️ Meta/scripts/")"; then
  echo "--- offending settings.json entries ---" >&2
  echo "$OFFENDING" >&2
  fail "1: vault-content hook(s) wired with no vault — these resolve to a dead \$HOME/⚙️ Meta/scripts/ path and error on every event"
fi

# The closing-signal hook carries a ~/.claude/skills fallback, so it works with
# no vault and MUST still be wired (proves the omission targeted only the
# fallback-less vault-content hooks).
grep -q "detect-closing-signal.py" "$SETTINGS_NOVAULT" \
  || fail "2: detect-closing-signal.py was dropped — omission was not surgical"

# ── 3. NEGATIVE CONTROL — WITH --vault-path: all three ARE written ──
VAULT="$TMP/MyBrain"
mkdir -p "$VAULT/⚙️ Meta/scripts"
SETTINGS_VAULT="$TMP/settings-vault.json"
echo '{}' > "$SETTINGS_VAULT"
OUT2="$(python3 "$INSTALLER" --hooks-source "$HOOKS_SRC" --settings "$SETTINGS_VAULT" --vault-path "$VAULT" 2>&1)" \
  || { echo "$OUT2"; fail "installer (with --vault-path) exited non-zero"; }

python3 -c "import json; json.load(open('$SETTINGS_VAULT'))" \
  || { echo "$OUT2"; fail "4: settings.json not valid JSON after with-vault install"; }

for marker in graph-context-hook.sh session-end-hook.sh write-hook.sh; do
  grep -q "$marker" "$SETTINGS_VAULT" \
    || { echo "$OUT2"; fail "3 (negative control): $marker NOT wired even WITH --vault-path — they were deleted, not deferred"; }
done
# And they must carry the real vault path, not the [VAULT_PATH] placeholder.
hook_commands_matching "$SETTINGS_VAULT" "$VAULT/⚙️ Meta/scripts/" >/dev/null \
  || { echo "$OUT2"; fail "3: vault-content hooks not substituted to the real vault path"; }
grep -q "\[VAULT_PATH\]" "$SETTINGS_VAULT" \
  && fail "3: unresolved [VAULT_PATH] placeholder left in settings.json"

echo "PASS: vault-content hooks omitted with no vault, wired with a vault (test_bootstrap_omits_vault_hooks)"
