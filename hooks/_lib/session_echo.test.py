#!/usr/bin/env python3
"""Negative control for session_echo.

The suppressor's failure mode is not "too loud" — it is SILENCE ON NEW
INFORMATION. A dedup that also swallows changed payloads reads exactly like a
working one from the terminal (quiet), so the only proof it is safe is a test
that a CHANGED payload still emits, and that every error path emits too.

Run: python3 hooks/_lib/session_echo.test.py
"""
import os
import sys
import tempfile
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import importlib
import _lib.session_echo as se  # noqa: E402

fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


with tempfile.TemporaryDirectory() as td:
    se.STATE_DIR = pathlib.Path(td) / "echo"

    print("POSITIVE: identical payload is suppressed after first emit")
    check("first emit speaks", se.should_emit("k", "PAYLOAD-A", "sess1") is True)
    check("identical re-emit is suppressed",
          se.should_emit("k", "PAYLOAD-A", "sess1") is False)
    check("third identical also suppressed",
          se.should_emit("k", "PAYLOAD-A", "sess1") is False)

    print("NEGATIVE CONTROL: changed payload MUST still speak")
    check("changed payload emits", se.should_emit("k", "PAYLOAD-B", "sess1") is True)
    check("and is then suppressed on repeat",
          se.should_emit("k", "PAYLOAD-B", "sess1") is False)
    check("reverting to the older payload also emits (it changed again)",
          se.should_emit("k", "PAYLOAD-A", "sess1") is True)

    print("ISOLATION: a different session is unaffected")
    check("other session still emits the same payload",
          se.should_emit("k", "PAYLOAD-A", "sess2") is True)
    print("ISOLATION: a different key is unaffected")
    check("other key still emits", se.should_emit("k2", "PAYLOAD-A", "sess1") is True)

    print("FAIL-OPEN: every degraded path emits rather than going silent")
    check("no session_id -> emit", se.should_emit("k", "PAYLOAD-A", None) is True)
    check("empty session_id -> emit", se.should_emit("k", "PAYLOAD-A", "") is True)
    check("empty payload -> emit (caller decides)",
          se.should_emit("k", "", "sess1") is True)

    os.environ["SESSION_ECHO_BYPASS"] = "1"
    check("bypass env forces emit", se.should_emit("k", "PAYLOAD-A", "sess1") is True)
    del os.environ["SESSION_ECHO_BYPASS"]

    print("FAIL-OPEN: unreadable state dir must not silence")
    se.STATE_DIR = pathlib.Path("/proc/nonexistent-cannot-create/echo")
    check("unwritable state -> emit", se.should_emit("k", "PAYLOAD-A", "sess1") is True)

    print("FAIL-OPEN: corrupt state file must not silence")
    se.STATE_DIR = pathlib.Path(td) / "echo2"
    se.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (se.STATE_DIR / "sess3.json").write_text("{not json at all")
    check("corrupt state -> emit", se.should_emit("k", "PAYLOAD-A", "sess3") is True)
    check("and recovers into a valid state",
          se.should_emit("k", "PAYLOAD-A", "sess3") is False)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("all session_echo controls passed")
