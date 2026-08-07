#!/usr/bin/env bash
# Test: sync-vault-scripts.sh — the skill->vault script sync.
#
# Bug class it guards: a vault's <meta>/scripts/ was populated once at setup and
# never re-synced, so fixed/new scripts (session-close-runner.sh,
# check-rule-conflicts.py, drift-detection.py, passive-capture.py, ...) never
# reached existing vaults. This is the skill->vault half of sync-skills.sh.
#
# Assertions:
#   1. The VAULT_SCRIPTS manifest is IMPORT-CLOSED — no manifest .py imports a
#      sibling scripts/*.py module that isn't also in the manifest (else it
#      would crash at runtime in the vault).
#   2. A fresh sync populates <meta>/scripts/ with the core scripts.
#   3. A re-run is idempotent (0 created / 0 updated).
#   4. A locally-edited vault script is backed up to .bak before overwrite, then
#      restored to the repo version (non-destructive contract).
#   5. A symlinked scripts dir is left untouched (maintainer live-edit workflow).
#   6. --dry-run writes nothing.
#   7. An unresolvable vault is a NON-FATAL no-op (exit 0).
#
# Self-contained: tmpdir fake vaults. Exit 0 = pass.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SYNC="$REPO_ROOT/scripts/sync-vault-scripts.sh"
if [ ! -f "$SYNC" ]; then
  echo "ERROR: $SYNC not found" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- 1. manifest import-closure -------------------------------------------
python3 - "$REPO_ROOT" "$SYNC" <<'PY'
import ast, os, re, sys
repo, sync = sys.argv[1], sys.argv[2]
lines = open(sync, encoding="utf-8").read().splitlines()
start = next((i for i, l in enumerate(lines) if "VAULT_SCRIPTS=(" in l), None)
assert start is not None, "VAULT_SCRIPTS array not found"
names = []
for l in lines[start + 1:]:
    if l.strip() == ")":
        break
    m = re.search(r'"([^"]+)"', l)
    if m:
        names.append(m.group(1))
assert names, "manifest is empty"
py = [n for n in names if n.endswith(".py")]
mod_names = {n[:-3] for n in py}
missing = []
for n in py:
    p = os.path.join(repo, "scripts", n)
    if not os.path.isfile(p):
        continue  # source-absent on this checkout (e.g. pre-merge) — skip
    try:
        tree = ast.parse(open(p, encoding="utf-8").read())
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods = [node.module.split(".")[0]]
        for mod in mods:
            sib = os.path.join(repo, "scripts", mod + ".py")
            if os.path.isfile(sib) and mod not in mod_names:
                missing.append(f"{n} imports sibling '{mod}' (scripts/{mod}.py) not in manifest")
if missing:
    print("IMPORT-CLOSURE FAIL:")
    for x in missing:
        print("  -", x)
    sys.exit(1)
print(f"PASS: manifest import-closed ({len(py)} py scripts checked, {len(names)} total)")
PY

# --- 2. fresh sync populates <meta>/scripts/ ------------------------------
VAULT="$TMP/vault"; mkdir -p "$VAULT/⚙️ Meta"
bash "$SYNC" --vault "$VAULT" --quiet >/dev/null
for s in _meta_resolver.py aggregate-sessions.py check-rule-conflicts.py; do
  if [ ! -f "$VAULT/⚙️ Meta/scripts/$s" ]; then
    echo "FAIL: $s was not synced into the vault" >&2; exit 1
  fi
done
echo "PASS: fresh sync populates <meta>/scripts/"

# --- 3. re-run is idempotent ----------------------------------------------
OUT="$(bash "$SYNC" --vault "$VAULT")"
if ! printf '%s\n' "$OUT" | grep -qE "Created:[[:space:]]*0"; then
  echo "FAIL: re-run created files (not idempotent)" >&2; printf '%s\n' "$OUT" >&2; exit 1
fi
if ! printf '%s\n' "$OUT" | grep -qE "Updated:[[:space:]]*0"; then
  echo "FAIL: re-run updated files (not idempotent)" >&2; printf '%s\n' "$OUT" >&2; exit 1
fi
echo "PASS: re-run is idempotent (0 created / 0 updated)"

# --- 4. local edit is backed up before overwrite, then restored -----------
echo "# local customization" >> "$VAULT/⚙️ Meta/scripts/check-rule-conflicts.py"
bash "$SYNC" --vault "$VAULT" --quiet >/dev/null
if ! ls "$VAULT/⚙️ Meta/scripts/"check-rule-conflicts.py.bak-* >/dev/null 2>&1; then
  echo "FAIL: no .bak created for the locally-edited script" >&2; exit 1
fi
if ! cmp -s "$REPO_ROOT/scripts/check-rule-conflicts.py" "$VAULT/⚙️ Meta/scripts/check-rule-conflicts.py"; then
  echo "FAIL: edited script was not restored to the repo version" >&2; exit 1
fi
echo "PASS: local edit backed up to .bak, then updated to the repo version"

# --- 5. symlinked scripts dir is skipped ----------------------------------
VSYM="$TMP/vaultsym"; mkdir -p "$VSYM/⚙️ Meta" "$TMP/elsewhere"
ln -s "$TMP/elsewhere" "$VSYM/⚙️ Meta/scripts"
bash "$SYNC" --vault "$VSYM" >/dev/null 2>&1 || true
if [ -n "$(ls -A "$TMP/elsewhere" 2>/dev/null)" ]; then
  echo "FAIL: wrote through a symlinked scripts dir" >&2; exit 1
fi
echo "PASS: symlinked scripts dir is skipped (maintainer workflow)"

# --- 6. --dry-run writes nothing ------------------------------------------
VDRY="$TMP/vaultdry"; mkdir -p "$VDRY/⚙️ Meta"
bash "$SYNC" --vault "$VDRY" --dry-run >/dev/null
if [ -d "$VDRY/⚙️ Meta/scripts" ] && [ -n "$(ls -A "$VDRY/⚙️ Meta/scripts" 2>/dev/null)" ]; then
  echo "FAIL: --dry-run wrote files" >&2; exit 1
fi
echo "PASS: --dry-run writes nothing"

# --- 7. unresolvable vault is a non-fatal no-op ---------------------------
env -u VAULT_ROOT HOME="$TMP/emptyhome" bash "$SYNC" --quiet >/dev/null 2>&1
echo "PASS: unresolvable vault is a non-fatal no-op (exit 0)"

# --- 8. vault resolved from settings.json (the arg-less auto-update path) --
SVAULT="$TMP/svault"; mkdir -p "$SVAULT/⚙️ Meta"
SHOME="$TMP/shome"; mkdir -p "$SHOME/.claude"
cat > "$SHOME/.claude/settings.json" <<JSON
{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"bash '$SVAULT/⚙️ Meta/scripts/session-end-hook.sh'"}]}]}}
JSON
env -u VAULT_ROOT HOME="$SHOME" bash "$SYNC" --quiet >/dev/null
if [ ! -f "$SVAULT/⚙️ Meta/scripts/_meta_resolver.py" ]; then
  echo "FAIL: vault not resolved from settings.json (nothing synced)" >&2; exit 1
fi
echo "PASS: vault resolved from settings.json and synced arg-lessly"

echo
echo "All assertions passed. sync-vault-scripts.sh contract holds."
