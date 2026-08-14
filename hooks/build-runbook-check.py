#!/usr/bin/env python3
"""
PreToolUse guard for Agent calls that look like build/ship/skill/MCP/connector/hook
work but where Build Standards.md (and MCP Build Runbook.md when applicable) was
not read in the recent tool history.

Triggered by /multi-agent-build patterns that historically skipped the runbook
read and incurred MCP Lesson #22 + #23 penalties (parallel agents diverging on
schema, schema collapse on >50-file chunks, no normalize-before-merge step).

Bypass: set `BUILD_RUNBOOK_BYPASS=1` in env.

Wired via PreToolUse matcher: "Agent" in ~/.claude/settings.json or
.claude/settings.local.json. Action: warn (does not block; surfaces a one-line
reminder before the agent dispatches).

Lesson refs:
- ⚙️ Meta/Build Standards.md (pre-build checklist, optimization pass, MiniMax preprocessing, tool stack cross-reference)
- ⚙️ Meta/MCP Build Runbook.md Lessons #22 + #23
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# NO VAULT_ROOT here on purpose (MYC-3529). This hook never touches the vault:
# it matches BUILD_STANDARDS / MCP_RUNBOOK as bare FILENAME substrings against
# `file_path` arguments recorded in the session transcript under
# ~/.claude/projects, so it already works for a Read of either runbook in ANY
# vault. The module used to bind `VAULT_ROOT` from the env var with a ~/vault
# default and then never reference it — a dead read that made the file look
# vault-rooted to every reader and to the MYC-2505 survey, which is why it was
# tagged SEV-A. Deleting it is the whole fix; nothing here needs a vault root.
BUILD_STANDARDS = "Build Standards.md"
MCP_RUNBOOK = "MCP Build Runbook.md"
TRANSCRIPT_DIR = Path.home() / ".claude" / "projects"
RECENT_LOOKBACK = 50  # tool calls
BUILD_KEYWORDS = re.compile(
    r"\b(build|ship|skill|mcp|connector|hook|plugin|new agent|new workflow|new pipeline)\b",
    re.IGNORECASE,
)
MCP_KEYWORDS = re.compile(r"\bmcp\b", re.IGNORECASE)


def find_recent_jsonl() -> Path | None:
    """Find the most recently modified session transcript."""
    if not TRANSCRIPT_DIR.exists():
        return None
    candidates = sorted(
        (
            p
            for p in TRANSCRIPT_DIR.rglob("*.jsonl")
            if p.is_file()
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def runbook_read_recently(transcript_path: Path | None, runbook_filename: str) -> bool:
    """Check if Build Standards.md (or other runbook) was Read in last RECENT_LOOKBACK tool calls."""
    if not transcript_path or not transcript_path.exists():
        return False
    try:
        with transcript_path.open(encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return False
    # Walk backward, count tool calls, check if Read of runbook appears.
    tool_calls_seen = 0
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        # tool_use entries vary in shape across Claude Code versions
        msg = entry.get("message") or entry
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("tool_use", "tool_result"):
                tool_calls_seen += 1
                if item.get("type") == "tool_use" and item.get("name") == "Read":
                    file_path = (item.get("input") or {}).get("file_path") or ""
                    if runbook_filename in file_path:
                        return True
        if tool_calls_seen >= RECENT_LOOKBACK:
            break
    return False


def main() -> int:
    if os.environ.get("BUILD_RUNBOOK_BYPASS") == "1":
        return 0

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0

    tool_input = payload.get("tool_input") or {}
    description = tool_input.get("description", "")
    prompt = tool_input.get("prompt", "")
    text = f"{description}\n{prompt}"

    if not BUILD_KEYWORDS.search(text):
        return 0

    transcript = find_recent_jsonl()
    bs_read = runbook_read_recently(transcript, BUILD_STANDARDS)
    mcp_text = MCP_KEYWORDS.search(text)
    mcp_read = runbook_read_recently(transcript, MCP_RUNBOOK) if mcp_text else True

    missing: list[str] = []
    if not bs_read:
        missing.append(BUILD_STANDARDS)
    if mcp_text and not mcp_read:
        missing.append(MCP_RUNBOOK)
    if not missing:
        return 0

    files_str = " + ".join(f"`{f}`" for f in missing)
    print(
        "WARN by build-runbook-check: this Agent call looks like build/ship/skill/MCP work "
        f"but {files_str} was NOT Read in the last {RECENT_LOOKBACK} tool calls.\n"
        "Per ⚙️ Meta/MCP Build Runbook Lesson #22 (parallel agents skipping the runbook diverge "
        "on schema, lose normalize-before-merge step) and Lesson #23 (multi-agent build without "
        "Build Standards.md hits avoidable failure modes), Read the runbook before dispatch.\n"
        "Bypass: BUILD_RUNBOOK_BYPASS=1.",
        file=sys.stderr,
    )
    # Warn (non-blocking) — exit 0 keeps the call going. Comment out next line to convert to block.
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    # A hook that print()s non-ASCII raises UnicodeEncodeError on a cp1252
    # console: the gate then fails silently OPEN, or denies the tool call with
    # no legible cause. Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
