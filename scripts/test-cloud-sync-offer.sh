#!/usr/bin/env bash
# Negative-controlled test for the UNCONDITIONAL cloud-sync relocate OFFER
# (worktree-footprint-signal.py cloud branch, MYC-2360).
#
# The offer must fire for ANY AI-brain vault inside a consumer cloud-sync folder,
# discovered INDEPENDENT of guided onboarding:
#   * via the Obsidian registry (obsidian.json) when cwd is NOT in the vault
#     (the "user already had an iCloud Obsidian vault" gap), AND
#   * via the current git repo (find_main_repo) when cwd IS the vault.
# It must present BOTH remediation shapes (relocate-vault.sh + relocate-machinery-
# sidecar.sh), name the vault + the service, and then go SILENT once relocated
# (Shape A symlink->local resolves clean; Shape B is suppressed by a machinery-
# sidecar manifest), for a plain local vault, for a pristine non-git Obsidian
# vault (markdown alone is not the freeze class), and under bypass.
#
# A guard earns trust only by failing on the thing it catches: every positive
# is paired with a negative control. Stdlib python3 + bash + git only.
# Run: bash scripts/test-cloud-sync-offer.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
HOOK="$ROOT/hooks/worktree-footprint-signal.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0

ICLOUD="Library/Mobile Documents/com~apple~CloudDocs"   # detect_cloud_sync marker
NEUTRAL="$TMP/neutral"; mkdir -p "$NEUTRAL"              # a non-vault, non-git cwd
NONE="$TMP/no-such-obsidian.json"                       # empty registry sentinel

# Extract additionalContext from the hook's JSON stdout; "" when suppressed.
emit_ctx() {
  python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
except (ValueError, OSError):   # hook stdout was not valid JSON -> treat as silent
    print(""); sys.exit(0)
print(d.get("additionalContext","") if isinstance(d,dict) else "")'
}

# Build an obsidian.json registry fixture listing the given vault path(s).
write_obsidian_conf() {  # $1=config-path  $2..=vault paths
  local cfg="$1"; shift
  mkdir -p "$(dirname "$cfg")"
  python3 - "$cfg" "$@" <<'PY'
import json, sys
cfg, paths = sys.argv[1], sys.argv[2:]
vaults = {f"id{i}": {"path": p, "ts": 1700000000000, "open": True}
          for i, p in enumerate(paths)}
json.dump({"vaults": vaults}, open(cfg, "w"))
PY
}

# Invoke the hook with a clean, deterministic footprint env (only the cloud
# branch under test can speak; worktree-count / low-disk / dev-root / bloat-dir
# noise is silenced). EVERY footprint input the hook reads must be pinned here,
# including the ones that reach OUTSIDE the fixture: the dev-root and
# aggregate-bloat signals read ambient machine state, so without pinning them
# these "expected silent" assertions would pass or fail depending on how many
# worktrees the developer happens to have — a hermeticity hole, not a fixture
# detail. ABS_DEV_ROOT/WORKTREE_FOOTPRINT_CACHE point at empty scratch dirs.
FOOTPRINT_SCRATCH="$(mktemp -d)"
mkdir -p "$FOOTPRINT_SCRATCH/empty-dev"
trap 'rm -rf "$FOOTPRINT_SCRATCH"' EXIT
run_hook() {  # $1=cwd  rest=VAR=VAL env assignments
  local cwd="$1"; shift
  ( cd "$cwd" && env WORKTREE_FREE_GB=0 WORKTREE_WARN=9999 \
      ABS_DEV_ROOT="$FOOTPRINT_SCRATCH/empty-dev" \
      WORKTREE_FOOTPRINT_CACHE="$FOOTPRINT_SCRATCH/bloat-cache.json" \
      WORKTREE_FOOTPRINT_FILE_WARN=999999999 "$@" \
      python3 "$HOOK" </dev/null ) | emit_ctx
}

