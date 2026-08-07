#!/usr/bin/env bash
# ai-brain-auto-update.sh — thin delegator to ai-brain-auto-update.py, the
# canonical cross-platform implementation (macOS / Linux / Windows).
#
# Why this file survives: settings.json entries wired before the .py existed
# call `bash .../ai-brain-auto-update.sh`. Deleting it would break every such
# install until its next manual re-install — the exact stale-deploy class the
# updater exists to prevent. The installer rewires the entry to the .py form
# on its next run; until then, this keeps the old wiring functional.
#
# Contract preserved: prints ONE hook JSON object, always exits 0.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
python3 "$HERE/ai-brain-auto-update.py" "$@" 2>/dev/null \
  || printf '{"continue":true,"suppressOutput":true}\n'
exit 0
