#!/bin/bash
# sync-vault-scripts.sh — propagate updated vault-side scripts from the
# ai-brain-starter repo into the user's vault  <meta>/scripts/  directory.
#
# WHY THIS EXISTS
#   A vault's "<meta>/scripts/" folder is populated ONCE, at setup, by the
#   setup phases (Phase 5 copies the aggregators + graph hook, Phase 18 the
#   journal index, etc.). It is never re-synced. So when the repo ships a new
#   or fixed vault script — session-close-runner.sh, check-rule-conflicts.py,
#   drift-detection.py, passive-capture.py, … — it never reaches EXISTING
#   vaults. scripts/sync-skills.sh only syncs skill->~/.claude/skills; this is
#   the missing skill->vault half.
#
# CONTRACT (mirrors sync-skills.sh so the two behave identically)
#   - Idempotent: identical dest = no-op (no noise).
#   - Non-destructive: a dest that DIFFERS from the incoming repo file is backed
#     up to <file>.bak-YYYY-MM-DD-HHMM BEFORE being overwritten — local edits
#     are always recoverable.
#   - Maintainer-safe: a symlinked dest (live-editing the skill repo from the
#     vault) is skipped, never clobbered.
#   - Source-absent is non-fatal: a manifest entry not yet on this checkout
#     (e.g. session-close-runner.sh before #173 merges) is simply skipped.
#
# VAULT RESOLUTION (so it can run with zero args from the auto-update flow):
#   1. --vault PATH
#   2. $VAULT_ROOT
#   3. parse ~/.claude/settings.json — the installed hooks embed the vault path
#      (e.g. "<vault>/⚙️ Meta/scripts/session-end-hook.sh")
#   If none resolve, this is a NON-FATAL no-op (logs the reason, exits 0): a box
#   with no vault set up yet must not error during an auto-update.
#
# THE SOURCE MUST BE TRUSTWORTHY  ($ABS_CLONE_PATCHES_STASHED)
#   This script's whole job is "the repo is newer than the vault, push the repo
#   version". That is only true when the repo checkout actually holds what the
#   user expects it to hold.
#
#   bootstrap auto-stashes local uncommitted changes before its `git pull`. For
#   the ~40 seconds that follow, the checkout is a pristine origin/main with the
#   user's patches REMOVED — and running this script in that window copies the
#   UNPATCHED file over the patched one in the vault. Reported on Windows:
#   session-close-runner.sh and vault-safe-commit.sh in "⚙️ Meta/scripts" were
#   both reverted this way. The .bak is written faithfully and holds the good
#   version, which is precisely why nobody notices: the run reports "Updated: 2"
#   like any healthy update, and the regression is only visible by diffing a
#   backup nobody has a reason to open.
#
#   So bootstrap exports ABS_CLONE_PATCHES_STASHED=<stash message> around the
#   pull, and this script refuses to propagate while it is set. Stale-but-
#   working beats silently-regressed. The guard lives HERE rather than at the
#   call site because there are three call paths (bootstrap.ps1 directly,
#   bootstrap.sh -> sync-skills.sh -> sync-skills.py, and manual runs) and a
#   fourth would not know to re-implement it.
#
# USAGE
#   bash sync-vault-scripts.sh [--vault PATH] [--dry-run] [--quiet]
#
# EXIT: 0 = clean / nothing to do / vault not resolvable / source untrustworthy
#       (all non-fatal); 2 = a real copy or backup error occurred.

# Intentionally NOT using `set -u` — macOS bash 3.2 treats empty-array expansion
# as "unbound", which would false-positive on a clean first run (same reason as
# sync-skills.sh). pipefail is safe.
set -o pipefail

# Source repo = the checkout this script lives in (scripts/..), so it works from
# the installed skill, a dev checkout, or CI alike. $STARTER_DIR overrides.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STARTER_DIR="${STARTER_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
STAMP="$(date +%Y-%m-%d-%H%M)"
DRY_RUN=0
QUIET=0
VAULT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --vault) VAULT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "sync-vault-scripts.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

# --- Manifest: the scripts a RUNNING vault invokes from <meta>/scripts/. ------
# EXPLICIT allow-list, never a glob over scripts/ (which is mostly repo tooling
# — ci.sh, install-*.py, test-*.sh — that must NEVER land in a vault). Every
# entry must be import-closed: stdlib-only, OR its local-module dependencies are
# listed here too, else it crashes at runtime in the vault. The self-test
# tests/integration/test_vault_script_sync.sh enforces import-closure.
VAULT_SCRIPTS=(
  "_meta_resolver.py"          # shared meta-folder resolver (deterministic keystone)
  "_project_key.py"            # shared project-key resolver (dep of check-rule-conflicts.py)
  "_floors.py"                 # shared floor vocabulary (dep of build-journal-index.py)
  "aggregate-sessions.py"      # session-close: Last Session.md index
  "aggregate-decisions.py"     # session-close: Decision Log index
  "session-close-runner.sh"    # session-close: deterministic aggregation runner (#173)
  "vault-safe-commit.sh"       # session-close: targeted-path commit helper
  "session-end-hook.sh"        # Stop-hook body
  "write-hook.sh"              # PostToolUse(Write)-hook body
  "graph-context-hook.sh"      # UserPromptSubmit graph-routing hook body
  "build-journal-index.py"     # insights: journal index builder
  "journal-preflight.py"       # daily-journal Step 0: pulls every context source (stdlib-only; message/rescuetime fetchers optional, degrades honestly)
  "check-rule-conflicts.py"    # rule maintenance
  "drift-detection.py"         # rule / CLAUDE.md drift detection
  "passive-capture.py"         # instinct-engine passive capture (opt-in)
)

