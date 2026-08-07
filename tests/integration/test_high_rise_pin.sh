#!/usr/bin/env bash
# High-Rise vendor pin: the pin must describe the vendored CONTENT, not the
# checkout that last ran the sync. Every claim here has a negative control,
# because normalizing line endings is only correct if the drift guard still
# bites on a real hand-edit.
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SYNC="$ROOT/scripts/sync-high-rise.py"
FAIL=0

ok() { echo "PASS  $1"; }
bad() { echo "FAIL  $1"; FAIL=$((FAIL + 1)); }
run() {
  local label="$1"; shift
  if "$@"; then ok "$label"; else bad "$label"; fi
}

# The real vendored tree must match its pin on THIS checkout, whatever line
# endings this platform happens to use.
run "vendored High-Rise files match PIN.json on this checkout" \
  python3 "$SYNC" --check

# ---- the pin must describe CONTENT, not the checkout ------------------------
# A Windows clone with core.autocrlf=true has CRLF on disk while PIN.json was
# recorded from LF. Hashing raw bytes reported all three vendored files as
# "edited locally" when nothing was touched -- and the advertised remedy
# (re-sync) would have written CRLF hashes into PIN.json and inverted the
# breakage onto every other platform. Same defect class as the cloud-safe
# walker ratchet (#411).
if python3 - "$SYNC" <<'PY'
import importlib.util, json, shutil, sys, tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("synchr", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

body = b"# Floors\n\nGround floor.\nSecond floor.\n"
crlf = body.replace(b"\n", b"\r\n")

# 1. Same content, two line endings, one digest.
assert m._content_sha256(body) == m._content_sha256(crlf), "pin hash differs across line endings"

# 2. Negative control: normalizing must not make the hash blind to real edits.
assert m._content_sha256(body) != m._content_sha256(body + b"local edit\n"), \
    "pin hash ignores a genuine content edit"

tmp = Path(tempfile.mkdtemp())
try:
    m.VENDOR_DIR = tmp
    m.PIN_FILE = tmp / "PIN.json"
    m.VENDORED_FILES = ("floors.md",)
    (tmp / "PIN.json").write_text(json.dumps({
        "tag": "v0.0.0",
        "commit": "0" * 40,
        "files": {"floors.md": m._content_sha256(body)},
    }), encoding="utf-8")

    # 3. End to end: a CRLF working tree against an LF-recorded pin is clean.
    (tmp / "floors.md").write_bytes(crlf)
    assert m.check() == 0, "CRLF checkout of LF-pinned content reported false drift"

    # 4. Negative control: a hand-edit still trips, on either line ending.
    (tmp / "floors.md").write_bytes(crlf + b"hand edit\r\n")
    assert m.check() != 0, "hand-edited vendored file did not trip the drift guard"
    (tmp / "floors.md").write_bytes(body + b"hand edit\n")
    assert m.check() != 0, "hand-edited LF vendored file did not trip the drift guard"
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
then
  ok "pin is line-ending independent and still bites on a real hand-edit"
else
  bad "pin hash contract broken - either it tracks the checkout, or it went blind to edits"
fi

if [ "$FAIL" -ne 0 ]; then
  echo "FAILED: $FAIL"
  exit 1
fi
echo "ALL TESTS PASSED"
