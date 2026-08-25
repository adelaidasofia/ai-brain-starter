#!/usr/bin/env bash
# Test: surface-unniced-launchagents.py — the "background job at normal
# priority" detector.
#
# Bug class: a fleet of user LaunchAgents left at normal scheduling priority
# competes with WindowServer for CPU and disk. Measured 2026-08-19: two hard
# freezes on a 10-core host (load 43.78, then 109) with 2GB RAM free and swap
# flat — 68 agents, none declaring itself background. Because memory and disk
# stay healthy, the failure reads as a crash and misdirects diagnosis at RAM.
#
# The decision is factored into the pure function `is_priority_declared`, so
# assertions 1-6 exercise every branch deterministically with no real plists.
# Assertions 7-9 drive the real scan against a FIXTURE HOME, so the end-to-end
# path is covered without reading the developer's actual LaunchAgents.
#
# Assertions:
#   NEGATIVE controls (declared -> NOT flagged):
#     1. ProcessType=Background.
#     2. ProcessType=Adaptive.
#     3. Explicit integer Nice.
#   POSITIVE controls (undeclared -> flagged):
#     4. A plist with neither key.
#     5. LowPriorityIO alone — disk-only, still competes for CPU. The subtle one.
#     6. A non-dict / garbage payload is never treated as declared.
#   END-TO-END (fixture HOME, real scan):
#     7. 3 undeclared agents -> systemMessage naming the count.
#     8. All-declared fleet -> silent continue:true (no false alarm).
#     9. UNNICED_LAUNCHAGENTS_BYPASS=1 -> no-op, valid JSON, continue:true.
#
# Self-contained. Exit 0 = pass, exit 1 = fail.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/surface-unniced-launchagents.py"
# HOME alone does NOT sandbox on Windows: Path.home() reads USERPROFILE there,
# so a bare HOME= override runs against the developer's REAL ~/.claude (MYC-3536).
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
if [ ! -f "$HOOK" ]; then
  echo "FAIL: hook not found at $HOOK" >&2
  exit 1
fi

# --- Assertions 1-6: pure-predicate pos/neg control ----------------------
HOOK="$HOOK" python3 - <<'PY'
import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location("agents_under_test", os.environ["HOOK"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures = []

def check(label, expected, payload):
    got = mod.is_priority_declared(payload)
    if got is not expected:
        failures.append(f"{label}: expected {expected}, got {got}")

# NEGATIVE controls — a declared job must never be flagged.
check("1 ProcessType=Background", True, {"ProcessType": "Background"})
check("2 ProcessType=Adaptive", True, {"ProcessType": "Adaptive"})
check("3 explicit Nice", True, {"Nice": 10})

# POSITIVE controls — an undeclared job must be flagged.
check("4 neither key", False, {"Label": "com.example.job"})
check("5 LowPriorityIO alone", False, {"LowPriorityIO": True})
check("6 garbage payload", False, ["not", "a", "dict"])

# ProcessType=Interactive is explicitly NOT a background declaration.
check("6b ProcessType=Interactive", False, {"ProcessType": "Interactive"})

if failures:
    print("FAIL: is_priority_declared pos/neg control", file=sys.stderr)
    for f in failures:
        print("  " + f, file=sys.stderr)
    sys.exit(1)
PY
echo "PASS: is_priority_declared pos+neg control (assertions 1-6)"

# --- Assertions 7-9: end-to-end against a FIXTURE HOME -------------------
# macOS ONLY. The hook is Darwin-gated by construction -- LaunchAgents do not
# exist elsewhere, so on Linux/Windows it exits early with a bare continue and
# the end-to-end path has nothing to assert. Assertions 1-6 above are pure and
# DO run on every platform, so this file still carries real coverage in CI
# (the canonical ci gate runs on ubuntu-latest).
if [ "$(uname -s)" != "Darwin" ]; then
  echo "SKIP: assertions 7-9 are macOS-only (hook is Darwin-gated); 1-6 passed on $(uname -s)"
  exit 0
fi

FIXTURE="$(mktemp -d)"
trap 'rm -rf "$FIXTURE"' EXIT
mkdir -p "$FIXTURE/Library/LaunchAgents" "$FIXTURE/.claude"

write_plist() {  # $1=name  $2=extra-xml
  cat > "$FIXTURE/Library/LaunchAgents/$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
$2
</dict></plist>
EOF
}

# 7: three undeclared agents must surface, naming the count.
write_plist com.test.alpha ""
write_plist com.test.beta ""
write_plist com.test.gamma ""
OUT="$(run_sandboxed "$FIXTURE" env UNNICED_AGENTS_CAP_HOURS=0 python3 "$HOOK" 2>/dev/null)"
echo "$OUT" | python3 -c '
import json,sys
d=json.load(sys.stdin)
m=d.get("systemMessage","")
assert "3 of 3" in m, f"expected count 3 of 3 in message, got: {m}"
assert "ProcessType" in m, "message must name the fix"
' || { echo "FAIL assertion 7: undeclared fleet not surfaced" >&2; echo "$OUT" >&2; exit 1; }
echo "PASS: undeclared fleet surfaced with count + fix (assertion 7)"

# 8: an all-declared fleet must stay SILENT — the no-false-alarm control.
write_plist com.test.alpha "  <key>ProcessType</key><string>Background</string>"
write_plist com.test.beta  "  <key>ProcessType</key><string>Background</string>"
write_plist com.test.gamma "  <key>Nice</key><integer>10</integer>"
OUT="$(run_sandboxed "$FIXTURE" env UNNICED_AGENTS_CAP_HOURS=0 python3 "$HOOK" 2>/dev/null)"
echo "$OUT" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d.get("continue") is True, d
assert "systemMessage" not in d, f"declared fleet must not surface: {d}"
' || { echo "FAIL assertion 8: false alarm on a fully-declared fleet" >&2; echo "$OUT" >&2; exit 1; }
echo "PASS: fully-declared fleet stays silent (assertion 8)"

# 9: bypass must no-op with valid JSON.
OUT="$(run_sandboxed "$FIXTURE" env UNNICED_LAUNCHAGENTS_BYPASS=1 python3 "$HOOK" 2>/dev/null)"
echo "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("continue") is True, d' \
  || { echo "FAIL assertion 9: bypass did not emit continue JSON" >&2; exit 1; }
echo "PASS: bypass no-ops with valid JSON (assertion 9)"

echo "All assertions passed. Unniced-LaunchAgent detector holds (pos+neg control)."
