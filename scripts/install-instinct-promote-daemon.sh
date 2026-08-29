#!/usr/bin/env bash
# install-instinct-promote-daemon.sh
#
# One-shot installer for the Instinct Engine's daily promotion pass (macOS).
#
# The engine's confidence update is bidirectional but MANUAL -- it only moves
# when a human runs /patterns and invokes `reinforce`/`correct`. Left to that,
# it does not run, and every stored confidence stays the seed it was born with
# while the observation ledger fills with evidence nothing reads. This installs
# the scheduled half.
#
# Usage:
#   ./scripts/install-instinct-promote-daemon.sh /abs/path/to/vault
#
# Idempotent: re-running unloads the old plist before writing the new one.
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
TEMPLATE="$REPO_ROOT/templates/launchd/com.abs.instinct-promote.plist.template"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_FILE="$TARGET_DIR/com.abs.instinct-promote.plist"
LOG_DIR="$HOME/.local/state/ai-brain-starter"

if [[ ! -f "$TEMPLATE" ]]; then
    echo "template missing at $TEMPLATE" >&2
    exit 2
fi

# Resolve the memory dir explicitly and FAIL if it is not there. The engine's
# own auto-detect would fall back to globbing ~/.claude/projects/*/memory and
# silently pick whichever sorts first -- a scheduled job that quietly promotes
# against the wrong store looks identical to one working correctly.
MEMORY_DIR=""
for CAND in "$VAULT_ROOT/⚙️ Meta/Agent Memory" "$VAULT_ROOT/Meta/Agent Memory"; do
    if [[ -d "$CAND" ]]; then MEMORY_DIR="$CAND"; break; fi
done
if [[ -z "$MEMORY_DIR" ]]; then
    echo "no 'Agent Memory' directory under $VAULT_ROOT" >&2
    echo "looked for: '⚙️ Meta/Agent Memory' and 'Meta/Agent Memory'" >&2
    exit 2
fi

mkdir -p "$TARGET_DIR" "$LOG_DIR"

if [[ -f "$TARGET_FILE" ]]; then
    echo "[install-instinct-promote] unloading existing plist..."
    launchctl unload "$TARGET_FILE" 2>/dev/null || true
fi

# sed -i differs across macOS / GNU; write through a temp file.
sed \
    -e "s|{{REPO_ROOT}}|$REPO_ROOT|g" \
    -e "s|{{MEMORY_DIR}}|$MEMORY_DIR|g" \
    -e "s|{{LOG_DIR}}|$LOG_DIR|g" \
    "$TEMPLATE" > "$TARGET_FILE"

if ! plutil -lint "$TARGET_FILE" >/dev/null 2>&1; then
    echo "rendered plist is malformed (a path probably contains a character" >&2
    echo "that broke the XML). Refusing to load: $TARGET_FILE" >&2
    exit 1
fi

echo "[install-instinct-promote] wrote $TARGET_FILE"
echo "[install-instinct-promote] memory dir: $MEMORY_DIR"
launchctl load "$TARGET_FILE"
echo "[install-instinct-promote] loaded (runs daily 04:20)"
echo
echo "Dry-run it now:  /usr/bin/python3 '$REPO_ROOT/scripts/instinct.py' promote --dry-run --memory-dir '$MEMORY_DIR'"
echo "Logs:            $LOG_DIR/instinct-promote.{out,err}.log"
echo "Liveness:        ~/.claude/instinct/promote-state.json -> last_run"
echo "Stop:            launchctl unload $TARGET_FILE"
