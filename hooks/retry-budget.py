#!/usr/bin/env python3
"""
PreToolUse Bash hook: retry budget.

Blocks the 4th invocation of an identical Bash command within 30 minutes.
Claude loops on failing commands and burns context; this hook forces an
escape at 3 attempts so the blocker gets surfaced to the user.

Bypass: prefix command with RETRY_BUDGET_BYPASS=1 when intentionally
re-running (polling a cron, expected retries, iteration on a fix where
each attempt is a real change).

State: /tmp/claude-retry-budget-{session_id}.json keyed by md5(norm_cmd).
Window: 30 min rolling. Commands <15 chars exempt (ls, pwd, date, etc.).

Pattern inspired by Devin 2.0 ("ask user for help if CI does not pass
after the third attempt") and Cursor 2.0 ("don't loop more than 3 times
to fix linter errors").
"""
import json
import sys
import os
import time
import hashlib
import glob
import tempfile

THRESHOLD_BLOCK = 4       # 4th+ attempt blocks (3 attempts allowed)
WINDOW_SEC = 30 * 60
MIN_CMD_LEN = 15
STATE_DIR = tempfile.gettempdir()  # /tmp is POSIX-only; Windows has no /tmp
STATE_TTL_SEC = 24 * 3600


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if data.get("tool_name", "") != "Bash":
        sys.exit(0)

    command = (data.get("tool_input", {}) or {}).get("command", "") or ""
    if len(command.strip()) < MIN_CMD_LEN:
        sys.exit(0)

    if "RETRY_BUDGET_BYPASS=1" in command:
        sys.exit(0)

    session_id = (
        data.get("session_id")
        or os.environ.get("CLAUDE_SESSION_ID")
        or "unknown"
    )
    state_path = os.path.join(
        STATE_DIR, f"claude-retry-budget-{session_id}.json"
    )

    norm = " ".join(command.split())[:400]
    cmd_hash = hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]
    now = time.time()

    try:
        with open(state_path) as f:
            state = json.load(f)
    except Exception:
        state = {}

    history = [t for t in state.get(cmd_hash, []) if now - t < WINDOW_SEC]
    history.append(now)
    state[cmd_hash] = history

    try:
        with open(state_path, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

    # Best-effort cleanup of sibling state files older than 24h
    try:
        for path in glob.glob(
            os.path.join(STATE_DIR, "claude-retry-budget-*.json")
        ):
            try:
                if now - os.path.getmtime(path) > STATE_TTL_SEC:
                    os.remove(path)
            except Exception:
                pass
    except Exception:
        pass

    count = len(history)
    if count >= THRESHOLD_BLOCK:
        preview = norm[:80] + ("\u2026" if len(norm) > 80 else "")
        print(
            "BLOCKED by retry-budget hook:\n"
            f"  This command has run {count} times in the last 30 minutes:\n"
            f"    {preview}\n"
            "  Stop looping. Surface to the user: what failed on previous "
            "attempts, and what do they want to do?\n"
            "  Bypass (if you genuinely need to re-run): prefix with "
            "RETRY_BUDGET_BYPASS=1.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
