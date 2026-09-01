#!/bin/bash
# exit-contract: NOT-A-CHECKER -- renders a markdown dashboard from JSONL
#   rows

# traffic-digest.sh — render repo-traffic-log.jsonl as a readable markdown dashboard
#
# Reads every snapshot in Meta/repo-traffic-log.jsonl and writes
# Meta/Repo Traffic Dashboard.md — the version you actually open in Obsidian
# on Sunday morning. Computes week-over-week deltas, top 5 referrers across
# time, commit velocity, social deltas, and a sparkline-style history table.
#
# Run manually:   bash traffic-digest.sh
# Run via cron:   traffic-snapshot.sh chains this automatically (see end of that file)
#
# Dependencies: jq. No gh required — this script only reads the local JSONL.

set -uo pipefail

# Auto-detect vault root from script location or $VAULT_ROOT
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT="${VAULT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Auto-detect the Meta folder via the shared resolver (prefers the variant
# containing a known human-memory subfolder, so a machine "Meta/" can't shadow
# the human "⚙️ Meta/"). See scripts/_meta_resolver.py.
VAULT_META="$(python3 "$SCRIPT_DIR/_meta_resolver.py" "$VAULT" Decisions 2>/dev/null || true)"
[ -z "$VAULT_META" ] && VAULT_META="$VAULT/Meta"

LOG_FILE="$VAULT_META/repo-traffic-log.jsonl"
DASHBOARD="$VAULT_META/Repo Traffic Dashboard.md"
ERROR_LOG="$VAULT_META/repo-traffic-log.error.log"
STAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

if [ ! -f "$LOG_FILE" ] || [ ! -s "$LOG_FILE" ]; then
  echo "[$STAMP] ERROR: $LOG_FILE missing or empty. Run traffic-snapshot.sh first." >> "$ERROR_LOG"
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "[$STAMP] ERROR: jq not in PATH. Install with 'brew install jq'." >> "$ERROR_LOG"
  exit 1
fi

# Grab everything as a JSON array so we can operate on the whole history at once.
ALL_SNAPSHOTS=$(jq -s '.' "$LOG_FILE")
SNAPSHOT_COUNT=$(echo "$ALL_SNAPSHOTS" | jq 'length')
LATEST=$(echo "$ALL_SNAPSHOTS" | jq '.[-1]')

# --- Scalars from the latest snapshot ---------------------------------------
LATEST_TIMESTAMP=$(echo "$LATEST" | jq -r '.timestamp')
LATEST_REPO=$(echo "$LATEST" | jq -r '.repo')
LATEST_HEAD_SHA=$(echo "$LATEST" | jq -r '.head_sha')
LATEST_STARS=$(echo "$LATEST" | jq -r '.social.stars')
LATEST_WATCHERS=$(echo "$LATEST" | jq -r '.social.watchers')
LATEST_FORKS=$(echo "$LATEST" | jq -r '.social.forks')
LATEST_OPEN_ISSUES=$(echo "$LATEST" | jq -r '.social.open_issues')
LATEST_CLONES=$(echo "$LATEST" | jq -r '.clones_14d.count')
LATEST_CLONES_UNIQ=$(echo "$LATEST" | jq -r '.clones_14d.uniques')
LATEST_VIEWS=$(echo "$LATEST" | jq -r '.views_14d.count')
LATEST_VIEWS_UNIQ=$(echo "$LATEST" | jq -r '.views_14d.uniques')

DELTA_STARS=$(echo "$LATEST" | jq -r '.deltas.stars // 0')
DELTA_COMMITS=$(echo "$LATEST" | jq -r '.deltas.commits // 0')
DELTA_FORKS=$(echo "$LATEST" | jq -r '.deltas.forks // 0')
DELTA_ISSUES=$(echo "$LATEST" | jq -r '.deltas.new_issues // 0')

