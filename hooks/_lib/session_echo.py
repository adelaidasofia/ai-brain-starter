"""Per-session echo suppression for ~/.claude/hooks.

WHY THIS EXISTS
---------------
Measured 2026-08-15 across 64 sessions of real transcripts: 153,000 characters
per session of hook output were BYTE-IDENTICAL repetition inside a single
session. The worst single case, `[CONTEXT.md auto-load]`, injected the same
8.4 KB block up to 55 times in one session — 88% of its volume was re-echo.

That is not a rendering problem and it must not be fixed by narrowing triggers.
The trigger is right: the model SHOULD get canonical terminology. The bug is
that an injector re-states bytes the context already holds. Re-injecting an
identical payload adds zero information and costs its full size every time.

WHAT THIS DOES / DOES NOT DO
----------------------------
DOES     suppress a payload whose bytes are identical to one this session
         already emitted under the same key.
DOES NOT suppress anything that CHANGED. A new or edited payload always
         emits — that is the whole point: speak when something is different,
         stay quiet when repeating yourself.
DOES NOT touch trigger conditions. Coverage is unchanged: every fact still
         reaches the model, exactly once, the first time it is true.

FAIL-OPEN BY CONSTRUCTION
-------------------------
Every error path returns True (emit). A broken dedup must degrade to today's
behaviour (noisy) and never to silence — losing a guard's message is a far
worse failure than repeating it. Codified: "Fail loud, never silent no-op".

CONCURRENCY
-----------
State is keyed by session_id, so parallel sessions never share a file. Writes
are atomic (tmp + os.replace). Per call this is one small read + one small
write — bounded work, no corpus walk, so it stays safe under the many-
concurrent-sessions workload ("Per-Session Work That Fans Out Under
Concurrency Is Bounded By Construction").
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / ".session-echo"
TTL_SECONDS = 3 * 86400
BYPASS_ENV = "SESSION_ECHO_BYPASS"


def _state_path(session_id: str) -> Path:
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in "-_")[:80]
    return STATE_DIR / f"{safe or 'unknown'}.json"


def _prune(now: float) -> None:
    """Drop state files older than the TTL. Bounded: the dir holds one small
    file per recent session, and this runs only when a session's file is new."""
    try:
        for p in STATE_DIR.glob("*.json"):
            try:
                if now - p.stat().st_mtime > TTL_SECONDS:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def should_emit(key: str, payload: str, session_id: str | None) -> bool:
    """True if this exact payload has NOT already been emitted this session.

    key         stable identifier for the emitter (e.g. "context-md").
    payload     the exact text that would be emitted.
    session_id  from the hook's stdin JSON. Falsy -> always emit (fail-open).
    """
    if os.environ.get(BYPASS_ENV) == "1":
        return True
    if not session_id or not payload:
        return True
    try:
        digest = hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()[:16]
        path = _state_path(session_id)
        fresh = not path.exists()
        seen = {}
        if not fresh:
            try:
                seen = json.loads(path.read_text(encoding="utf-8")) or {}
                if not isinstance(seen, dict):
                    seen = {}
            except (OSError, ValueError):
                seen = {}
        if seen.get(key) == digest:
            return False
        seen[key] = digest
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(seen), encoding="utf-8")
        os.replace(tmp, path)
        if fresh:
            _prune(time.time())
        return True
    except Exception:
        # Any failure at all: emit. Never trade coverage for quiet.
        return True