CREATED=(); UPDATED=(); BACKED_UP=(); SKIPPED=(); ABSENT=(); ERRORS=()

note() { [ "$QUIET" -eq 1 ] || echo "$1"; }

# --- REFUSE to propagate from a knowingly-degraded checkout --------------------
# See "THE SOURCE MUST BE TRUSTWORTHY" in the header. Deliberately printed even
# under --quiet: the whole failure mode is that this step looks like a normal
# update, and bootstrap calls it with --quiet.
if [ -n "${ABS_CLONE_PATCHES_STASHED:-}" ]; then
  echo "sync-vault-scripts: SKIPPED — not propagating to the vault."
  echo "  This bootstrap run stashed your local changes to $STARTER_DIR before"
  echo "  pulling, so the scripts here are currently the UNPATCHED upstream copies."
  echo "  Copying them into the vault would overwrite your patched vault scripts"
  echo "  with the versions you patched them to fix. Your vault is untouched."
  echo "  Your changes are in: git stash -> ${ABS_CLONE_PATCHES_STASHED}"
  echo "  To restore them and then sync the vault:"
  echo "    cd \"$STARTER_DIR\" && git stash pop && bash scripts/sync-vault-scripts.sh"
  exit 0
fi

# --- pick a REAL python interpreter -------------------------------------------
# Mirrors the probe sync-vault-scripts.ps1 uses (#313). Under git-bash / MSYS on
# Windows a bare `python3` on PATH is usually the Microsoft Store
# app-execution-alias shim: it satisfies `command -v`, but prints nothing and
# pops the Store, so the resolver call came back EMPTY and this script misread
# it as "no Meta folder" and silently no-opped (issue #375). Trust only a
# candidate that actually reports major version 3.
PY_CMD=""
_probe_python() {
  [ "$("$@" -c 'import sys; print(sys.version_info[0])' 2>/dev/null \
        | head -n1 | tr -d '\r')" = "3" ]
}
for _cand in python3 python py; do
  command -v "$_cand" >/dev/null 2>&1 || continue
  if [ "$_cand" = "py" ]; then
    # The Windows launcher needs -3 to guarantee a Python 3 interpreter.
    _probe_python py -3 && { PY_CMD="py -3"; break; }
  else
    _probe_python "$_cand" && { PY_CMD="$_cand"; break; }
  fi
done
unset _cand

# --- Resolve the vault root ---------------------------------------------------
resolve_vault_from_settings() {
  local settings="$HOME/.claude/settings.json"
  [ -f "$settings" ] || return 1
  [ -n "$PY_CMD" ] || return 1
  # shellcheck disable=SC2086  # PY_CMD may be "py -3"; word-splitting intended
  $PY_CMD - "$settings" <<'PY' 2>/dev/null
import json, re, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
for _ev, groups in (data.get("hooks") or {}).items():
    for g in groups:
        for h in g.get("hooks", []):
            cmd = h.get("command", "")
            # The installed hook commands embed the absolute vault path right
            # before the meta-folder + /scripts/. Grab the longest such prefix.
            m = re.search(r"(/[^'\"]+?)/(?:⚙️ Meta|Meta)/scripts/", cmd)
            if m:
                print(m.group(1))
                sys.exit(0)
sys.exit(1)
PY
}

if [ -z "$VAULT" ]; then VAULT="${VAULT_ROOT:-}"; fi
if [ -z "$VAULT" ]; then VAULT="$(resolve_vault_from_settings || true)"; fi

if [ -z "$VAULT" ] || [ ! -d "$VAULT" ]; then
  note "sync-vault-scripts: no vault resolved (--vault / \$VAULT_ROOT / settings.json all empty) — skipping (non-fatal)."
  exit 0
fi

