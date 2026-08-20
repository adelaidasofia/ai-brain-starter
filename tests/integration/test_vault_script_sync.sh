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
#      local module that will not exist next to it in the vault (else it would
#      crash at runtime there).
#
#      SCOPE NOTE: this check used to resolve a dependency ONLY as
#      `scripts/<mod>.py` — a FILE, in ONE directory. Both halves were blind
#      spots, and build-journal-index.py fell through the gap on both:
#        (a) it imports `_lib.safe_read`, and `_lib` is a PACKAGE DIRECTORY, not
#            a .py file, so `os.path.isfile(scripts/_lib.py)` was False and the
#            check skipped it entirely;
#        (b) `_lib` does not live in scripts/ at all — the script reaches it by
#            sys.path.insert-ing ../hooks, a directory this check never looked in.
#      Net effect: the gate reported "manifest import-closed" while the synced
#      vault copy died at import with `ModuleNotFoundError: No module named
#      '_lib'`, so /weekly and /monthly rebuilt no journal index at all and the
#      stale journal-index.json sat there looking fine. A guard's resolution
#      rule IS its blind spot: resolve a dep the way PYTHON does (file OR
#      package, across every directory the script puts on sys.path), or the
#      gate is green by construction.
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

# The _lib PACKAGE deps, synced separately because the manifest above is a flat
# list of scripts/ filenames and cannot express a package. Presence of a module
# here means <meta>/scripts/_lib/<mod>.py exists in a synced vault.
lib_start = next((i for i, l in enumerate(lines) if "VAULT_LIB_MODULES=(" in l), None)
lib_mods = []
if lib_start is not None:
    for l in lines[lib_start + 1:]:
        if l.strip() == ")":
            break
        m = re.search(r'"([^"]+)"', l)
        if m:
            lib_mods.append(m.group(1))
SYNCED_PACKAGES = {"_lib"} if lib_mods else set()

# Directories a manifest script may pull onto sys.path. scripts/ is the sibling
# dir; hooks/ is reached via `sys.path.insert(..., parent.parent / "hooks")`.
# A dep found in ANY of these exists in the REPO but is not necessarily synced —
# only manifest entries are, and only scripts/ files can BE manifest entries.
SEARCH_DIRS = ("scripts", "hooks")


def resolve_local(mod):
    """Where does `mod` live in this repo? Resolve like Python: file OR package.

    Returns (kind, reldir) or None. `kind` is "module" (mod.py) or "package"
    (mod/__init__.py) — a package is the case the old isfile() check missed.
    """
    for d in SEARCH_DIRS:
        if os.path.isfile(os.path.join(repo, d, mod + ".py")):
            return ("module", d)
        if os.path.isfile(os.path.join(repo, d, mod, "__init__.py")):
            return ("package", d)
    return None


missing = []
for n in py:
    p = os.path.join(repo, "scripts", n)
    if not os.path.isfile(p):
        continue  # source-absent on this checkout (e.g. pre-merge) — skip
    try:
        src = open(p, encoding="utf-8").read()
        tree = ast.parse(src)
    except SyntaxError:
        continue
    # An import inside a try/except ImportError is a DELIBERATE optional dep with
    # a fallback — that is the sanctioned way to stay import-closed while still
    # using a shared primitive in the repo. Collect those and exempt them.
    guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        handles_import_error = any(
            (h.type is None)
            or (isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "Exception"))
            or (isinstance(h.type, ast.Tuple)
                and any(isinstance(e, ast.Name) and e.id in ("ImportError", "Exception")
                        for e in h.type.elts))
            for h in node.handlers
        )
        if not handles_import_error:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Import):
                guarded.update(a.name.split(".")[0] for a in sub.names)
            elif isinstance(sub, ast.ImportFrom) and sub.level == 0 and sub.module:
                guarded.add(sub.module.split(".")[0])

    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods = [node.module.split(".")[0]]
        for mod in mods:
            if mod in guarded or mod in mod_names:
                continue
            found = resolve_local(mod)
            if not found:
                continue  # stdlib or third-party — not our problem
            kind, where = found
            loc = f"{where}/{mod}.py" if kind == "module" else f"{where}/{mod}/"
            if where == "scripts":
                missing.append(
                    f"{n} imports local {kind} '{mod}' ({loc}) not in manifest — "
                    f"add it to VAULT_SCRIPTS")
            elif mod in SYNCED_PACKAGES:
                continue  # mirrored into <meta>/scripts/_lib/ by VAULT_LIB_MODULES
            else:
                # Not in scripts/, so it cannot be a VAULT_SCRIPTS entry (that
                # manifest is flat filenames). Do NOT suggest a try/except fallback
                # with a hand-rolled reader: check-cloud-safe-file-walkers.py
                # refuses to trust a locally-defined safe_read_text ("bogus
                # safe_read module is not trusted"), so that "fix" trades an
                # import crash for an unaudited read path. Mirror the real module.
                missing.append(
                    f"{n} imports local {kind} '{mod}' from {loc}, which is NOT synced "
                    f"to the vault, so the synced copy dies at import. Add it to "
                    f"VAULT_LIB_MODULES in sync-vault-scripts.sh (it must be "
                    f"stdlib-only), which mirrors it to <meta>/scripts/{mod}/.")
