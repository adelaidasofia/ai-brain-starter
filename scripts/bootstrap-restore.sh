#!/usr/bin/env bash
# bootstrap-restore.sh — interactive restore from .bak files.
#
# Walks ~/.claude/ and the ai-brain-starter skill folders looking for
# *.bak-YYYY-MM-DD-HHMM files, lists them, and offers to restore each.
#
# Closes mycelium-hq/ai-brain-starter#2 — bootstrap: --restore mode.
#
# Usage:
#   bash scripts/bootstrap-restore.sh                # interactive
#   bash scripts/bootstrap-restore.sh --yes          # auto-confirm all
#   bash scripts/bootstrap-restore.sh --list         # list only, no action
#   bash scripts/bootstrap-restore.sh --since 7      # backups from last N days only
#   bash scripts/bootstrap-restore.sh --dry-run      # show what would happen
#   bash scripts/bootstrap-restore.sh --path PATH    # restore from a specific path
#
# Safety:
#   - Never deletes a backup. The restore moves the backup back to the original
#     name; the previous live file is renamed to *.pre-restore-YYYYMMDD-HHMMSS
#     so nothing is destroyed.
#   - Lists all candidates first, asks per-item unless --yes.
#   - Verifies the .bak file is non-empty before restoring (refuses to restore
#     a 0-byte backup over a live file).

set -uo pipefail

YES=0
LIST_ONLY=0
DRY_RUN=0
SINCE_DAYS=0
SEARCH_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y) YES=1; shift ;;
    --list) LIST_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --since) SINCE_DAYS="$2"; shift 2 ;;
    --path) SEARCH_PATH="$2"; shift 2 ;;
    --help|-h)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 2 ;;
  esac
done

# Default search paths
if [[ -z "$SEARCH_PATH" ]]; then
  SEARCH_PATHS=(
    "$HOME/.claude/settings.json"
    "$HOME/.claude/settings.local.json"
    "$HOME/.claude/.mcp.json"
    "$HOME/.claude/skills"
  )
else
  SEARCH_PATHS=("$SEARCH_PATH")
fi

log()  { printf "  \033[36m·\033[0m %s\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*"; }
hdr()  { printf "\n\033[1m%s\033[0m\n" "$*"; }

# Find all *.bak-* files under search paths
find_backups() {
  local before_filter
  before_filter=()
  for p in "${SEARCH_PATHS[@]}"; do
    if [[ -f "$p" ]]; then
      # Look for bak files alongside this file
      local dir
      dir="$(dirname "$p")"
      local base
      base="$(basename "$p")"
      while IFS= read -r f; do
        before_filter+=("$f")
      done < <(find "$dir" -maxdepth 1 -name "${base}.bak-*" 2>/dev/null)
    elif [[ -d "$p" ]]; then
      while IFS= read -r f; do
        before_filter+=("$f")
      done < <(find "$p" -name "*.bak-*" -type f 2>/dev/null)
    fi
  done

  if [[ "$SINCE_DAYS" -gt 0 ]]; then
    local cutoff_epoch
    cutoff_epoch=$(($(date +%s) - SINCE_DAYS * 86400))
    for f in "${before_filter[@]}"; do
      local mtime
      if mtime=$(stat -f %m "$f" 2>/dev/null) || mtime=$(stat -c %Y "$f" 2>/dev/null); then
        if [[ "$mtime" -ge "$cutoff_epoch" ]]; then
          echo "$f"
        fi
      fi
    done
  else
    for f in "${before_filter[@]}"; do
      echo "$f"
    done
  fi
}

derive_target() {
  # ~/.claude/settings.json.bak-2026-04-30-1455 → ~/.claude/settings.json
  local bak="$1"
  echo "${bak%%.bak-*}"
}

restore_one() {
  local bak="$1"
  local target
  target="$(derive_target "$bak")"

  if [[ ! -s "$bak" ]]; then
    warn "Skipping (backup is empty): $bak"
    return 1
  fi

  if [[ ! -f "$target" ]]; then
    log "Live file missing for: $bak (will create from backup)"
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY RUN: would restore $bak → $target"
    log "         and rename $target → ${target}.pre-restore-$(date +%Y%m%d-%H%M%S)"
    return 0
  fi

  if [[ -f "$target" ]]; then
    local pre_restore
    pre_restore="${target}.pre-restore-$(date +%Y%m%d-%H%M%S)"
    if ! mv "$target" "$pre_restore"; then
      err "Failed to back up live file before restore: $target"
      return 1
    fi
    log "Live file preserved at: $pre_restore"
  fi

  if cp "$bak" "$target"; then
    ok "Restored $target"
    return 0
  fi
  err "Failed to restore: $bak → $target"
  return 1
}

# === main ===

hdr "ai-brain-starter — restore from .bak files"

BACKUPS=()
while IFS= read -r line; do
  [[ -n "$line" ]] && BACKUPS+=("$line")
done < <(find_backups | sort -r)

if [[ ${#BACKUPS[@]} -eq 0 ]]; then
  log "No .bak-* files found. Nothing to restore."
  exit 0
fi

echo ""
echo "Found ${#BACKUPS[@]} backup file(s):"
for f in "${BACKUPS[@]}"; do
  size=$(stat -f %z "$f" 2>/dev/null || stat -c %s "$f" 2>/dev/null || echo "?")
  if mtime_h=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$f" 2>/dev/null) || \
     mtime_h=$(date -d "@$(stat -c %Y "$f" 2>/dev/null)" "+%Y-%m-%d %H:%M" 2>/dev/null); then :; else
    mtime_h="?"
  fi
  printf "  - %s (%s bytes, %s)\n" "$f" "$size" "$mtime_h"
done

if [[ "$LIST_ONLY" -eq 1 ]]; then
  exit 0
fi

echo ""
restored=0
skipped=0
failed=0

for bak in "${BACKUPS[@]}"; do
  target="$(derive_target "$bak")"
  echo ""
  echo "Backup: $bak"
  echo "Target: $target"

  if [[ "$YES" -eq 0 ]]; then
    read -r -p "  Restore this? [y/N/q] " ans
    case "$ans" in
      q|Q) break ;;
      y|Y) ;;
      *) skipped=$((skipped+1)); continue ;;
    esac
  fi

  if restore_one "$bak"; then
    restored=$((restored+1))
  else
    failed=$((failed+1))
  fi
done

echo ""
hdr "Summary"
log "Restored: $restored"
log "Skipped:  $skipped"
log "Failed:   $failed"

if [[ "$failed" -gt 0 ]]; then
  exit 1
fi
exit 0