# --- Top 5 referrers aggregated across ALL snapshots ------------------------
TOP_REFERRERS_MD=$(echo "$ALL_SNAPSHOTS" | jq -r '
  [.[] | .top_referrers[]? // empty]
  | group_by(.referrer)
  | map({
      referrer: .[0].referrer,
      total_uniques: (map(.uniques) | add),
      total_count: (map(.count) | add),
      appearances: length
    })
  | sort_by(-.total_uniques)
  | .[0:5]
  | map("| \(.referrer) | \(.total_uniques) | \(.total_count) | \(.appearances) |")
  | join("\n")
')

# --- Top 5 paths aggregated across ALL snapshots ----------------------------
TOP_PATHS_MD=$(echo "$ALL_SNAPSHOTS" | jq -r '
  [.[] | .top_paths[]? // empty]
  | group_by(.path)
  | map({
      path: .[0].path,
      total_uniques: (map(.uniques) | add),
      total_count: (map(.count) | add)
    })
  | sort_by(-.total_uniques)
  | .[0:5]
  | map("| `\(.path)` | \(.total_uniques) | \(.total_count) |")
  | join("\n")
')

# --- Snapshot history table (reverse chronological, last 12 rows) -----------
HISTORY_MD=$(echo "$ALL_SNAPSHOTS" | jq -r '
  [.[] | {
    ts: .timestamp,
    clones: .clones_14d.count,
    clones_uniq: .clones_14d.uniques,
    views: .views_14d.count,
    views_uniq: .views_14d.uniques,
    stars: .social.stars,
    forks: .social.forks,
    commits_delta: (.deltas.commits // 0)
  }]
  | reverse
  | .[0:12]
  | map("| \(.ts[0:10]) | \(.clones) / \(.clones_uniq) | \(.views) / \(.views_uniq) | \(.stars) | \(.forks) | +\(.commits_delta) |")
  | join("\n")
')

# --- Cumulative growth across all snapshots ---------------------------------
TOTAL_CLONES_EVER=$(echo "$ALL_SNAPSHOTS" | jq '[.[] | .clones_14d.count] | add // 0')
TOTAL_VIEWS_EVER=$(echo "$ALL_SNAPSHOTS" | jq '[.[] | .views_14d.count] | add // 0')
FIRST_SNAPSHOT_DATE=$(echo "$ALL_SNAPSHOTS" | jq -r '.[0].timestamp[0:10]')

# --- Write the dashboard ----------------------------------------------------
cat > "$DASHBOARD" <<EOF
---
creationDate: $(date +%Y-%m-%d)
type: meta
last_updated: ${LATEST_TIMESTAMP:0:10}
aliases: [repo traffic, github traffic]
---

# Repo Traffic Dashboard -- \`$LATEST_REPO\`

*Auto-generated by \`traffic-digest.sh\` on ${STAMP}. Never edit by hand -- changes will be overwritten. Source: \`repo-traffic-log.jsonl\` (${SNAPSHOT_COUNT} snapshot$([ "$SNAPSHOT_COUNT" = "1" ] || echo s), first logged ${FIRST_SNAPSHOT_DATE}).*

## Latest snapshot -- ${LATEST_TIMESTAMP:0:10}

| Metric | Value | Delta since last |
|---|---|---|
| **Clones (14d)** | ${LATEST_CLONES} total / ${LATEST_CLONES_UNIQ} unique | -- |
| **Views (14d)** | ${LATEST_VIEWS} total / ${LATEST_VIEWS_UNIQ} unique | -- |
| **Stars** | ${LATEST_STARS} | +${DELTA_STARS} |
| **Watchers** | ${LATEST_WATCHERS} | -- |
| **Forks** | ${LATEST_FORKS} | +${DELTA_FORKS} |
| **Open issues** | ${LATEST_OPEN_ISSUES} | +${DELTA_ISSUES} |
| **Commits since last snapshot** | +${DELTA_COMMITS} | -- |
| **HEAD SHA** | \`${LATEST_HEAD_SHA}\` | -- |

## Top referrers (lifetime, by unique visitors)

| Referrer | Unique visitors | Total visits | Snapshots seen |
|---|---|---|---|
${TOP_REFERRERS_MD:-| _(no referrer data yet)_ | -- | -- | -- |}

## Top paths (lifetime, by unique visitors)

| Path | Unique visitors | Total visits |
|---|---|---|
${TOP_PATHS_MD:-| _(no path data yet)_ | -- | -- |}

## Snapshot history (last 12)

| Date | Clones (tot/uniq) | Views (tot/uniq) | Stars | Forks | Commits delta |
|---|---|---|---|---|---|
${HISTORY_MD}

## Cumulative (across all snapshots)

- **Total clones observed:** ${TOTAL_CLONES_EVER} (sum of 14-day windows -- contains overlap if snapshots are <14 days apart)
- **Total views observed:** ${TOTAL_VIEWS_EVER} (same caveat)
- **Snapshots:** ${SNAPSHOT_COUNT}
- **First snapshot:** ${FIRST_SNAPSHOT_DATE}
- **Latest snapshot:** ${LATEST_TIMESTAMP:0:10}

## How to read this

- **Clones massively outpace views** -> people are piping the install prompt into Claude and it's cloning without anyone browsing. The blog->Claude->clone pipeline.
- **Top referrers are Substack domains** -> confirms the blog-post->prompt->clone path is the dominant install channel.
- **Stars vs clones ratio** -> most cloners don't star. That's fine for an open-source starter; stars are a social signal, clones are usage.
- **If clones suddenly drop to zero for 2+ snapshots** -> blog post fell off the feed, or the install prompt broke.
- **If clones spike 3x+ in one week** -> a new blog post landed, or someone big shared the repo.
EOF

echo "[$STAMP] traffic-digest: wrote dashboard (${SNAPSHOT_COUNT} snapshots summarized)"
