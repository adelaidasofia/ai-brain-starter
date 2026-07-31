#!/usr/bin/env bash
# Cloud-safe read primitive + precise recursive-content-walker enforcement gate.
# Every safety claim has a negative control: FIFO, forced timeout, lingering
# worker saturation, unsafe AST fixture, and fail-closed recovery uncertainty.
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
GUARD="$ROOT/scripts/check-cloud-safe-file-walkers.py"
FAIL=0

ok() { echo "PASS  $1"; }
bad() { echo "FAIL  $1"; FAIL=$((FAIL + 1)); }
run() {
  local label="$1"; shift
  if "$@"; then ok "$label"; else bad "$label"; fi
}

run "shared safe_read FIFO/timeout/cap controls" \
  python3 "$ROOT/hooks/test_safe_read.py"
run "worktree recovery bounded reads fail closed on FIFO/timeout" \
  python3 "$ROOT/hooks/test_worktree_cloud_safe_recovery.py"
run "AST guard built-in positive/negative controls" \
  python3 "$GUARD" --self-test

run "relocate sweep adopts shared safe_read" \
  python3 "$GUARD" --check "$ROOT/scripts/relocate-sweep.py"
run "worktree recovery adopts shared safe_read" \
  python3 "$GUARD" --check "$ROOT/hooks/_lib/worktree_safety.py"
run "metadata-only sync scanner stays unflagged" \
  python3 "$GUARD" --check "$ROOT/hooks/check-sync-folder-machinery.py"
run "whole Python fleet matches the reviewed hash ratchet" \
  python3 "$GUARD" --all
run "sync scanner negative and clean controls remain live" \
  python3 "$ROOT/hooks/check-sync-folder-machinery.py" --self-test

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/unsafe.py" <<'PY'
import os
def scan(root):
    for base, dirs, files in os.walk(root):
        for name in files:
            open(os.path.join(base, name)).read()
PY
if python3 "$GUARD" --check "$TMP/unsafe.py" >/dev/null 2>&1; then
  bad "negative control: unsafe recursive reader trips"
else
  ok "negative control: unsafe recursive reader trips"
fi

# The repo-wide gate must catch a brand-new unsafe file, allow only the exact
# reviewed legacy content, then bite again when that legacy file changes.
git -C "$TMP" init -q
: > "$TMP/baseline.txt"
if python3 "$GUARD" --all --root "$TMP" --baseline "$TMP/baseline.txt" >/dev/null 2>&1; then
  bad "fleet ratchet: new unsafe walker trips"
else
  ok "fleet ratchet: new unsafe walker trips"
fi
python3 - "$TMP/unsafe.py" "$TMP/baseline.txt" <<'PY'
import hashlib, sys
from pathlib import Path
source = Path(sys.argv[1])
# Mirror the guard's own pin: newline-normalized content, not raw bytes. The
# heredoc above happens to write LF, so this row would match either way today --
# normalizing keeps the fixture honest instead of accidentally correct.
content = source.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
Path(sys.argv[2]).write_text(f"{hashlib.sha256(content).hexdigest()} unsafe.py\n")
PY
run "fleet ratchet: exact reviewed legacy content passes" \
  python3 "$GUARD" --all --root "$TMP" --baseline "$TMP/baseline.txt"
printf '\n# changed after review\n' >> "$TMP/unsafe.py"
if python3 "$GUARD" --all --root "$TMP" --baseline "$TMP/baseline.txt" >/dev/null 2>&1; then
  bad "fleet ratchet: edited legacy walker trips"
else
  ok "fleet ratchet: edited legacy walker trips"
fi

