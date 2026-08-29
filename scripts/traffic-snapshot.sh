#!/bin/bash
# exit-contract: NOT-A-CHECKER -- fetches GitHub traffic and appends a JSONL
#   row

# traffic-snapshot.sh — capture a weekly snapshot of GitHub repo traffic
#
# Fetches clones, views, stars/watchers/forks, top referrers, and top paths from the
# GitHub Traffic API and appends a single JSON line to
# Meta/repo-traffic-log.jsonl in the vault.
#
# Why: the Traffic API only retains 14 days. Without this log, historical data is lost.
# Run weekly via launchd/cron or manually: bash traffic-snapshot.sh
#
# Requires: gh CLI authenticated with repo scope (`gh auth status` should pass).
# Writes to: Meta/repo-traffic-log.jsonl (JSON Lines -- one snapshot per line)
# Logs errors to: Meta/repo-traffic-log.error.log
#
# Usage:
#   bash traffic-snapshot.sh                                    # auto-detect repo + vault
#   bash traffic-snapshot.sh --repo owner/repo                 # explicit repo
#   bash traffic-snapshot.sh --repo owner/repo --repo-dir /path/to/clone

set -uo pipefail

# Auto-detect vault root from script location or $VAULT_ROOT
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT="${VAULT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Auto-detect the Meta folder via the shared resolver (prefers the variant
# containing a known human-memory subfolder, so a machine "Meta/" can't shadow
# the human "⚙️ Meta/"). See scripts/_meta_resolver.py.
VAULT_META="$(python3 "$SCRIPT_DIR/_meta_resolver.py" "$VAULT" Decisions 2>/dev/null || true)"
[ -z "$VAULT_META" ] && VAULT_META="$VAULT/Meta"

# Parse arguments
REPO=""
REPO_DIR=""
for arg in "$@"; do
  case "$arg" in
    --repo)  shift_next="repo" ;;
    --repo-dir) shift_next="repo_dir" ;;
    *)
      case "${shift_next:-}" in
        repo) REPO="$arg"; shift_next="" ;;
        repo_dir) REPO_DIR="$arg"; shift_next="" ;;
      esac
      ;;
  esac
done

