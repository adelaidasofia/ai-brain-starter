#!/usr/bin/env bash
# exit-contract: NOT-A-CHECKER -- installs a launchd or cron agent; a
#   one-shot installer

# install-vault-daily-maintenance.sh
#
# One-shot installer for the daily vault-maintenance launchd agent on macOS.
# Substitutes the template plist with operator-specific paths, copies it to
# ~/Library/LaunchAgents/, and loads it. The job runs once a day at 04:30 local,
# at low CPU + IO priority, and is itself load-gated + mutex-serialized.
#
# Usage:
#   ./scripts/install-vault-daily-maintenance.sh /abs/path/to/vault
#
# Idempotent: re-running unloads the old plist before writing the new one.
#
# Requires: macOS, launchctl, /bin/bash. Linux users: add a cron line instead
# (see the comment block in templates/launchd/com.abs.vault-daily-maintenance.plist.template).
set -euo pipefail

VAULT_ROOT=""
QUIET=0
for arg in "$@"; do
    case "$arg" in
        --quiet) QUIET=1 ;;
        *) [[ -z "$VAULT_ROOT" ]] && VAULT_ROOT="$arg" ;;
    esac
done
say() { [[ $QUIET -eq 1 ]] || echo "$@"; }

if [[ -z "$VAULT_ROOT" ]]; then
    echo "usage: $0 /abs/path/to/vault [--quiet]" >&2
    exit 2
fi
if [[ ! -d "$VAULT_ROOT" ]]; then
    echo "vault root does not exist: $VAULT_ROOT" >&2
    exit 2
fi

# Opt-OUT only (MYC-2363). This job is what keeps the machine from only ever
# getting fuller, so nothing may gate its VALUE behind an opt-in — an opt-in
# default leaves every fresh install without any GC at all, which is the bug.
# A power user who drives reclaim by hand sets ABS_NO_AUTO_GC=1.
if [[ "${ABS_NO_AUTO_GC:-0}" == "1" ]]; then
    say "[install-vault-daily-maintenance] skipped (ABS_NO_AUTO_GC=1)"
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---- Linux / non-macOS: a cron line, same job, same gates ------------------
# Without this branch the whole daily pass is macOS-only, so a Linux install
# gets no GC at all while looking installed.
if [[ "$(uname -s)" != "Darwin" ]]; then
    if ! command -v crontab >/dev/null 2>&1; then
        say "[install-vault-daily-maintenance] no launchd and no crontab — daily maintenance NOT scheduled."
        say "  Run it from your own scheduler: bash $REPO_ROOT/scripts/vault-daily-maintenance.sh --vault-root $VAULT_ROOT"
        exit 0
    fi
    MARKER="# ai-brain-starter: vault-daily-maintenance"
    LINE="30 4 * * * /bin/bash $REPO_ROOT/scripts/vault-daily-maintenance.sh --vault-root '$VAULT_ROOT' >/dev/null 2>&1 $MARKER"
    # Idempotent: drop any previous line we installed, then append the current
    # one, so a moved repo or vault re-points instead of accumulating entries.
    ( crontab -l 2>/dev/null | grep -vF "$MARKER" || true; echo "$LINE" ) | crontab -
    say "[install-vault-daily-maintenance] cron entry installed (daily 04:30 local)"
    say "Stop: crontab -l | grep -vF '$MARKER' | crontab -"
    exit 0
fi

TEMPLATE="$REPO_ROOT/templates/launchd/com.abs.vault-daily-maintenance.plist.template"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_FILE="$TARGET_DIR/com.abs.vault-daily-maintenance.plist"
LOG_DIR="$HOME/.local/state/ai-brain-starter"

if [[ ! -f "$TEMPLATE" ]]; then
    echo "template missing at $TEMPLATE" >&2
    exit 2
fi

mkdir -p "$TARGET_DIR" "$LOG_DIR"

if [[ -f "$TARGET_FILE" ]]; then
    say "[install-vault-daily-maintenance] unloading existing plist..."
    launchctl unload "$TARGET_FILE" 2>/dev/null || true
fi

# Substitute placeholders. sed -i differs across macOS / GNU; use a temp file.
sed \
    -e "s|{{REPO_ROOT}}|$REPO_ROOT|g" \
    -e "s|{{VAULT_ROOT}}|$VAULT_ROOT|g" \
    -e "s|{{LOG_DIR}}|$LOG_DIR|g" \
    "$TEMPLATE" > "$TARGET_FILE"

say "[install-vault-daily-maintenance] wrote $TARGET_FILE"
launchctl load "$TARGET_FILE"
say "[install-vault-daily-maintenance] loaded (runs daily at 04:30 local)"
say
say "Logs:           $LOG_DIR/vault-daily-maintenance.{out,err}.log"
say "Run now (test): bash $REPO_ROOT/scripts/vault-daily-maintenance.sh --vault-root $VAULT_ROOT --force"
say "Stop:           launchctl unload $TARGET_FILE  (or ABS_NO_AUTO_GC=1 before re-install)"
