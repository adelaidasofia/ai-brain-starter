#!/bin/bash
# exit-contract: NOT-A-CHECKER -- takes an hourly git snapshot commit of the
#   vault

# auto-snapshot.sh — hourly snapshot of the personal vault to local git.
#
# What it does:
#   1. cd into the vault root
#   2. git add -A (respects .gitignore)
#   3. If anything is staged, git commit with a timestamped message
#   4. If nothing changed, exit silently (no empty commits, no log spam)
#
# Why local-only is safe:
#   The vault has a chflag-immutable pre-push hook that blocks ALL pushes.
#   Local commits stay in .git/objects/ on this laptop and never network out.
#   Combined with the pre-commit remote-scrubbing hook, there is no path
#   from auto-commit to GitHub. See .git/hooks/pre-push for the full
#   protection model.
#
# Recovery from a lost-update:
#   git -C "<vault>" log --all --oneline --diff-filter=M -- "<file>"
#   git -C "<vault>" show <commit>:"<file>" > /tmp/recovered.md
#
# Logs to: Meta/.auto-snapshot.log (one line per run)
#
# Installed as the bottom-of-stack defense against lost-update
# bugs in concurrent Claude Code sessions.

set -euo pipefail

# Auto-detect vault root from script location or $VAULT_ROOT env var
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT="${VAULT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
LOG="$VAULT/⚙️ Meta/.auto-snapshot.log"
TIMESTAMP=$(date +"%Y-%m-%dT%H:%M:%S")

cd "$VAULT" || {
  echo "[$TIMESTAMP] FAIL — vault not found at $VAULT" >> "$LOG"
  exit 1
}

# Defensive: refuse to run if a remote exists. The pre-commit hook will scrub
# any remote at commit time, but we're being doubly safe — if a remote sneaks
# in, we'd rather log + exit than commit-and-scrub.
if git remote 2>/dev/null | grep -q .; then
  echo "[$TIMESTAMP] WARN — remote(s) detected, refusing to snapshot until removed" >> "$LOG"
  git remote >> "$LOG"
  exit 1
fi

# Guard: abort if vault has >5000 tracked files (likely a large Obsidian vault).
# git add -A on a 60K-file vault takes 10+ min and locks .git/index.lock.
TRACKED=$(git ls-files 2>/dev/null | wc -l | tr -d ' ')
if [ "$TRACKED" -gt 5000 ]; then
  echo "[$TIMESTAMP] ABORT — $TRACKED tracked files; git add -A would walk the full tree. Use session-close targeted staging instead." >> "$LOG"
  exit 1
fi

# Stage everything respecting .gitignore
git add -A 2>>"$LOG"

# Check if anything is actually staged. If not, exit silently.
if git diff --cached --quiet; then
  # Don't even log no-op runs — too noisy at hourly frequency.
  exit 0
fi

# Count staged changes for the commit message
NUM_FILES=$(git diff --cached --name-only | wc -l | tr -d ' ')

# Commit. The pre-commit hook will fire and scrub any remote that snuck in.
# Use --no-verify? NO — we WANT the pre-commit hook to fire here.
COMMIT_MSG="auto: snapshot $TIMESTAMP ($NUM_FILES files changed)"

if git commit -m "$COMMIT_MSG" >> "$LOG" 2>&1; then
  echo "[$TIMESTAMP] OK — committed $NUM_FILES files" >> "$LOG"
else
  echo "[$TIMESTAMP] FAIL — git commit errored" >> "$LOG"
  exit 1
fi

# Trim the log file if it grows past 10 MB (keep last 5 MB)
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG" 2>/dev/null || stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 10485760 ]; then
  tail -c 5242880 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