# If no repo specified, try to detect from a local git clone
if [ -z "$REPO" ]; then
  # Check common locations for a repo clone
  for candidate_dir in "$HOME/Desktop/ai-brain-starter" "$HOME/ai-brain-starter"; do
    if [ -d "$candidate_dir/.git" ]; then
      REPO_DIR="$candidate_dir"
      REPO=$(cd "$candidate_dir" && git remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$||' || true)
      break
    fi
  done
fi

if [ -z "$REPO" ]; then
  echo "ERROR: Could not detect repo. Pass --repo owner/name" >&2
  exit 1
fi

LOG_FILE="$VAULT_META/repo-traffic-log.jsonl"
ERROR_LOG="$VAULT_META/repo-traffic-log.error.log"
STAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Guard: gh must be installed and authenticated
if ! command -v gh >/dev/null 2>&1; then
  echo "[$STAMP] ERROR: gh CLI not found in PATH" >> "$ERROR_LOG"
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "[$STAMP] ERROR: gh CLI not authenticated. Run 'gh auth login'." >> "$ERROR_LOG"
  exit 1
fi

# Fetch all traffic data
CLONES=$(gh api "repos/$REPO/traffic/clones" 2>/dev/null)
VIEWS=$(gh api "repos/$REPO/traffic/views" 2>/dev/null)
REFERRERS=$(gh api "repos/$REPO/traffic/popular/referrers" 2>/dev/null)
PATHS=$(gh api "repos/$REPO/traffic/popular/paths" 2>/dev/null)
REPO_META=$(gh api "repos/$REPO" 2>/dev/null)

if [ -z "$CLONES" ] || [ -z "$VIEWS" ] || [ -z "$REPO_META" ]; then
  echo "[$STAMP] ERROR: one or more GitHub API calls returned empty. Not writing snapshot." >> "$ERROR_LOG"
  exit 1
fi

STARS=$(echo "$REPO_META" | jq -r '.stargazers_count // 0')
WATCHERS=$(echo "$REPO_META" | jq -r '.subscribers_count // 0')
FORKS=$(echo "$REPO_META" | jq -r '.forks_count // 0')
OPEN_ISSUES=$(echo "$REPO_META" | jq -r '.open_issues_count // 0')
PUSHED_AT=$(echo "$REPO_META" | jq -r '.pushed_at // ""')
DEFAULT_BRANCH=$(echo "$REPO_META" | jq -r '.default_branch // "main"')

HEAD_SHA="unknown"
if [ -n "$REPO_DIR" ] && [ -d "$REPO_DIR/.git" ]; then
  HEAD_SHA=$(cd "$REPO_DIR" && git rev-parse HEAD 2>/dev/null || echo "unknown")
fi

# --- Delta fields ---
PREV_SNAPSHOT=""
if [ -f "$LOG_FILE" ] && [ -s "$LOG_FILE" ]; then
  PREV_SNAPSHOT=$(tail -n 1 "$LOG_FILE")
fi

PREV_HEAD_SHA=""
PREV_STARS=0
PREV_WATCHERS=0
PREV_FORKS=0
PREV_TIMESTAMP=""
if [ -n "$PREV_SNAPSHOT" ]; then
  PREV_HEAD_SHA=$(echo "$PREV_SNAPSHOT" | jq -r '.head_sha // ""')
  PREV_STARS=$(echo "$PREV_SNAPSHOT" | jq -r '.social.stars // 0')
  PREV_WATCHERS=$(echo "$PREV_SNAPSHOT" | jq -r '.social.watchers // 0')
  PREV_FORKS=$(echo "$PREV_SNAPSHOT" | jq -r '.social.forks // 0')
  PREV_TIMESTAMP=$(echo "$PREV_SNAPSHOT" | jq -r '.timestamp // ""')
fi

COMMITS_DELTA=0
COMMITS_SINCE_LAST="[]"
if [ -n "$REPO_DIR" ] && [ -d "$REPO_DIR/.git" ] && [ -n "$PREV_HEAD_SHA" ] && [ "$PREV_HEAD_SHA" != "unknown" ] && [ "$PREV_HEAD_SHA" != "$HEAD_SHA" ]; then
  (cd "$REPO_DIR" && git fetch --quiet origin 2>/dev/null) || true
  COMMITS_DELTA=$(cd "$REPO_DIR" && git rev-list --count "$PREV_HEAD_SHA..$HEAD_SHA" 2>/dev/null || echo 0)
  COMMITS_SINCE_LAST=$(cd "$REPO_DIR" && git log --pretty=format:'%h%x1f%s%x1f%an%x1f%aI' "$PREV_HEAD_SHA..$HEAD_SHA" 2>/dev/null \
    | jq -R -s 'split("\n") | map(select(length > 0)) | map(split("\u001f")) | map({sha: .[0], subject: .[1], author: .[2], date: .[3]})' 2>/dev/null || echo "[]")
fi

STARS_DELTA=$((STARS - PREV_STARS))
WATCHERS_DELTA=$((WATCHERS - PREV_WATCHERS))
FORKS_DELTA=$((FORKS - PREV_FORKS))

NEW_ISSUES_COUNT=0
NEW_DISCUSSIONS_COUNT=0
if [ -n "$PREV_TIMESTAMP" ]; then
  NEW_ISSUES_JSON=$(gh api "repos/$REPO/issues?since=$PREV_TIMESTAMP&state=all&per_page=100" 2>/dev/null || echo "[]")
  NEW_ISSUES_COUNT=$(echo "$NEW_ISSUES_JSON" | jq '[.[] | select(has("pull_request") | not)] | length' 2>/dev/null || echo 0)
  NEW_DISCUSSIONS_COUNT=$(gh api graphql -f query='
    query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        discussions(first: 100, orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes { createdAt }
        }
      }
    }' -F owner="${REPO%/*}" -F name="${REPO#*/}" 2>/dev/null \
    | jq --arg since "$PREV_TIMESTAMP" '[.data.repository.discussions.nodes[]? | select(.createdAt > $since)] | length' 2>/dev/null || echo 0)
