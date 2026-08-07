#!/bin/bash
# PostToolUse hook — fires after every Write tool call
# Auto-triggers meeting-todos extraction when a meeting note is saved
#
# Folder matching is i18n-aware: by default we match "Meeting Notes" and
# "Meeting-Notes". To match other languages or custom folder names
# (Reuniones, Réunions, 会议笔记, Notas de Reunión, etc.) set
#   AI_BRAIN_MEETING_NOTES_DIR="Meeting Notes:Reuniones:Réunions"
# in your shell init (Phase 11 of /setup-brain writes this for you).
# Colon-separated; case-insensitive; substring match, no regex escaping.

# --- ai-brain-starter: shim-safe PATH (strip refuse-shims) ----------------
# Some machines carry a python3/python PATH shim (e.g. trailofbits
# modern-python) that exit-1s on bare invocation and would turn every bare
# python call below into a silent no-op. Drop any */hooks/shims dir from PATH
# so bare python calls here (and, via export, in children) hit a real python.
if [ "${PATH#*/hooks/shims}" != "$PATH" ]; then
  _abs_new=""; _abs_oifs=$IFS; IFS=:
  for _abs_d in $PATH; do
    case $_abs_d in */hooks/shims|*/hooks/shims/) ;; *) _abs_new=${_abs_new:+$_abs_new:}$_abs_d ;; esac
  done
  IFS=$_abs_oifs; PATH=$_abs_new; export PATH
  unset _abs_new _abs_d _abs_oifs
fi
# --------------------------------------------------------------------------

INPUT=$(cat)

# Extract file_path from the Write tool input
FILE_PATH=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    path = d.get('tool_input', {}).get('file_path', '')
    print(path)
except:
    print('')
" 2>/dev/null)

# Default folders if env var unset. Both EN variants — the original
# write-hook.sh hardcoded these two and we preserve them as the floor.
DEFAULT_FOLDERS="Meeting Notes:Meeting-Notes"
FOLDERS="${AI_BRAIN_MEETING_NOTES_DIR:-$DEFAULT_FOLDERS}"

# Case-insensitive substring check. `tr` ASCII-lowercases the lookup side;
# multibyte characters in non-EN folder names pass through unchanged (so
# `Réunions/` stays `réunions/` and matches a path containing `Réunions/`
# after that path is also lowered). Fixed-string match — no regex needed.
LOWER_PATH=$(echo "$FILE_PATH" | tr '[:upper:]' '[:lower:]')
MATCHED=0
IFS=':' read -ra FOLDER_ARRAY <<< "$FOLDERS"
for folder in "${FOLDER_ARRAY[@]}"; do
  # Strip any trailing slash the user may have included.
  folder="${folder%/}"
  [[ -z "$folder" ]] && continue
  LOWER_FOLDER=$(echo "$folder" | tr '[:upper:]' '[:lower:]')
  if echo "$LOWER_PATH" | grep -qF "${LOWER_FOLDER}/"; then
    MATCHED=1
    break
  fi
done

if [[ $MATCHED -eq 1 ]]; then
  BASENAME=$(basename "$FILE_PATH" .md)
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"Meeting note saved: '$BASENAME'. Run /meeting-todos on this file now -- extract action items, show the user a preview, and add confirmed tasks to the to-do file. Do this automatically without waiting to be asked.\"}}"
else
  echo "{}"
fi
