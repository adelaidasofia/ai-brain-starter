#!/usr/bin/env bash
#
# Integration test: block-skip-prefix-in-vault-write.py
#
# The guard blocks a life-record vault write that still carries a literal
# `__SKIP` line -- content the user told the assistant NOT to persist. A
# persisted line cannot be un-persisted (it is in the file, in git history, and
# in any index that reads the vault), so this fails closed.
#
# EVERY assertion has a negative control. A guard that blocks everything and a
# guard that blocks the right thing produce the same PASS on a block-only
# suite; the allow cases are what tell them apart.
#
# Pure string-level: the hook matches path strings from the payload, so no real
# vault, files, or git are needed -- fast and hermetic.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
HOOK="$REPO_ROOT/hooks/block-skip-prefix-in-vault-write.py"
PY="${PYTHON:-python3}"

JOURNAL="/vault/Journals/May 2026/2026-05-28.md"
COACHING="/vault/Home/Coaching Sessions/2026-05-28 - topic.md"
OUT_OF_SCOPE="/vault/Notes/grocery-list.md"

pass=0

# run <payload-json> [env assignments...] -> hook exit code (0 allow / 2 block)
run() {
  local payload="$1"; shift
  if [ "$#" -gt 0 ]; then
    printf '%s' "$payload" | env "$@" "$PY" "$HOOK" >/dev/null 2>&1 && echo 0 || echo $?
  else
    printf '%s' "$payload" | env -u SKIP_PREFIX_BYPASS -u SKIP_PREFIX_EXTRA_PATHS \
      "$PY" "$HOOK" >/dev/null 2>&1 && echo 0 || echo $?
  fi
}

# payload <tool> <file_path-or-command> <content>  (JSON-safe via python)
payload() {
  "$PY" - "$1" "$2" "$3" <<'PYEOF'
import json, sys
tool, target, content = sys.argv[1], sys.argv[2], sys.argv[3]
if tool == "Bash":
    ti = {"command": content}
elif tool == "Write":
    ti = {"file_path": target, "content": content}
elif tool == "Edit":
    ti = {"file_path": target, "new_string": content}
elif tool == "EditOld":
    tool, ti = "Edit", {"file_path": target, "old_string": content, "new_string": "clean"}
elif tool == "MultiEdit":
    tool, ti = "MultiEdit", {"file_path": target, "edits": [{"new_string": "clean"},
                                                            {"new_string": content}]}
print(json.dumps({"tool_name": tool, "tool_input": ti}))
PYEOF
}

assert() { # <label> <got> <want>
  if [ "$2" = "$3" ]; then echo "PASS  $1"; pass=$((pass+1))
  else echo "FAIL  $1 (got $2, want $3)"; exit 1; fi
}

# --- 1. the core case, with its negative control -----------------------------
assert "NEGATIVE CONTROL: clean journal write is allowed" \
  "$(run "$(payload Write "$JOURNAL" 'Today was good.')")" 0

assert "journal write carrying __SKIP is BLOCKED" \
  "$(run "$(payload Write "$JOURNAL" 'Today was good.
__SKIP the private bit
More text.')")" 2

# --- 2. tab-separated token (a trailing-SPACE-only match would leak this) ----
assert "__SKIP followed by a TAB is BLOCKED" \
  "$(run "$(payload Write "$JOURNAL" "$(printf 'ok\n__SKIP\tprivate\n')")")" 2

assert "bare __SKIP at end of line is BLOCKED" \
  "$(run "$(payload Write "$JOURNAL" 'ok
__SKIP')")" 2

# --- 3. over-match control: a real word starting with the token -------------
assert "NEGATIVE CONTROL: __SKIPPED is not the token, allowed" \
  "$(run "$(payload Write "$JOURNAL" 'ok
__SKIPPED the gym today')")" 0

assert "NEGATIVE CONTROL: mid-line __SKIP is not a line prefix, allowed" \
  "$(run "$(payload Write "$JOURNAL" 'I wrote __SKIP in my notes')")" 0

# --- 4. scope: out-of-scope paths are none of the guard's business -----------
assert "NEGATIVE CONTROL: __SKIP outside a life-record path is allowed" \
  "$(run "$(payload Write "$OUT_OF_SCOPE" '__SKIP buy milk')")" 0

assert "coaching path is in scope and BLOCKS" \
  "$(run "$(payload Write "$COACHING" '__SKIP private')")" 2

# --- 5. Edit: removing the marker must never be blocked ---------------------
assert "NEGATIVE CONTROL: __SKIP in old_string (assistant REMOVING it) allowed" \
  "$(run "$(payload EditOld "$JOURNAL" '__SKIP private')")" 0

assert "__SKIP in new_string is BLOCKED" \
  "$(run "$(payload Edit "$JOURNAL" '__SKIP private')")" 2

assert "MultiEdit: __SKIP in any edits[] new_string is BLOCKED" \
  "$(run "$(payload MultiEdit "$JOURNAL" '__SKIP private')")" 2

# --- 6. Bash write path ------------------------------------------------------
assert "NEGATIVE CONTROL: bash READ of a journal path is allowed" \
  "$(run "$(payload Bash "" "cat '$JOURNAL'")")" 0

assert "bash redirect writing __SKIP into a journal is BLOCKED" \
  "$(run "$(payload Bash "" "cat > '$JOURNAL' <<EOF
__SKIP private
EOF")")" 2

# --- 7. bypasses -------------------------------------------------------------
assert "session-env bypass allows" \
  "$(run "$(payload Write "$JOURNAL" '__SKIP private')" SKIP_PREFIX_BYPASS=1)" 0

assert "inline bypass prefix on the Bash path allows" \
  "$(run "$(payload Bash "" "SKIP_PREFIX_BYPASS=1 cat > '$JOURNAL' <<EOF
__SKIP private
EOF")")" 0

assert "NEGATIVE CONTROL: a QUOTED bypass token is not a real bypass" \
  "$(run "$(payload Bash "" "echo 'SKIP_PREFIX_BYPASS=1' > '$JOURNAL'; cat > '$JOURNAL' <<EOF
__SKIP private
EOF")")" 2

# --- 8. self-referential docs may quote the token ----------------------------
assert "NEGATIVE CONTROL: a SKILL.md quoting __SKIP is allowed" \
  "$(run "$(payload Write "/vault/Journals/May 2026/SKILL.md" '__SKIP example')")" 0

# --- 9. user-extensible coverage --------------------------------------------
CUSTOM="/vault/Diario/2026-05-28.md"
assert "NEGATIVE CONTROL: custom path is out of scope by default" \
  "$(run "$(payload Write "$CUSTOM" '__SKIP privado')")" 0

assert "SKIP_PREFIX_EXTRA_PATHS extends coverage to a custom path" \
  "$(run "$(payload Write "$CUSTOM" '__SKIP privado')" SKIP_PREFIX_EXTRA_PATHS='Diario/')" 2

assert "an unparseable EXTRA_PATHS entry does not crash or disable defaults" \
  "$(run "$(payload Write "$JOURNAL" '__SKIP private')" SKIP_PREFIX_EXTRA_PATHS='Diario/:[unclosed')" 2

echo
echo "ALL PASS ($pass assertions, negative control on every claim)"
