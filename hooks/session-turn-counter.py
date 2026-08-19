#!/usr/bin/env python3
"""Session turn counter — Stop hook.

Per CLAUDE.md / efficiency.md Rule 55: long sessions degrade quality (Microsoft
DELEGATE-52: ~25% corruption over 20 edits, amplified 5x by document size). The
60-exchange force-close threshold is quality-preserving, not just cost-preserving.

Counts assistant turns per session_id. State at /tmp/claude-turn-count-{session_id}.json.

  Turn 50  -> warn via systemMessage
  Turn 60+ -> hard-flag, recommend handoff file at ⚙️ Meta/Handoffs/<slug>.md
  Otherwise silent.

Bypass: TURN_COUNTER_BYPASS=1 in env.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    if os.environ.get("TURN_COUNTER_BYPASS") == "1":
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        # Malformed input — fail open, don't block the session.
        return 0

    session_id = payload.get("session_id") or "unknown"
    state_path = Path(f"/tmp/claude-turn-count-{session_id}.json")

    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            state = {"turns": 0, "session_start": time.time()}
    else:
        state = {"turns": 0, "session_start": time.time()}

    state["turns"] = int(state.get("turns", 0)) + 1
    try:
        state_path.write_text(json.dumps(state))
    except OSError:
        # /tmp write failure — fail open.
        pass

    turns = state["turns"]
    output: dict = {}

    if turns >= 60:
        output["systemMessage"] = (
            f"[Rule 55] Session at turn {turns} — past force-close threshold (60). "
            f"Quality degrades in long-tail per Microsoft DELEGATE-52 "
            f"(~25% corruption over 20 edits, 5x amplified by doc size). "
            f"Token-usage audit 2026-05-10: ~$12,366 of last 30d burn was the "
            f"past-turn-60 tail. Wrap cleanly: write a handoff at "
            f"⚙️ Meta/Handoffs/<slug>.md with consumes_when: frontmatter, run "
            f"session-close, defer remaining work to a fresh session. "
            f"Per CLAUDE.md: NEVER reopen a closed session as addendum."
        )
    elif turns == 50:
        output["systemMessage"] = (
            f"[Rule 55] Session at turn {turns}/60. Approaching force-close. "
            f"If work is unfinished, structure a handoff at "
            f"⚙️ Meta/Handoffs/<slug>.md now while context is clean."
        )

    if output:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    # A cp1252 Windows console raises UnicodeEncodeError the moment this prints a
    # non-ASCII character, and the hook dies with no legible cause
    # (ai-brain-starter#313). Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
