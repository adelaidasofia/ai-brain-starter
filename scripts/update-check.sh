#!/usr/bin/env bash
# exit-contract: ADVISORY -- a session-start rule parses the STATUS token out
#   of stdout; a non-zero on the ordinary you-have-updates path would read as
#   a failure

#
# update-check.sh — daily drift detector for the ai-brain-starter setup
#
# Designed to be called by Claude at the start of every session via a CLAUDE.md
# rule. Outputs a structured, parseable result that Claude can translate into
# plain English for the user.
#
# Behavior:
#   - Runs at most once per day (tracked via ~/.claude/.ai-brain-starter-last-check).
#   - If we already checked today: prints UP_TO_DATE and exits 0.
#   - Otherwise: runs `git fetch --quiet` against the user's local clone.
#   - Compares HEAD to origin/main.
#   - If they differ: prints BEHIND, the count of new commits, the new CHANGELOG
#     entries (plain text), and exits 0. The CHANGELOG slice is what Claude
#     translates into plain English for the user.
#   - If they match: prints UP_TO_DATE and exits 0.
#   - Always writes today's date to the check file at the end (success or
#     "nothing to do"), so we don't ask again until tomorrow.
#
# This script never installs anything. It only reports. Installation is done
# by bootstrap.sh, which Claude calls if the user says "yes, install."
#
# Output format (parseable by Claude):
#   STATUS: <UP_TO_DATE | BEHIND | ERROR | SKIPPED_TODAY>
#   COMMITS_BEHIND: <integer>
#   CURRENT_HEAD: <short sha>
#   LATEST_HEAD: <short sha>
#   ---CHANGELOG_NEW---
#   <new CHANGELOG entries since the user's current commit, or empty>
#   ---END---
#
# Usage (called from a Claude session via the CLAUDE.md rule):
#   bash ~/.claude/skills/ai-brain-starter/scripts/update-check.sh
#
# Force a re-check (bypass the once-per-day cooldown):
#   bash ~/.claude/skills/ai-brain-starter/scripts/update-check.sh --force

set -uo pipefail

REPO_DIR="$HOME/.claude/skills/ai-brain-starter"
CHECK_FILE="$HOME/.claude/.ai-brain-starter-last-check"
TODAY="$(date +%Y-%m-%d)"
FORCE=0

# Parse args
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
  esac
done

# Daily cooldown: skip if already checked today (unless --force)
if [[ $FORCE -eq 0 && -f "$CHECK_FILE" ]]; then
  LAST="$(cat "$CHECK_FILE" 2>/dev/null || echo '')"
  if [[ "$LAST" == "$TODAY" ]]; then
    echo "STATUS: SKIPPED_TODAY"
    exit 0
  fi
fi

# Repo must exist
if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "STATUS: ERROR"
  echo "REASON: ai-brain-starter not installed at $REPO_DIR"
  exit 0
fi

cd "$REPO_DIR" || { echo "STATUS: ERROR"; echo "REASON: cannot enter $REPO_DIR"; exit 0; }

# Fetch quietly. If network is down or auth fails, treat as ERROR but don't crash.
if ! git fetch --quiet origin main 2>/dev/null; then
  echo "STATUS: ERROR"
  echo "REASON: git fetch failed (network down, or repo unreachable)"
  echo "$TODAY" > "$CHECK_FILE"
  exit 0
fi

CURRENT="$(git rev-parse HEAD 2>/dev/null)"
LATEST="$(git rev-parse origin/main 2>/dev/null)"

if [[ -z "$CURRENT" || -z "$LATEST" ]]; then
  echo "STATUS: ERROR"
  echo "REASON: could not read HEAD or origin/main"
  echo "$TODAY" > "$CHECK_FILE"
  exit 0
fi

if [[ "$CURRENT" == "$LATEST" ]]; then
  echo "STATUS: UP_TO_DATE"
  echo "CURRENT_HEAD: $(git rev-parse --short HEAD)"
  echo "$TODAY" > "$CHECK_FILE"
  exit 0
fi

# Behind. Count commits and extract the new CHANGELOG slice.
COMMITS_BEHIND="$(git rev-list --count "$CURRENT..$LATEST" 2>/dev/null || echo 0)"

echo "STATUS: BEHIND"
echo "COMMITS_BEHIND: $COMMITS_BEHIND"
echo "CURRENT_HEAD: $(git rev-parse --short "$CURRENT")"
echo "LATEST_HEAD: $(git rev-parse --short "$LATEST")"
echo "---CHANGELOG_NEW---"

# Extract the CHANGELOG entries that are in origin/main but not in HEAD.
# Strategy: dump the CHANGELOG.md from origin/main, then walk it from the top,
# stopping at the first H2 heading whose body is unchanged from the user's
# current CHANGELOG.md (i.e. an entry they already have). Print everything
# above that line — those are the new entries.

NEW_CHANGELOG="$(git show origin/main:docs/CHANGELOG.md 2>/dev/null || git show origin/main:CHANGELOG.md 2>/dev/null || echo '')"
CURRENT_CHANGELOG="$(cat docs/CHANGELOG.md 2>/dev/null || cat CHANGELOG.md 2>/dev/null || echo '')"

if [[ -n "$NEW_CHANGELOG" && -n "$CURRENT_CHANGELOG" ]]; then
  # Find the first H2 line in the current changelog
  FIRST_CURRENT_H2="$(echo "$CURRENT_CHANGELOG" | grep -m1 '^## ' || echo '')"
  if [[ -n "$FIRST_CURRENT_H2" ]]; then
    # Print everything in the new changelog ABOVE this line
    echo "$NEW_CHANGELOG" | awk -v cutoff="$FIRST_CURRENT_H2" '
      $0 == cutoff { exit }
      /^## / { in_entry = 1 }
      in_entry { print }
    '
  else
    # Current changelog has no H2 — just dump the whole new one
    echo "$NEW_CHANGELOG"
  fi
fi

echo "---END---"
echo "$TODAY" > "$CHECK_FILE"
exit 0
