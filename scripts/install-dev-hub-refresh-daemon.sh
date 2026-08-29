#!/usr/bin/env bash
# exit-contract: NOT-A-CHECKER -- installs a launchd plist and edits the
#   global gitignore

# install-dev-hub-refresh-daemon.sh
#
# One-shot installer for the bare-~/dev-hub freshness daemon on macOS (MYC-1893).
# It does two things:
#   1. Ensures the codegraph index dir (.codegraph/) is GLOBALLY git-ignored, so
#      codegraph indexing can never dirty a bare ~/dev hub (a background-job write
#      that otherwise blocks the hub's auto-fast-forward).
#   2. Renders templates/launchd/com.abs.dev-hub-refresh.plist.template with this
#      clone's path, installs it to ~/Library/LaunchAgents/, and loads it.
#
# The daemon runs scripts/dev-hub-refresh.py --apply every 6h (and once at load):
# fetch-first, fast-forward CLEAN hubs sitting on their default branch (a
# guaranteed, reflog-reversible fast-forward), and surface the rest. It NEVER
# switches branches, cleans a dirty tree, or forces anything.
#
# Usage:
#   ./scripts/install-dev-hub-refresh-daemon.sh
#
# Idempotent: re-running unloads the old plist first, and the global-ignore line
# is added at most once.
#
# Requires: macOS, launchctl, /usr/bin/python3, git.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO_ROOT/templates/launchd/com.abs.dev-hub-refresh.plist.template"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_FILE="$TARGET_DIR/com.abs.dev-hub-refresh.plist"
LOG_DIR="$HOME/.local/state/ai-brain-starter"

if [[ ! -f "$TEMPLATE" ]]; then
    echo "template missing at $TEMPLATE" >&2
    exit 2
fi

# --- 1. ensure codegraph machinery is globally git-ignored (never dirties a hub) ---
GLOBAL_IGNORE="$(git config --global core.excludesfile 2>/dev/null || true)"
if [[ -z "$GLOBAL_IGNORE" ]]; then
    GLOBAL_IGNORE="${XDG_CONFIG_HOME:-$HOME/.config}/git/ignore"
fi
# Expand a leading ~ that git may store literally.
GLOBAL_IGNORE="${GLOBAL_IGNORE/#\~/$HOME}"
mkdir -p "$(dirname "$GLOBAL_IGNORE")"
touch "$GLOBAL_IGNORE"
if ! grep -qxF '.codegraph/' "$GLOBAL_IGNORE"; then
    {
        echo ""
        echo "# Codegraph knowledge-graph index (machine-local, rebuilt per-repo, never synced)."
        echo "# Global-ignored so indexing a bare ~/dev hub cannot dirty its tree (MYC-1893)."
        echo ".codegraph/"
    } >> "$GLOBAL_IGNORE"
    echo "[install-dev-hub-refresh-daemon] added .codegraph/ to $GLOBAL_IGNORE"
fi

# --- 2. install + load the launchd agent ---
mkdir -p "$TARGET_DIR" "$LOG_DIR"
if [[ -f "$TARGET_FILE" ]]; then
    echo "[install-dev-hub-refresh-daemon] unloading existing plist..."
    launchctl unload "$TARGET_FILE" 2>/dev/null || true
fi

# sed -i differs across macOS / GNU; render via a temp redirect.
sed \
    -e "s|{{REPO_ROOT}}|$REPO_ROOT|g" \
    -e "s|{{LOG_DIR}}|$LOG_DIR|g" \
    "$TEMPLATE" > "$TARGET_FILE"
echo "[install-dev-hub-refresh-daemon] wrote $TARGET_FILE"
launchctl load "$TARGET_FILE"
echo "[install-dev-hub-refresh-daemon] loaded + scheduled (every 6h + at load)"
echo
echo "Logs:        $LOG_DIR/dev-hub-refresh.{out,err}.log"
echo "Run now:     python3 $REPO_ROOT/scripts/dev-hub-refresh.py --apply"
echo "Dry-run:     python3 $REPO_ROOT/scripts/dev-hub-refresh.py"
echo "Stop:        launchctl unload $TARGET_FILE"