fi

SNAPSHOT=$(jq -c -n \
  --arg timestamp "$STAMP" \
  --arg repo "$REPO" \
  --arg head_sha "$HEAD_SHA" \
  --arg prev_head_sha "$PREV_HEAD_SHA" \
  --arg prev_timestamp "$PREV_TIMESTAMP" \
  --arg pushed_at "$PUSHED_AT" \
  --arg default_branch "$DEFAULT_BRANCH" \
  --argjson stars "$STARS" \
  --argjson watchers "$WATCHERS" \
  --argjson forks "$FORKS" \
  --argjson open_issues "$OPEN_ISSUES" \
  --argjson stars_delta "$STARS_DELTA" \
  --argjson watchers_delta "$WATCHERS_DELTA" \
  --argjson forks_delta "$FORKS_DELTA" \
  --argjson commits_delta "$COMMITS_DELTA" \
  --argjson commits_since_last "$COMMITS_SINCE_LAST" \
  --argjson new_issues_count "$NEW_ISSUES_COUNT" \
  --argjson new_discussions_count "$NEW_DISCUSSIONS_COUNT" \
  --argjson clones "$CLONES" \
  --argjson views "$VIEWS" \
  --argjson referrers "$REFERRERS" \
  --argjson paths "$PATHS" \
  '{
    timestamp: $timestamp,
    repo: $repo,
    head_sha: $head_sha,
    pushed_at: $pushed_at,
    default_branch: $default_branch,
    previous: {
      head_sha: $prev_head_sha,
      timestamp: $prev_timestamp
    },
    social: {
      stars: $stars,
      watchers: $watchers,
      forks: $forks,
      open_issues: $open_issues
    },
    deltas: {
      stars: $stars_delta,
      watchers: $watchers_delta,
      forks: $forks_delta,
      commits: $commits_delta,
      new_issues: $new_issues_count,
      new_discussions: $new_discussions_count
    },
    commits_since_last: $commits_since_last,
    clones_14d: {
      count: $clones.count,
      uniques: $clones.uniques,
      daily: $clones.clones
    },
    views_14d: {
      count: $views.count,
      uniques: $views.uniques,
      daily: $views.views
    },
    top_referrers: $referrers,
    top_paths: $paths
  }'
)

echo "$SNAPSHOT" >> "$LOG_FILE"

CLONE_COUNT=$(echo "$SNAPSHOT" | jq -r '.clones_14d.count')
CLONE_UNIQUES=$(echo "$SNAPSHOT" | jq -r '.clones_14d.uniques')
VIEW_COUNT=$(echo "$SNAPSHOT" | jq -r '.views_14d.count')
VIEW_UNIQUES=$(echo "$SNAPSHOT" | jq -r '.views_14d.uniques')
echo "[$STAMP] snapshot appended: $CLONE_COUNT clones ($CLONE_UNIQUES unique) / $VIEW_COUNT views ($VIEW_UNIQUES unique) / $STARS stars / $FORKS forks"
echo "[$STAMP] deltas: stars +$STARS_DELTA / watchers +$WATCHERS_DELTA / forks +$FORKS_DELTA / commits +$COMMITS_DELTA / new_issues $NEW_ISSUES_COUNT / new_discussions $NEW_DISCUSSIONS_COUNT"

# Chain the digest so the dashboard always reflects the latest snapshot
DIGEST_SCRIPT="$VAULT_META/scripts/traffic-digest.sh"
if [ -x "$DIGEST_SCRIPT" ]; then
  bash "$DIGEST_SCRIPT" 2>>"$ERROR_LOG" || echo "[$STAMP] WARN: traffic-digest.sh failed (non-fatal)" >> "$ERROR_LOG"
fi
