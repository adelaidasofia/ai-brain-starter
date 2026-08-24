#!/usr/bin/env python3
"""
PreToolUse hook: block calendar MCP calls whose start/end times lack an
explicit timezone offset.

Root cause this prevents: Google Calendar's MCP treats a naive ISO string
(no "Z", no "+/-HH:MM" suffix) as UTC in practice, even when a separate
`time_zone` parameter is passed. Result: an "11 AM user-local-tz" request lands
as 11:00 UTC = 06:00 user-local-tz — silently five hours off.

Rule: every `start` and `end` field sent to cal_create_event or
cal_update_event MUST end in a TZ marker (`Z`, `-05:00`, `-04:00`, etc.).
The hook inspects tool_input and exits 2 (blocking) if the rule is broken.

Bypass (rare): CAL_TZ_BYPASS=1 in the environment.
"""
from __future__ import annotations

import json
import os
import re
import sys

TARGET_TOOLS = {
    "mcp__google-workspace__cal_create_event",
    "mcp__google-workspace__cal_update_event",
}

# Matches ISO datetime ending with Z or ±HH:MM or ±HHMM
TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")

# Date-only strings (all-day events) are fine — allow them through
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def main() -> None:
    if os.environ.get("CAL_TZ_BYPASS") == "1":
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_name = payload.get("tool_name") or ""
    if tool_name not in TARGET_TOOLS:
        sys.exit(0)

    tool_input = payload.get("tool_input") or {}
    problems: list[str] = []

    for field in ("start", "end"):
        value = tool_input.get(field)
        if not value or not isinstance(value, str):
            continue
        if DATE_ONLY_RE.match(value):
            continue
        if not TZ_SUFFIX_RE.search(value):
            problems.append(f"  {field}={value!r}")

    if not problems:
        sys.exit(0)

    sys.stderr.write(
        "CALENDAR TIMEZONE MISSING: start/end must include an explicit offset "
        "(e.g. -05:00 for user-local-tz, -04:00 for NYC/EDT, Z for UTC).\n"
        "The separate time_zone parameter is not enough — naive times get "
        "interpreted as UTC and land hours off.\n"
        "Offending fields:\n"
        + "\n".join(problems)
        + "\n\nFix: rewrite the ISO string as 'YYYY-MM-DDTHH:MM:SS-05:00' "
          "(user-local-tz) or the correct offset for the timezone the user actually "
          "means. Confirm the timezone with the user if ambiguous.\n"
          "See ⚙️ Meta/rules/calendar.md for the full rule.\n"
          "Bypass: CAL_TZ_BYPASS=1 (only if you really know what you're doing).\n"
    )
    sys.exit(2)


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
    main()