want_contains() {  # label  needle  haystack
  case "$3" in *"$2"*) echo "PASS  $1";;
    *) echo "FAIL  $1  (missing: $2)"; fails=$((fails+1));; esac
}
want_silent() {  # label  haystack
  if [ -z "$2" ]; then echo "PASS  $1 (silent)"
  else echo "FAIL  $1  expected silent, got: ${2:0:90}"; fails=$((fails+1)); fi
}
want_absent() {  # label  needle  haystack  (negative: needle must NOT appear)
  case "$3" in *"$2"*) echo "FAIL  $1  (should be absent: $2)"; fails=$((fails+1));;
    *) echo "PASS  $1";; esac
}

# ── 1. THE CORE GAP: registry discovery, cwd NOT in the vault ────────────────
V1="$TMP/$ICLOUD/MyBrain"; mkdir -p "$V1"; : > "$V1/CLAUDE.md"
CONF1="$TMP/ob1.json"; write_obsidian_conf "$CONF1" "$V1"
ctx="$(run_hook "$NEUTRAL" OBSIDIAN_CONFIG="$CONF1")"
want_contains "registry: offers move-local shape"   "relocate-vault.sh"            "$ctx"
want_contains "registry: offers sidecar shape"       "relocate-machinery-sidecar.sh" "$ctx"
want_contains "registry: names the vault path"       "$V1"                          "$ctx"
want_contains "registry: names the cloud service"    "iCloud"                       "$ctx"
# Simplification (offer-one-action follow-up): recommend ONE safe default
# (keep notes, move machinery), never a two-option menu the user must answer.
want_contains "registry: leads with ONE default fix"          "DEFAULT FIX"          "$ctx"
want_contains "registry: tells Claude not to dump a menu"     "Do NOT make them choose" "$ctx"
want_absent   "registry: no old two-option menu phrasing"     "Tell me which"        "$ctx"

# ── 2. cwd IS the git vault; empty registry (proves find_main_repo path) ─────
V2="$TMP/$ICLOUD/GitBrain"; mkdir -p "$V2"; : > "$V2/CLAUDE.md"; git -C "$V2" init -q
ctx="$(run_hook "$V2" OBSIDIAN_CONFIG="$NONE")"
want_contains "cwd-in-vault: offer fires via find_main_repo" "relocate-vault.sh" "$ctx"

# ── 3. Shape A relocated (symlink -> local): registry lists the OLD path ─────
#       which resolves to a local disk -> SILENT (existing user not nagged).
V3OLD="$TMP/$ICLOUD/MovedBrain"; mkdir -p "$V3OLD"; : > "$V3OLD/CLAUDE.md"
V3NEW="$TMP/localdisk/MovedBrain"; mkdir -p "$TMP/localdisk"
mv "$V3OLD" "$V3NEW"; ln -s "$V3NEW" "$V3OLD"   # what relocate-vault.sh leaves
CONF3="$TMP/ob3.json"; write_obsidian_conf "$CONF3" "$V3OLD"
ctx="$(run_hook "$NEUTRAL" OBSIDIAN_CONFIG="$CONF3")"
want_silent "shape A: symlink->local resolves clean" "$ctx"

# ── 4. Shape B (machinery-sidecar): vault STILL in iCloud but a sidecar ──────
#       manifest names it -> SILENT. Paired negative control: drop the manifest
#       (point BRAIN_SIDECAR elsewhere) and the SAME vault must offer again.
V4="$TMP/$ICLOUD/SidecarBrain"; mkdir -p "$V4"; : > "$V4/CLAUDE.md"
CONF4="$TMP/ob4.json"; write_obsidian_conf "$CONF4" "$V4"
SIDE="$TMP/sidecar"; mkdir -p "$SIDE/manifests"
python3 -c 'import json,sys; json.dump({"schema":"machinery-sidecar/1","vault":sys.argv[1]}, open(sys.argv[2],"w"))' \
  "$V4" "$SIDE/manifests/sidecarbrain.json"
