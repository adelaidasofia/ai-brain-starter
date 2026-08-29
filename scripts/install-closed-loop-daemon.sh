#!/usr/bin/env bash
# exit-contract: NOT-A-CHECKER -- installs a launchd plist; a one-shot
#   installer

# install-closed-loop-daemon.sh
#
# One-shot installer for the closed-loop daemon launchd agent on macOS.
# Substitutes the template plist with operator-specific paths, copies it to
# ~/Library/LaunchAgents/, loads it, and starts it.
#
# Usage:
#   ./scripts/install-closed-loop-daemon.sh /abs/path/to/vault
#
# Idempotent: re-running unloads the old plist before writing the new one.
#
# Requires: macOS, launchctl, /usr/bin/python3.
set -euo pipefail

VAULT_ROOT="${1:-}"
if [[ -z "$VAULT_ROOT" ]]; then
    echo "usage: $0 /abs/path/to/vault" >&2
    exit 2
fi
if [[ ! -d "$VAULT_ROOT" ]]; then
    echo "vault root does not exist: $VAULT_ROOT" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO_ROOT/templates/launchd/com.abs.closed-loop-daemon.plist.template"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_FILE="$TARGET_DIR/com.abs.closed-loop-daemon.plist"
LOG_DIR="$HOME/.local/state/ai-brain-starter"

if [[ ! -f "$TEMPLATE" ]]; then
    echo "template missing at $TEMPLATE" >&2
    exit 2
fi

mkdir -p "$TARGET_DIR" "$LOG_DIR"

if [[ -f "$TARGET_FILE" ]]; then
    echo "[install-closed-loop-daemon] unloading existing plist..."
    launchctl unload "$TARGET_FILE" 2>/dev/null || true
fi

# Substitute placeholders. sed -i differs across macOS / GNU; use a temp file.
sed \
    -e "s|{{REPO_ROOT}}|$REPO_ROOT|g" \
    -e "s|{{VAULT_ROOT}}|$VAULT_ROOT|g" \
    -e "s|{{LOG_DIR}}|$LOG_DIR|g" \
    "$TEMPLATE" > "$TARGET_FILE"

echo "[install-closed-loop-daemon] wrote $TARGET_FILE"
launchctl load "$TARGET_FILE"
echo "[install-closed-loop-daemon] loaded"
launchctl start com.abs.closed-loop-daemon || true
echo "[install-closed-loop-daemon] started"
echo
echo "Logs: $LOG_DIR/closed-loop-daemon.{out,err}.log"
echo "Stop: launchctl unload $TARGET_FILE"
