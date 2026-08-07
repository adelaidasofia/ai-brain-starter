#!/usr/bin/env bash
# Negative-control suite for scripts/check-vault-root-reads.py - the class-level
# ban on naive VAULT_ROOT env reads (MYC-2505).
#
# WHY THIS EXISTS
#   A globally-exported VAULT_ROOT names exactly ONE vault. Code that reads it
#   naively fails two ways, both silently: UNSET it defaults to ~/vault (inert on
#   every vault not literally named "vault"), and SET it overrides the vault the
#   caller actually meant. hooks/validate-handoff-frontmatter.py shipped that way
#   and was inert on every install (#375/#404).
#
#   The guard is the third layer of immediate remediation -> per-script hardening
#   -> class-level watchdog, and the twin of scripts/check-meta-resolution.sh.
#   A guard that is only ever seen PASS is worthless, so every claim it makes has
#   a control here that proves it still bites.
#
# THIS SUITE ALSO FAILS LOUD WHEN THE GUARD GOES BLIND, not just when it is
# wrong: a sanctioned resolver that no longer exists, a fleet scan that matches
# zero files, a baseline row for a file that is already clean, and the guard
# being unwired from CI are each their own assertion. Silence is the only banned
# state (mirrors tests/integration/test_vault_backup_conf_bom.sh).
#
# Bash + stdlib Python only, no network.
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
GUARD="$ROOT/scripts/check-vault-root-reads.py"
BASELINE="$ROOT/scripts/vault-root-read-baseline.txt"
FAIL=0

ok()  { echo "PASS  $1"; }
bad() { echo "FAIL  $1"; FAIL=$((FAIL + 1)); }
run() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi; }
# trips <label> <cmd...> - the command MUST fail (that is the assertion).
trips() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then bad "$label"; else ok "$label"; fi
}

if [ ! -f "$GUARD" ]; then
  echo "FAIL  guard missing: $GUARD"
  exit 1
fi

# ---- the guard's own built-in controls ------------------------------------
run "guard built-in positive/negative controls" python3 "$GUARD" --self-test

# ---- the live tree ---------------------------------------------------------
run "real scripts/ + hooks/ fleet matches the reviewed hash ratchet" \
  python3 "$GUARD" --all

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---- positive control: the bug class must trip -----------------------------
cat > "$TMP/naive.py" <<'PY'
import os
from pathlib import Path
VAULT = Path(os.environ.get("VAULT_ROOT", str(Path.home() / "vault")))
PY
trips "negative control: naive ~/vault-defaulting read trips the guard" \
  python3 "$GUARD" --check "$TMP/naive.py"

cat > "$TMP/naive_getenv.py" <<'PY'
import os
VAULT = os.getenv("VAULT_ROOT")
PY
trips "negative control: os.getenv form trips the guard" \
  python3 "$GUARD" --check "$TMP/naive_getenv.py"

cat > "$TMP/naive_subscript.py" <<'PY'
import os
VAULT = os.environ["VAULT_ROOT"]
PY
trips "negative control: os.environ[...] form trips the guard" \
  python3 "$GUARD" --check "$TMP/naive_subscript.py"

# Indirection through a module constant is a real shape in this repo
# (hooks/scan-prior-sessions-for-secrets.py). A literal-only rule would have a
# one-line bypass, which is worse than no guard.
cat > "$TMP/naive_indirect.py" <<'PY'
import os
VAULT_ROOT_ENV = "VAULT_ROOT"
def _root():
    return os.environ.get(VAULT_ROOT_ENV, "")
PY
trips "negative control: read indirected through a module constant trips" \
  python3 "$GUARD" --check "$TMP/naive_indirect.py"

# An exemption that does not say WHY is a rubber stamp, not an exemption.
cat > "$TMP/bare_marker.py" <<'PY'
import os
# vault-root-ok:
VAULT = os.environ.get("VAULT_ROOT", "")
PY
trips "negative control: vault-root-ok with no reason is itself a violation" \
  python3 "$GUARD" --check "$TMP/bare_marker.py"

# ---- clean controls: the sanctioned paths must pass ------------------------
cat > "$TMP/resolver.py" <<'PY'
import os
from pathlib import Path
def _resolve_vault_root():
    auto = Path(__file__).resolve().parents[2]
    env = os.environ.get("VAULT_ROOT")
    return Path(env) if env and Path(env) == auto else auto
