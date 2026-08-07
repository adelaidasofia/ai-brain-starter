#!/usr/bin/env python3
"""
PreToolUse blocker for Write/Edit on Claude Code config files.

Fires on Write|Edit. If tool_input.file_path is a Claude Code config file
(settings.json, settings.local.json, .mcp.json) AND the projected new content
contains a BLOCK-severity issue (duplicate top-level key, invalid JSON), exit 2
with stderr explanation. Claude Code treats exit 2 as deny.

Why: warn-after-the-fact (FileChanged) means the bad config has already shipped
to the next process that reads it. Block at the write boundary — the cost of a
false positive on a config file is low; the cost of a silently-corrupt config
is hours of "why isn't my permission working?" debugging.

The lint logic itself lives in lint-claude-settings.py (same dir).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

LINT_SCRIPT = Path(__file__).parent / "lint-claude-settings.py"

CONFIG_BASENAMES = {"settings.json", "settings.local.json", ".mcp.json"}


def _is_config_path(path: str) -> bool:
    p = Path(path)
    if p.name not in CONFIG_BASENAMES:
        return False
    parts = p.parts
    return ".claude" in parts or p.name == ".mcp.json"


def _project_edit(file_path: str, old: str, new: str, replace_all: bool) -> str | None:
    p = Path(file_path)
    if not p.exists():
        return None
    try:
        current = p.read_text()
    except OSError:
        return None
    if old not in current:
        return None
    if replace_all:
        return current.replace(old, new)
    return current.replace(old, new, 1)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool = payload.get("tool_name", "")
    if tool not in ("Write", "Edit"):
        return 0

    inp = payload.get("tool_input") or {}
    file_path = inp.get("file_path", "")
    if not _is_config_path(file_path):
        return 0

    if tool == "Write":
        projected = inp.get("content", "")
    else:
        old = inp.get("old_string", "")
        new = inp.get("new_string", "")
        replace_all = bool(inp.get("replace_all", False))
        projected = _project_edit(file_path, old, new, replace_all)
        if projected is None:
            return 0

    label = f"<pretooluse:{Path(file_path).name}>"
    result = subprocess.run(
        # sys.executable, not "python3": Windows installs have no python3 on PATH.
        [sys.executable or "python3", str(LINT_SCRIPT),
         "--strict", "--content", projected, "--label", label],
        capture_output=True,
        text=True,
    )
    if result.returncode == 2:
        print(
            f"BLOCKED by pre-write-settings-lint: {file_path}\n"
            f"{result.stderr.rstrip()}\n"
            "This write would silently corrupt your Claude Code config. "
            "Fix the issue above (most often: duplicate top-level key) and retry.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
