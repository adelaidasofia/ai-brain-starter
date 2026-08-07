#!/usr/bin/env bash
# Cross-platform HOME sandboxing for the integration suite (MYC-3536).
#
# WHY THIS EXISTS
#
# Many tests here redirect HOME to a tmpdir so the developer's real ~/.claude is
# never touched. Their comments say so explicitly ("HOME redirected so the marker
# file never touches the real ~/.claude"). On POSIX, `HOME=...` delivers that.
#
# On Windows it does not. Python resolves Path.home() and expanduser("~") through
# ntpath.expanduser, which reads USERPROFILE and ignores HOME completely:
#
#     $ HOME=C:\sandbox python -c "from pathlib import Path; print(Path.home())"
#     C:\Users\<you>          # <-- the REAL home, not the sandbox
#
# So on Git Bash / Windows every such test ran against the real ~/.claude. Live
# consequence on 2026-07-30: the suite rewrote the developer's real
# ~/.claude/settings.json, pointing 95 of 111 hook entries at hook_runner.py
# inside the throwaway git worktree the tests had run from. When those worktrees
# were deleted the launcher could not open the runner, CPython exited 2 — which
# is Claude Code's intentional-BLOCK signal — and every tool call in every later
# session was denied. Same fail-closed class as #375 and #409.
#
# A second trap: env var values are NOT path-translated by MSYS when they reach a
# native Python. Setting USERPROFILE=/tmp/tmp.XXXX makes Windows Python resolve
# C:\tmp\tmp.XXXX — not Git Bash's /tmp. So the sandbox path must go through
# cygpath. That is the main reason this is a shared helper and not an inline
# `USERPROFILE=` on every call site.
#
# USAGE
#
#   . "$(dirname "${BASH_SOURCE[0]}")/lib/sandbox_home.sh"
#
#   # (a) sandbox the whole test process:
#   TMP="$(mktemp -d)"; sandbox_home "$TMP"
#
#   # (b) sandbox one child process, leaving the caller's env alone:
#   run_sandboxed "$TMP" python3 "$HOOK"
#   run_sandboxed "$TMP" env VAULT_ROOT="$v" python3 "$HOOK"
#   run_sandboxed "$TMP" env -u VAULT_ROOT python3 "$HOOK"
#
# Both forms set HOME and USERPROFILE, and neutralise the HOMEDRIVE/HOMEPATH
# pair that ntpath.expanduser falls back to when USERPROFILE is absent, so there
# is no route left from "~" to the real profile.

# Path in the form a native (non-MSYS) interpreter needs. No-op off Windows.
_sandbox_native_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    printf '%s' "$1"
  fi
}

# sandbox_home DIR — export a sandboxed home for the rest of this shell.
sandbox_home() {
  local d="${1:-}"
  if [ -z "$d" ]; then
    echo "sandbox_home: a directory argument is required" >&2
    return 2
  fi
  mkdir -p "$d/.claude"
  export HOME="$d"
  USERPROFILE="$(_sandbox_native_path "$d")"
  export USERPROFILE
  # Belt and braces: with USERPROFILE always set these are never consulted, but
  # leaving the real ones in the environment would make any future fallback path
  # resolve straight back to the real profile.
  export HOMEDRIVE=""
  export HOMEPATH=""
}

# run_sandboxed DIR CMD [ARGS...] — run one command under a sandboxed home.
#
# Deliberately creates nothing: callers pass a dir they already built, and a
# negative control that asserts "~/.claude is absent" must not be handed an
# empty ~/.claude by its own test harness.
run_sandboxed() {
  local d="${1:-}"
  if [ -z "$d" ]; then
    echo "run_sandboxed: a directory argument is required" >&2
    return 2
  fi
  shift
  env HOME="$d" \
      USERPROFILE="$(_sandbox_native_path "$d")" \
      HOMEDRIVE="" HOMEPATH="" \
      "$@"
}