VAULT_ROOT = _resolve_vault_root()
PY
run "clean control: read inside _resolve_vault_root passes" \
  python3 "$GUARD" --check "$TMP/resolver.py"

cat > "$TMP/perfile.py" <<'PY'
import os
from pathlib import Path
def vault_root_for(path):
    found = detect_from(path)
    if found is not None:
        return found
    env = (os.environ.get("VAULT_ROOT") or "").strip()
    return Path(env) if env else None
PY
run "clean control: per-file hook resolver (detection first) passes" \
  python3 "$GUARD" --check "$TMP/perfile.py"

cat > "$TMP/fallback.py" <<'PY'
import os
from _lib.vault_root import resolve_vault_root
root = resolve_vault_root(cwd, os.environ.get("VAULT_ROOT"))
PY
run "clean control: env var passed INTO resolve_vault_root passes" \
  python3 "$GUARD" --check "$TMP/fallback.py"

cat > "$TMP/exempt.py" <<'PY'
import os
# vault-root-ok: opt-in switch, not vault detection; unset disables the feature
VAULT = os.environ.get("VAULT_ROOT", "")
PY
run "clean control: vault-root-ok WITH a reason passes" \
  python3 "$GUARD" --check "$TMP/exempt.py"

# The pattern appearing as DATA must never trip: a docstring describing the bug,
# or a test fixture stored as a source string. This is why the guard parses
# instead of grepping - and it is what lets the guard scan its own tree without
# an exclusion list that could silently grow.
cat > "$TMP/as_data.py" <<'PY'
"""The old code read os.environ.get("VAULT_ROOT", str(Path.home() / "vault"))."""
FIXTURE = '''
import os
VAULT = os.environ.get("VAULT_ROOT", "~/vault")
'''
PY
run "clean control: the pattern quoted as data (docstring/fixture) never trips" \
  python3 "$GUARD" --check "$TMP/as_data.py"

# ---- the ratchet: new fails, exact reviewed bytes pass, edited fails again --
mkdir -p "$TMP/repo/hooks"
git -C "$TMP/repo" init -q
cp "$TMP/naive.py" "$TMP/repo/hooks/legacy.py"
: > "$TMP/repo/baseline.txt"
# A NAMED error, not a traceback. Caught live while writing this suite: an
# unpinned new file raised KeyError out of the stale-row computation, which
# still exits non-zero -- so a bare "did it fail?" assertion went green on a
# crash. "Fails CI with a named error" is the actual requirement, so assert the
# message, and assert the crash signature is absent.
new_out="$(python3 "$GUARD" --all --root "$TMP/repo" --baseline "$TMP/repo/baseline.txt" 2>&1)"
new_rc=$?
if [ "$new_rc" -ne 0 ] \
   && printf '%s' "$new_out" | grep -q 'naive VAULT_ROOT read' \
   && ! printf '%s' "$new_out" | grep -q 'Traceback'; then
  ok "fleet ratchet: an unpinned new naive read trips with a NAMED error"
else
  bad "fleet ratchet: unpinned new read must fail with a named error, not a crash (rc=$new_rc)"
fi

python3 - "$TMP/repo/hooks/legacy.py" "$TMP/repo/baseline.txt" <<'PY'
import hashlib, sys
from pathlib import Path
src = Path(sys.argv[1])
Path(sys.argv[2]).write_text(
    f"{hashlib.sha256(src.read_bytes()).hexdigest()} SEV-A-home-vault hooks/legacy.py\n"
)
PY
run "fleet ratchet: exact reviewed legacy bytes pass" \
  python3 "$GUARD" --all --root "$TMP/repo" --baseline "$TMP/repo/baseline.txt"

printf '\n# edited after review\n' >> "$TMP/repo/hooks/legacy.py"
trips "fleet ratchet: EDITING a pinned legacy file trips it again" \
  python3 "$GUARD" --all --root "$TMP/repo" --baseline "$TMP/repo/baseline.txt"

# A row whose file became clean must also fail: the backlog has to shrink by
# DELETING the row, not by leaving a dead one behind that hides a re-added read.
cat > "$TMP/repo/hooks/legacy.py" <<'PY'
import os
def _resolve_vault_root():
    return os.environ.get("VAULT_ROOT")
PY
trips "fleet ratchet: a baseline row for a now-clean file is STALE and trips" \
  python3 "$GUARD" --all --root "$TMP/repo" --baseline "$TMP/repo/baseline.txt"

