#!/usr/bin/env python3
"""hook_runner.py — portable failure-masking wrapper for Claude Code hooks.

WHY THIS EXISTS: hooks.json masks hook failures with POSIX shell forms like

    python3 <script> 2>/dev/null || echo '{"continue":true,"suppressOutput":true}'
    [ -f <script> ] && python3 <script> || true

On native Windows, Claude Code executes hook commands under PowerShell 5.1,
PowerShell 7, cmd.exe, or Git Bash depending on version and configuration —
and NO single shell one-liner parses in all four (`||` is a parse error in
PowerShell 5.1, `[ -f ]` and `/dev/null` are POSIX-only, a quoted first token
is a string literal in PowerShell). The result before this wrapper existed:
every hook errored visibly on every prompt for Windows users.

The only command shape all four shells parse identically is a bare PATH
command followed by quoted arguments. So on Windows the installer
(install-hooks-user-level.py) wires every hook as:

    py -3 "<abs>/scripts/hook_runner.py" --fallback silent "<abs>/hooks/<hook>.py"

and this wrapper reproduces the POSIX masking semantics in Python:

  - target script missing        -> print fallback JSON, exit 0 (the [ -f ] guard)
  - target exits 0               -> forward its stdout verbatim, exit 0
  - target exits 2 (a BLOCK)     -> forward stderr, exit 2 (blocking semantics
                                    preserved — exit 2 is Claude Code's
                                    intentional block signal, never masked)
  - target exits anything else,
    crashes, or can't launch     -> print fallback JSON, exit 0 (the 2>/dev/null
                                    + || echo masking)

Fallback forms (--fallback):
  silent  {"continue": true, "suppressOutput": true}          (default)
  allow   {"hookSpecificOutput": {"hookEventName": "PreToolUse",
           "permissionDecision": "allow"}}   — for PreToolUse guards that must
           fail open rather than wedge every tool call.

stdin (the hook payload JSON) is passed through to the target unchanged.
Stdlib-only; usable on macOS/Linux too, though POSIX installs keep their
original shell forms to avoid churning working configs.
"""

from __future__ import annotations

import os
import subprocess
import sys

FALLBACKS = {
    "silent": '{"continue":true,"suppressOutput":true}',
    "allow": ('{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
              '"permissionDecision":"allow"}}'),
}


def main(argv: list[str]) -> int:
    args = list(argv)
    fallback = FALLBACKS["silent"]
    if len(args) >= 2 and args[0] == "--fallback":
        fallback = FALLBACKS.get(args[1], FALLBACKS["silent"])
        args = args[2:]
    if not args:
        print(fallback)
        return 0
    script, extra = args[0], args[1:]

    # Missing target = the [ -f ] guard: silent fallback, exit 0. Checked here
    # explicitly because CPython exits 2 for "can't open file", which would
    # otherwise masquerade as an intentional block below.
    if not os.path.isfile(script):
        print(fallback)
        return 0

    try:
        payload = sys.stdin.read()
    except Exception:
        payload = ""

    try:
        proc = subprocess.run(
            [sys.executable, script, *extra],
            input=payload, capture_output=True, text=True,
        )
    except Exception:
        print(fallback)
        return 0

    if proc.returncode == 0:
        # Forward exactly — an empty stdout is a valid no-op for Claude Code.
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        return 0
    if proc.returncode == 2:
        # Intentional block: stderr carries the reason; must propagate.
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        return 2
    print(fallback)
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
