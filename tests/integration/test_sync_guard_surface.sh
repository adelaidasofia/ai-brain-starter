#!/usr/bin/env bash
# CI lock + negative control for the cloud-sync guard's SURFACING path
# (MYC-1133: hooks/surface-sync-guard-findings.py + its host).
#
# WHY: the guard this serves ran daily for three months, correctly reported a
# dozen git repos churning inside a synced folder on EVERY run, and changed
# nothing -- its only sink was a log file. Detection was never the gap; routing
# was. So the thing that must be pinned is not "does the scan find machinery"
# (the scan has its own --self-test) but "do the findings REACH a human".
#
# The delivery path is deliberately indirect: surface-sync-guard-findings.py is
# NOT its own SessionStart hook (that put SessionStart at 20/19 on the footprint
# SLA gate). worktree-footprint-signal.py calls its build_report() instead. That
# indirection is exactly what a test must cover -- a broken import there would
# leave BOTH files present, both self-tests green, and the finding silently
# undelivered. A guard that is working and a guard that is dead both emit
# silence, which is why silence alone is never evidence here.
#
# Asserts, all through the REAL host hook (never build_report in isolation):
#   1. FIRES: a planted high finding reaches SessionStart additionalContext,
#      naming the offending path.
#   2. QUIET: a clean, fresh snapshot produces no sync-guard noise (no cry-wolf).
#   3. STALE: a snapshot older than the staleness window warns that the daily
#      job died -- a dead scheduler and a clean machine otherwise look identical.
#   4. PARTIAL: a truncated root reports unproven coverage, never "clean".
#   5. FAIL-OPEN: a missing or corrupt snapshot never breaks SessionStart.
#   6. NO RE-WALK: the surfacer reads the snapshot only. A per-session walk of
#      every cloud root becomes N concurrent full walks under N sessions, which
#      is the resource storm this guard exists to prevent.
#
# Stdlib python3 + bash only. No network, no git. Tmpdir removed on exit.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"

HOST="$REPO_ROOT/hooks/worktree-footprint-signal.py"
SURFACER="$REPO_ROOT/hooks/surface-sync-guard-findings.py"

PASS=0; FAIL=0
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
ok()  { PASS=$((PASS + 1)); echo "  PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL  $1 :: ${2:-}"; }

SNAP_DIR="$HOME/.claude/state"
SNAP="$SNAP_DIR/sync-guard-last.json"
mkdir -p "$SNAP_DIR"

# write_snap AGE_SECONDS_AGO FINDINGS_JSON TRUNCATED_JSON
write_snap() {
  python3 - "$SNAP" "$1" "$2" "$3" <<'PY'
import json, sys, time
path, age, findings, truncated = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
f = json.loads(findings)
json.dump({
    "scanned_at": time.time() - age,
    "roots": [],
    "findings": f,
    "high_count": len([x for x in f if x.get("severity") == "high"]),
    "truncated_roots": json.loads(truncated),
}, open(path, "w"))
PY
}

run_host() { OUT="$(python3 "$HOST" <<<'{}' 2>/dev/null)"; }
ctx()      { printf '%s' "$OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("additionalContext",""))' 2>/dev/null; }
mentions() { ctx | grep -q "$1"; }

HIGH='[{"severity":"high","provider":"iCloud","path":"/planted/repo/.git","reason":"machinery dir '"'"'.git'"'"' inside synced folder"}]'

echo "=== 1. FIRES: a planted finding reaches SessionStart ==="
write_snap 60 "$HIGH" '[]'
run_host
if mentions "/planted/repo/.git"; then ok "planted finding reaches additionalContext"
else bad "planted finding reaches additionalContext" "not in output"; fi

echo "=== 2. QUIET: clean + fresh -> no sync-guard noise ==="
write_snap 60 '[]' '[]'
run_host
if mentions "sync-guard"; then bad "clean snapshot stays quiet" "cried wolf"
else ok "clean snapshot stays quiet"; fi

echo "=== 3. STALE: a dead daily job is not silence ==="
write_snap 360000 '[]' '[]'      # 100h
run_host
if mentions "may have stopped"; then ok "stale snapshot warns the job died"
else bad "stale snapshot warns the job died" "no staleness warning"; fi

echo "=== 4. PARTIAL: a truncated root is never reported clean ==="
write_snap 60 '[]' '["/x/huge-root"]'
run_host
if mentions "/x/huge-root"; then ok "truncated root reported as unproven coverage"
else bad "truncated root reported as unproven coverage" "silently called clean"; fi

echo "=== 5. FAIL-OPEN: missing / corrupt snapshot never breaks SessionStart ==="
rm -f "$SNAP"
run_host; rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$OUT" | grep -q '"continue"'; then ok "missing snapshot -> valid hook output, exit 0"
else bad "missing snapshot -> valid hook output, exit 0" "rc=$rc"; fi
printf 'not json{{{' > "$SNAP"
run_host; rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$OUT" | grep -q '"continue"'; then ok "corrupt snapshot -> valid hook output, exit 0"
else bad "corrupt snapshot -> valid hook output, exit 0" "rc=$rc"; fi

echo "=== 6. NO RE-WALK: the surfacer must never walk cloud roots itself ==="
if grep -Eq '\bos\.walk\b|\brglob\b|\bglob\.glob\b' "$SURFACER"; then
  bad "surfacer reads the snapshot only" "it walks the filesystem"
else ok "surfacer reads the snapshot only"; fi

echo "=== 7. the surfacer's own branch controls still pass ==="
if python3 "$SURFACER" --self-test >/dev/null 2>&1; then ok "surfacer --self-test"
else bad "surfacer --self-test" "non-zero"; fi

echo
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