if missing:
    print("IMPORT-CLOSURE FAIL:")
    for x in missing:
        print("  -", x)
    sys.exit(1)
print(f"PASS: manifest import-closed ({len(py)} py scripts checked, {len(names)} total)")
PY

# --- 1b. manifest SOURCE-closure (shell) ----------------------------------
# Section 1 walks Python ASTs and reports "N py scripts checked". Shell scripts
# were never scanned, so a manifest .sh could `source` a sibling that the sync
# does not carry — and did.
#
# Measured incident: `vault-safe-commit.sh` and `session-end-hook.sh` are both in
# the manifest and both source `_session_close_guard.sh`, which is NOT. The sync
# copied the consumers without the dependency. `vault-safe-commit.sh` falls back
# to a fail-closed stub, so EVERY vault commit refused — and it is the only route
# past the raw-git block guard, so committing stopped entirely. `session-end-hook.sh`
# falls back to "defer", so every session-end snapshot silently no-opped.
#
# Both the direct form (`. "$SCRIPT_DIR/x.sh"`) and the indirect one
# (`GUARD="$SCRIPT_DIR/x.sh"` … `. "$GUARD"`) must be caught: the indirect form is
# the one that shipped the outage.
python3 - "$REPO_ROOT" "$SYNC" <<'PY'
import os, re, sys
repo, sync = sys.argv[1], sys.argv[2]
lines = open(sync, encoding="utf-8").read().splitlines()
start = next((i for i, l in enumerate(lines) if "VAULT_SCRIPTS=(" in l), None)
assert start is not None, "VAULT_SCRIPTS array not found"
names = []
for l in lines[start + 1:]:
    if l.strip() == ")":
        break
    m = re.match(r'\s*"([^"]+)"', l)
    if m:
        names.append(m.group(1))

DIRECT = re.compile(
    r'^\s*(?:\.|source)\s+"?\$\{?(?:SCRIPT_DIR|SCRIPTS|SCRIPT_ROOT|HERE)\}?/([A-Za-z0-9_.-]+\.sh)"?',
    re.M)
ASSIGN = re.compile(
    r'^\s*[A-Za-z_][A-Za-z0-9_]*=\s*"?\$\{?(?:SCRIPT_DIR|SCRIPTS|SCRIPT_ROOT|HERE)\}?/([A-Za-z0-9_.-]+\.sh)"?',
    re.M)

sh = [n for n in names if n.endswith(".sh")]
missing, checked = [], 0
for n in sh:
    p = os.path.join(repo, "scripts", n)
    if not os.path.exists(p):
        continue  # source-absent on this checkout (e.g. pre-merge) — skip
    checked += 1
    text = open(p, encoding="utf-8", errors="replace").read()
    for dep in sorted(set(DIRECT.findall(text)) | set(ASSIGN.findall(text))):
        if dep in names:
            continue
        where = os.path.join(repo, "scripts", dep)
        if not os.path.exists(where):
            missing.append(
                f"{n} sources sibling '{dep}', which is not in the manifest AND "
                f"does not exist in scripts/ — dangling reference.")
        else:
            missing.append(
                f"{n} sources sibling '{dep}' from scripts/{dep}, which is NOT in "
                f"VAULT_SCRIPTS, so the synced vault copy falls back to its stub. "
                f"Add '{dep}' to the manifest.")
if missing:
    print("SOURCE-CLOSURE FAIL:")
    for x in missing:
        print("  -", x)
    sys.exit(1)
print(f"PASS: manifest source-closed ({checked} sh scripts checked, {len(names)} total)")
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
