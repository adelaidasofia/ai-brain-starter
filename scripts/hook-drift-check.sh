#!/bin/bash
# exit-contract: ADVISORY -- consumed by a SessionStart surface that must not
#   block a session; drift is reported on stdout for a human to resolve row
#   by row

# hook-drift-check.sh — detect drift between ai-brain-starter/hooks.json and
# the vault's live .claude/settings.local.json
#
# Why: hooks can silently go missing from settings.local.json when the vault
# is set up or updated. No one knows until they diff by hand. This script
# automates that diff and can be wired into the session-start hook chain
# so drift surfaces early.
#
# Exits:
#   0 = no drift (or script disabled)
#   0 + stdout warning = drift detected (still non-fatal so hooks don't block sessions)
#
# Usage:
#   bash hook-drift-check.sh                         # human-readable output
#   bash hook-drift-check.sh --json                  # machine-readable for hooks
#   bash hook-drift-check.sh --quiet                 # silent unless drift
#   bash hook-drift-check.sh --vault /path/to/vault  # explicit vault path
#
# Dependencies: jq. No git, no network.

set -uo pipefail

# Auto-detect vault root from script location or $VAULT_ROOT
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECTED_VAULT="${VAULT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Repo hooks template — check the full clone first (always has hooks.json),
# fall back to the installed skill folder (may or may not have it depending
# on how sync-skills.sh last ran).
REPO_HOOKS=""
for candidate in \
  "$HOME/Desktop/ai-brain-starter/hooks.json" \
  "$HOME/.claude/skills/ai-brain-starter/hooks.json" \
; do
  if [ -f "$candidate" ]; then
    REPO_HOOKS="$candidate"
    break
  fi
done

LOCAL_SETTINGS=""
MODE="human"
for arg in "$@"; do
  case "$arg" in
    --json) MODE="json" ;;
    --quiet) MODE="quiet" ;;
    --vault)
      # Next arg is the vault path — handled below
      ;;
    *)
      # Check if previous arg was --vault
      if [ "${prev_arg:-}" = "--vault" ]; then
        DETECTED_VAULT="$arg"
      fi
      ;;
  esac
  prev_arg="$arg"
done

LOCAL_SETTINGS="$DETECTED_VAULT/.claude/settings.local.json"

log() {
  [ "$MODE" = "quiet" ] && return
  [ "$MODE" = "json" ] && return
  echo "$@"
}

emit_json() {
  local status="$1"
  local message="$2"
  local details="${3:-[]}"
  if [ "$MODE" = "json" ]; then
    jq -c -n --arg status "$status" --arg message "$message" --argjson details "$details" \
      '{status: $status, message: $message, details: $details}'
  fi
}

if ! command -v jq >/dev/null 2>&1; then
  log "ERROR: jq not installed."
  emit_json "error" "jq not installed" "[]"
  exit 0
fi

if [ -z "$REPO_HOOKS" ]; then
  log "SKIP: repo hooks template not found in any known location"
  emit_json "skip" "repo hooks template missing" "[]"
  exit 0
fi
log "comparing against: $REPO_HOOKS"

if [ ! -f "$LOCAL_SETTINGS" ]; then
  log "SKIP: vault settings.local.json not found at $LOCAL_SETTINGS"
  emit_json "skip" "vault settings.local.json missing" "[]"
  exit 0
fi

# --- Compare hook counts per event type -------------------------------------
DRIFT_DETAILS="[]"
DRIFT_FOUND=0

check_event() {
  local event="$1"
  local repo_count local_count
  repo_count=$(jq -r --arg e "$event" '.hooks[$e][0].hooks | length // 0' "$REPO_HOOKS" 2>/dev/null || echo 0)
  local_count=$(jq -r --arg e "$event" '.hooks[$e][0].hooks | length // 0' "$LOCAL_SETTINGS" 2>/dev/null || echo 0)

  if [ "$repo_count" -ne "$local_count" ]; then
    DRIFT_FOUND=1
    log ""
    log "DRIFT: $event hooks — repo has $repo_count, vault has $local_count"
    DRIFT_DETAILS=$(jq -c --arg event "$event" --argjson rc "$repo_count" --argjson lc "$local_count" \
      '. += [{event: $event, kind: "count_mismatch", repo_count: $rc, local_count: $lc}]' <<<"$DRIFT_DETAILS")
  fi

  local i=0
  while [ "$i" -lt "$repo_count" ]; do
    local repo_cmd needle
    repo_cmd=$(jq -r --arg e "$event" --argjson i "$i" '.hooks[$e][0].hooks[$i].command // ""' "$REPO_HOOKS")
    if [ -z "$repo_cmd" ]; then i=$((i+1)); continue; fi

    needle=$(echo "$repo_cmd" | grep -oE '[a-zA-Z0-9_-]+\.(sh|py)' | head -1 || true)
    if [ -z "$needle" ]; then
      needle=$(echo "$repo_cmd" | grep -oE 'hookEventName[^"]*"[^"]+' | head -1 || true)
    fi
    [ -z "$needle" ] && needle=$(echo "$repo_cmd" | sed 's/^ *//' | head -c 40)

    if ! jq -r --arg e "$event" '.hooks[$e][0].hooks[]?.command // ""' "$LOCAL_SETTINGS" | grep -qF "$needle"; then
      DRIFT_FOUND=1
      log "   missing locally: $(echo "$repo_cmd" | head -c 100)..."
      log "     (needle: $needle)"
      DRIFT_DETAILS=$(jq -c --arg event "$event" --argjson i "$i" --arg snippet "$(echo "$repo_cmd" | head -c 100)" --arg needle "$needle" \
        '. += [{event: $event, kind: "hook_missing_locally", index: $i, snippet: $snippet, needle: $needle}]' <<<"$DRIFT_DETAILS")
    fi
    i=$((i+1))
  done
}

for event in UserPromptSubmit Stop PreCompact PostToolUse SessionStart; do
  check_event "$event"
done

if [ "$DRIFT_FOUND" = "0" ]; then
  log "No drift — vault hooks match repo template"
  emit_json "ok" "no drift" "[]"
  exit 0
fi

log ""
log "To fix: review the diff, then merge new hooks into settings.local.json."
log "  A safe atomic merge template (for UserPromptSubmit[0].hooks):"
log ""
log "    jq '.hooks.UserPromptSubmit[0].hooks += [<new hook>]' \\"
log "       \"\$LOCAL_SETTINGS\" > /tmp/merged.json && \\"
log "       cp \"\$LOCAL_SETTINGS\" \"\$LOCAL_SETTINGS.bak-\$(date +%Y-%m-%d-%H%M)\" && \\"
log "       mv /tmp/merged.json \"\$LOCAL_SETTINGS\""
log ""

emit_json "drift" "hook drift detected — see details" "$DRIFT_DETAILS"
exit 0