# --- Resolve the vault's meta dir via the SHARED resolver (decorated-first). ---
# The vault scripts live in the HUMAN meta folder ("⚙️ Meta"), which a naive
# "*Meta" glob would miss — plain machine "Meta" sorts first and would shadow it.
# _meta_resolver.py is the ONE source of truth for this (scripts/check-meta-
# resolution.sh bans re-implementing the glob in shell); it prefers whichever
# Meta variant holds a known human subfolder. Disambiguate on folders the human
# meta owns. It lives beside this script in the repo, so $SCRIPT_DIR resolves it.
# shellcheck disable=SC2086  # PY_CMD may be "py -3"; word-splitting intended
META="$([ -n "$PY_CMD" ] && $PY_CMD "$SCRIPT_DIR/_meta_resolver.py" "$VAULT" scripts Decisions Sessions 2>/dev/null || true)"
if [ -z "$META" ]; then
  note "sync-vault-scripts: no Meta folder in $VAULT — skipping (non-fatal)."
  exit 0
fi

DEST_DIR="$META/scripts"

# Maintainer-safe: if the vault scripts dir is a symlink (live-editing the repo
# from the vault), do not touch it.
if [ -L "$DEST_DIR" ]; then
  note "sync-vault-scripts: $DEST_DIR is a symlink (managed elsewhere) — skipping."
  exit 0
fi

if [ "$DRY_RUN" -eq 0 ]; then mkdir -p "$DEST_DIR" 2>/dev/null || {
  echo "sync-vault-scripts: cannot create $DEST_DIR" >&2; exit 2; }
fi

# --- Sync one script: backup-on-diff, then copy (mirrors sync-skills.sh). ------
sync_one() {
  local name="$1"
  local src="$STARTER_DIR/scripts/$name"
  local dest="$DEST_DIR/$name"

  if [ ! -f "$src" ]; then
    ABSENT+=("$name (not on this checkout yet)")
    return 0
  fi
  if [ -L "$dest" ]; then
    SKIPPED+=("$name (symlinked dest, maintainer workflow)")
    return 0
  fi
  if [ -f "$dest" ]; then
    if cmp -s "$src" "$dest"; then
      return 0   # identical — no-op, no noise
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
      UPDATED+=("$name (would update; backup -> ${name}.bak-${STAMP})")
      return 0
    fi
    local bak="${dest}.bak-${STAMP}"
    if cp "$dest" "$bak" 2>/dev/null; then
      BACKED_UP+=("$bak")
    else
      ERRORS+=("could not back up $dest before overwrite")
      return 1
    fi
    if cp "$src" "$dest" 2>/dev/null; then
      chmod +x "$dest" 2>/dev/null || true
      UPDATED+=("$name")
    else
      ERRORS+=("could not overwrite $dest (backup at $bak)")
      return 1
    fi
  else
    if [ "$DRY_RUN" -eq 1 ]; then
      CREATED+=("$name (would create)")
      return 0
    fi
    if cp "$src" "$dest" 2>/dev/null; then
      chmod +x "$dest" 2>/dev/null || true
      CREATED+=("$name")
    else
      ERRORS+=("could not create $dest")
      return 1
    fi
  fi
}

for s in "${VAULT_SCRIPTS[@]}"; do
  sync_one "$s"
done

# --- Summary (to stdout + a discoverable vault-side log) ----------------------
LOG_FILE="$DEST_DIR/.vault-script-sync.log"
summary() {
  echo "=== sync-vault-scripts.sh @ $STAMP${DRY_RUN:+ (dry-run)} ==="
  echo "vault: $VAULT"
  echo "meta:  $META"
  echo "Created:   ${#CREATED[@]}";   for f in "${CREATED[@]:-}"; do [ -n "$f" ] && echo "  + $f"; done
  echo "Updated:   ${#UPDATED[@]}";   for f in "${UPDATED[@]:-}"; do [ -n "$f" ] && echo "  ~ $f"; done
  echo "Backed up: ${#BACKED_UP[@]} (local edits preserved)"; for f in "${BACKED_UP[@]:-}"; do [ -n "$f" ] && echo "  b $f"; done
  echo "Skipped:   ${#SKIPPED[@]}";   for f in "${SKIPPED[@]:-}"; do [ -n "$f" ] && echo "  s $f"; done
  echo "Absent:    ${#ABSENT[@]}";    for f in "${ABSENT[@]:-}"; do [ -n "$f" ] && echo "  . $f"; done
  echo "Errors:    ${#ERRORS[@]}";    for f in "${ERRORS[@]:-}"; do [ -n "$f" ] && echo "  ! $f"; done
  echo ""
}

changed=$(( ${#CREATED[@]} + ${#UPDATED[@]} + ${#BACKED_UP[@]} + ${#ERRORS[@]} ))
if [ "$DRY_RUN" -eq 1 ]; then
  summary
elif [ "$QUIET" -eq 1 ]; then
  summary >> "$LOG_FILE" 2>/dev/null || true
  [ "$changed" -gt 0 ] && summary   # surface to stdout only when something changed
else
  summary | tee -a "$LOG_FILE"
fi

if [ "${#ERRORS[@]}" -gt 0 ]; then exit 2; fi
exit 0
