#!/bin/bash
# sync-skills.sh — thin delegator to sync-skills.py, the canonical
# cross-platform implementation (macOS / Linux / Windows).
#
# Kept because docs, bootstrap flows, and older auto-update wirings call this
# path. All sync logic (backups before overwrite, symlink + fork skip guards,
# .sync.log summary, and the vault-scripts refresh) lives in the .py. Exit code
# propagates (0 clean, 2 on any file error) so callers can surface failures.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
exec python3 "$HERE/sync-skills.py" "$@"