# ---- the pin must describe CONTENT, not the checkout ------------------------
# This repo has no .gitattributes, so a Windows clone with core.autocrlf=true has
# CRLF on disk while the Linux CI runner has LF. A raw-bytes hash pinned on one
# platform is 100% stale on the other -- the whole baseline reds the build with
# no real violation behind it. (Not hypothetical: the unnormalized
# scripts/cloud-safe-walker-baseline.txt reports every row stale on a Windows
# checkout today.) The pin is over newline-normalized source; this proves it.
if python3 - "$GUARD" <<'PY'
import importlib.util, sys, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location("cvr", sys.argv[1])
cvr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cvr)
body = b'import os\nfrom pathlib import Path\nV = os.environ.get("VAULT_ROOT", "x")\n'
tmp = Path(tempfile.mkdtemp())
out = {}
for name, data in (("lf", body), ("crlf", body.replace(b"\n", b"\r\n"))):
    p = tmp / f"{name}.py"
    p.write_bytes(data)
    violations, digest = cvr.scan_file(p, name)
    out[name] = (digest, len(violations))
assert out["lf"][0] == out["crlf"][0], f"hash differs across line endings: {out}"
assert out["lf"][1] == out["crlf"][1] == 1, f"violation count differs: {out}"
PY
then
  ok "baseline hash is line-ending independent (CRLF checkout == LF checkout)"
else
  bad "baseline hash changes with line endings - the ratchet would red on the other platform"
fi

# ---- blindness checks: the guard must not silently stop covering anything ---

# 1. The real fleet scan must actually match files. A pathspec typo or a repo
#    reshuffle would make this pass vacuously forever.
scanned="$(python3 "$GUARD" --all 2>/dev/null | sed -n 's/.*OK (\([0-9]*\) file(s) scanned.*/\1/p')"
if [ -n "$scanned" ] && [ "$scanned" -gt 50 ] 2>/dev/null; then
  ok "fleet scan is live ($scanned scripts/ + hooks/ files in scope)"
else
  bad "fleet scan matched ${scanned:-0} files - the pathspec went blind, or the gate is red"
fi

# 2. Every sanctioned resolver name must still be a real def in the tree. A name
#    kept after its function was renamed is a permanent hole nobody can see.
missing_resolver=""
for fn in _resolve_vault_root resolve_vault_root vault_root_for; do
  if ! grep -rqE "^[[:space:]]*def[[:space:]]+$fn\b" "$ROOT/scripts" "$ROOT/hooks" 2>/dev/null; then
    missing_resolver="$missing_resolver $fn"
  fi
done
if [ -z "$missing_resolver" ]; then
  ok "every sanctioned resolver name still exists as a def"
else
  bad "sanctioned name(s) with no def -- stale exemption:$missing_resolver"
fi

# 3. The baseline rows must still be REAL violations. If they were already clean
#    the ratchet would be pinning nothing and the backlog count would be a lie.
pinned_file="$(awk '$1 ~ /^[0-9a-f]{64}$/ {print $3; exit}' "$BASELINE" 2>/dev/null)"
if [ -z "$pinned_file" ]; then
  ok "baseline is empty - the backlog is cleared (nothing left to pin)"
elif [ ! -f "$ROOT/$pinned_file" ]; then
  bad "baseline names a missing file: $pinned_file"
else
  trips "baseline rows are real violations (spot-check: $pinned_file)" \
    python3 "$GUARD" --check "$ROOT/$pinned_file"
fi

# 4. Wired, not merely present. The dormant-guard class this repo keeps hitting
#    is a check that exists, passes locally, and runs in no CI job.
if grep -q "check-vault-root-reads.py" "$ROOT/scripts/ci.sh"; then
  ok "guard is wired into scripts/ci.sh"
else
  bad "guard is NOT wired into scripts/ci.sh - it would never run pre-push"
fi
if grep -q "check-vault-root-reads.py" "$ROOT/.github/workflows/lint.yml"; then
  ok "guard is wired into .github/workflows/lint.yml"
else
  bad "guard is NOT wired into .github/workflows/lint.yml - it would never run in CI"
fi

if [ "$FAIL" -ne 0 ]; then
  echo "FAILED: $FAIL"
  exit 1
fi
echo "ALL TESTS PASSED"
