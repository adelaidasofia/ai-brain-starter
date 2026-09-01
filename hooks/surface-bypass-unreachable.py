#!/usr/bin/env python3
"""surface-bypass-unreachable.py

SessionStart surfacer. Scans the DEPLOYED ~/.claude/hooks directory (the
union every source repo writes into) for the inline-bypass-REACHABILITY
class: a PreToolUse(Bash) gate that reads a `*_BYPASS` env var but never
consults the COMMAND STRING for it. An inline `VAR=1 <cmd>` prefix lives
only in the command, never in the hook process's own (session) env, so a
gate that reads os.environ only is advertising a bypass that cannot fire --
a permanent block with a fake exit, which trains people to disable the
guard some other way instead.

WHY A SEPARATE SessionStart SURFACER, ALONGSIDE THE CI WATCHDOG
-----------------------------------------------------------------
hooks/test_bypass_reachability_watchdog.py (this repo's own CI) can only see
hooks TRACKED IN THIS REPO. Hooks deploy to ~/.claude/hooks from more than
one source repo, and a hand-edited or hand-added hook is untracked anywhere.
No single repo's CI sees that deployed union, so a per-repo watchdog is
structurally blind to a broken hook that shipped from elsewhere, or one that
diverged from its tracked source after install. This surfacer is the
catch-all: it scans what is ACTUALLY on disk and deployed, regardless of
where it came from.

Silent when clean, or when the deployed hooks dir does not exist (a fresh
install, or a machine that has never deployed anything here). Never blocks
-- purely informational, matching every other surface-*.py hook in this
repo. Any internal error falls back to silence rather than breaking
SessionStart.

Override for tests: ABS_DEPLOYED_HOOKS_DIR points the scan at a fixture
directory instead of ~/.claude/hooks.

WIRING (SessionStart):
  "SessionStart": [
    {"hooks": [{
      "type": "command",
      "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/surface-bypass-unreachable.py 2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'"
    }]}
  ]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))

try:
    from _lib.bypass_scan import scan_hooks
except Exception:
    # Fail-open: a missing/broken _lib must never break SessionStart. A
    # scanner that cannot run reports nothing, same as a clean scan --
    # correct for a surfacer, which only ever advises and never blocks.
    scan_hooks = None  # type: ignore[assignment]


def _deployed_hooks_dir() -> Path:
    env = os.environ.get("ABS_DEPLOYED_HOOKS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "hooks"


def _emit(message: str | None = None) -> None:
    """SessionStart additionalContext (reaches the model) or `{}` (silent)."""
    if message:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": message,
            }
        }))
    else:
        print(json.dumps({}))
    sys.exit(0)


def build_message(hooks_dir: Path) -> "str | None":
    """The advisory to emit, or None when there is nothing to say.

    Pure function (no I/O beyond the scan itself) so tests can drive it
    directly against a fixture directory without going through stdin/stdout.
    """
    if scan_hooks is None or not hooks_dir.is_dir():
        return None
    _enforce, violators = scan_hooks(str(hooks_dir))
    if not violators:
        return None
    listed = "\n".join(f"  - {v}" for v in sorted(violators))
    return (
        "[surface-bypass-unreachable] "
        f"{len(violators)} deployed Bash-gate hook(s) advertise a `*_BYPASS` "
        "env var but never consult the COMMAND for it, so the bypass they "
        "print in their own block message can never fire from an inline "
        "`VAR=1 <cmd>` prefix (only from an exported session env var):\n"
        f"{listed}\n"
        "Fix: check both -- "
        '`os.environ.get(VAR) == "1" or inline_bypass(command, VAR)` -- '
        "using hooks/_lib/cmd_env.inline_bypass (ai-brain-starter) or the "
        "equivalent shared helper in the hook's own source repo."
    )


def main() -> None:
    try:
        json.load(sys.stdin)
    except Exception:
        pass
    try:
        _emit(build_message(_deployed_hooks_dir()))
    except Exception:
        # Absolute backstop: an advisory surfacer must never fail loud.
        _emit(None)


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        main()
    except Exception:
        print(json.dumps({}))
        sys.exit(0)