ctx="$(run_hook "$NEUTRAL" OBSIDIAN_CONFIG="$CONF4" BRAIN_SIDECAR="$SIDE")"
want_silent "shape B: sidecar manifest suppresses" "$ctx"
ctx="$(run_hook "$NEUTRAL" OBSIDIAN_CONFIG="$CONF4" BRAIN_SIDECAR="$TMP/empty-sidecar")"
want_contains "shape B neg-control: no manifest -> offer fires" "relocate-machinery-sidecar.sh" "$ctx"

# ── 5. NEGATIVE CONTROL: plain local vault -> SILENT ─────────────────────────
V5="$TMP/plainlocal/Brain"; mkdir -p "$V5"; : > "$V5/CLAUDE.md"; git -C "$V5" init -q
ctx="$(run_hook "$V5" OBSIDIAN_CONFIG="$NONE")"
want_silent "local vault: not the freeze class" "$ctx"

# ── 6. NEGATIVE CONTROL: pristine non-git iCloud Obsidian vault -> SILENT ────
#       (markdown alone is low-churn; don't nag every Obsidian user).
V6="$TMP/$ICLOUD/JustNotes"; mkdir -p "$V6"; echo "# note" > "$V6/note.md"
CONF6="$TMP/ob6.json"; write_obsidian_conf "$CONF6" "$V6"
ctx="$(run_hook "$NEUTRAL" OBSIDIAN_CONFIG="$CONF6")"
want_silent "pristine non-git iCloud vault: no false nag" "$ctx"

# ── 7. bypass -> SILENT (same vault as case 1, which otherwise fires) ────────
ctx="$(run_hook "$NEUTRAL" OBSIDIAN_CONFIG="$CONF1" WORKTREE_FOOTPRINT_BYPASS=1)"
want_silent "WORKTREE_FOOTPRINT_BYPASS suppresses" "$ctx"

# ── 8. fail-open: a malformed obsidian.json must not crash SessionStart ──────
BAD="$TMP/bad-obsidian.json"; printf 'not json{{' > "$BAD"
ctx="$(run_hook "$NEUTRAL" OBSIDIAN_CONFIG="$BAD")"
want_silent "malformed obsidian.json: fail-open, no crash" "$ctx"

# ── 9. OS-AWARE COMMAND SURFACE (MYC-2383) ──────────────────────────────────
#       A Windows/OneDrive user must get .ps1 commands they can actually run,
#       NOT the bash scripts (which do not run natively on Windows). The offer
#       branches on the running OS; WORKTREE_FOOTPRINT_OS forces each branch so
#       both are coverable on this Linux CI runner. Paired negative control: the
#       Windows offer must never leak a bash .sh command.
ctx_posix="$(run_hook "$NEUTRAL" OBSIDIAN_CONFIG="$CONF1" WORKTREE_FOOTPRINT_OS=posix)"
want_contains "posix branch: bash invocation"        "bash "                          "$ctx_posix"
want_contains "posix branch: .sh relocate script"    "relocate-vault.sh"              "$ctx_posix"

ctx_win="$(run_hook "$NEUTRAL" OBSIDIAN_CONFIG="$CONF1" WORKTREE_FOOTPRINT_OS=nt)"
want_contains "windows branch: .ps1 relocate script" "relocate-vault.ps1"             "$ctx_win"
want_contains "windows branch: .ps1 sidecar script"  "relocate-machinery-sidecar.ps1" "$ctx_win"
want_contains "windows branch: powershell invocation" "powershell"                    "$ctx_win"
case "$ctx_win" in
  *"relocate-vault.sh"*) echo "FAIL  windows branch: leaked a bash .sh command"; fails=$((fails+1));;
  *) echo "PASS  windows branch: no bash .sh command leaked (neg control)";;
esac

echo
if [ "$fails" -gt 0 ]; then echo "FAILED: $fails"; exit 1; fi
echo "ALL TESTS PASSED"