# ---- the pin must describe CONTENT, not the checkout ------------------------
# This repo has no .gitattributes, so a Windows clone with core.autocrlf=true has
# CRLF on disk while the Linux CI runner has LF. A raw-decode hash pinned on one
# platform is 100% stale on the other -- the whole baseline reds the local gate
# with no real violation behind it, which silently trains Windows maintainers to
# ignore that gate. (Not hypothetical: before the fix, all 41 reviewed rows in
# scripts/cloud-safe-walker-baseline.txt reported STALE on a Windows checkout
# while CI on ubuntu stayed green.) The pin is over newline-
# normalized source; same body as LF and as CRLF must hash identically.
if python3 - "$GUARD" <<'PY'
import importlib.util, shutil, sys, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location("walkers", sys.argv[1])
walkers = importlib.util.module_from_spec(spec)
# Register before exec: the guard defines @dataclass at module scope, and
# dataclasses resolves the owning module out of sys.modules while decorating.
sys.modules[spec.name] = walkers
spec.loader.exec_module(walkers)
body = (
    b"import os\n"
    b"def scan(root):\n"
    b"    for base, dirs, files in os.walk(root):\n"
    b"        open(files[0]).read()\n"
)
tmp = Path(tempfile.mkdtemp())
try:
    out = {}
    for name, data in (("lf", body), ("crlf", body.replace(b"\n", b"\r\n"))):
        path = tmp / f"{name}.py"
        path.write_bytes(data)
        rc, verdict = walkers.check_file(path)
        out[name] = (verdict["sha256"], rc, len(verdict["findings"]))
    assert out["lf"][0] == out["crlf"][0], f"hash differs across line endings: {out}"
    # A matching hash on two clean files would pass vacuously; pin a real finding.
    assert out["lf"][1] == out["crlf"][1] == 1, f"verdict differs: {out}"
    assert out["lf"][2] == out["crlf"][2] == 1, f"finding count differs: {out}"
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
then
  ok "baseline hash is line-ending independent (CRLF checkout == LF checkout)"
else
  bad "baseline hash changes with line endings - the ratchet would red on the other platform"
fi

cat > "$TMP/metadata.py" <<'PY'
import os
def count(root):
    return sum(len(files) for base, dirs, files in os.walk(root))
PY
run "clean control: metadata-only walker passes" \
  python3 "$GUARD" --check "$TMP/metadata.py"

cat > "$TMP/safe.py" <<'PY'
import os
from _lib.safe_read import safe_read_text
def scan(root):
    for base, dirs, files in os.walk(root):
        for name in files:
            safe_read_text(os.path.join(base, name))
PY
run "clean control: shared primitive passes" \
  python3 "$GUARD" --check "$TMP/safe.py"

RULE="$ROOT/templates/hookify-rules/hookify.warn-filesystem-walk-without-bounded-read.local.md"
if [ -f "$RULE" ] \
   && grep -q '^name: warn-filesystem-walk-without-bounded-read$' "$RULE" \
   && grep -q '^event: file$' "$RULE" \
   && grep -q 'safe_read' "$RULE"; then
  ok "Hookify file-event template is named and points at shared primitive"
else
  bad "Hookify file-event template contract"
fi
if python3 - "$RULE" <<'PY'
import re, sys
from pathlib import Path

line = next(
    line for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if line.lstrip().startswith("pattern:") and "(?s)" in line
)
raw = line.split("pattern:", 1)[1].strip()
pattern = raw[1:-1].replace("''", "'")
rule = re.compile(pattern)
cases = {
    "unsafe": ("for x in Path(root).walk():\n    x[0].read_text()", True),
    "copytree": ("shutil.copytree(source, target)", True),
    "safe": ("for x in os.walk(root):\n    safe_read_text(x)", False),
    "mixed": ("for x in os.walk(root):\n    safe_read_text(x); open(x).read()", True),
    "metadata": ("sum(len(files) for _, _, files in os.walk(root))", False),
    "write": ("for x in os.walk(root):\n    open('marker', 'w').write('x')", False),
}
assert all(bool(rule.search(source)) is expected for source, expected in cases.values())
PY
then
  ok "Hookify regex fires on unsafe/copytree/mixed and stays quiet on safe/metadata/write"
else
  bad "Hookify regex positive/negative controls"
fi

if [ "$FAIL" -ne 0 ]; then
  echo "FAILED: $FAIL"
  exit 1
fi
echo "ALL TESTS PASSED"
