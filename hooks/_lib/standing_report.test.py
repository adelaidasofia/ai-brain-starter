#!/usr/bin/env python3
"""Negative control for standing_report.

The dangerous failure is not verbosity, it is a CHANGED finding-set rendering
as the quiet digest — that would trade coverage for silence, which is the one
thing this pass must never do. So the controls that matter are: any change
restores the full render, and every degraded path returns full.
"""
import os
import sys
import pathlib
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import _lib.standing_report as sr  # noqa: E402

fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


# Realistic length ON PURPOSE. The digest carries a summary line plus an age
# clause, so a toy 25-char fixture is SHORTER than any digest and the
# never-grow guard correctly hands it back whole — which would silently make
# every compression assertion below vacuous.
_ROWS = "\n".join(f"  - item {i}: some finding with a path and a remedy" for i in range(20))
FULL_A = "[x] 90 items a fresh install will not reproduce. Run the sweep.\n" + _ROWS
FULL_B = "[x] 91 items a fresh install will not reproduce. Run the sweep.\n" + _ROWS + "\n  - item 20: one more"
SUMMARY = "[x] 90 items a fresh install will not reproduce. Run the sweep."

with tempfile.TemporaryDirectory() as td:
    sr.STATE_DIR = pathlib.Path(td) / "sr"

    print("first sight renders IN FULL")
    check("first call returns full", sr.report("k", FULL_A, SUMMARY) == FULL_A)

    print("unchanged set collapses to the digest, carrying count + age")
    r2 = sr.report("k", FULL_A, SUMMARY)
    check("second call is not the full text", r2 != FULL_A)
    check("digest keeps the summary/count", SUMMARY in r2)
    check("digest states it is unchanged", "unchanged" in r2)
    check("digest is much shorter", len(r2) < len(FULL_A) + 60)

    print("NEGATIVE CONTROL: any change restores the FULL render")
    check("changed set returns full", sr.report("k", FULL_B, SUMMARY) == FULL_B)
    check("then collapses again", sr.report("k", FULL_B, SUMMARY) != FULL_B)
    check("reverting is also a change -> full", sr.report("k", FULL_A, SUMMARY) == FULL_A)

    print("the counter names its own unit, and counts SESSIONS when it can")
    sr.report("k", FULL_A, SUMMARY)
    r = sr.report("k", FULL_A, SUMMARY)
    check("no session id -> labelled as fires, not sessions",
          "fire(s)" in r and "session(s)" not in r)
    sr.report("s", FULL_A, SUMMARY, "SESS-1")
    r1 = sr.report("s", FULL_A, SUMMARY, "SESS-1")
    r1b = sr.report("s", FULL_A, SUMMARY, "SESS-1")
    check("3 fires in one session still reads 1 session",
          "1 session(s)" in r1b and "1 session(s)" in r1)
    r2 = sr.report("s", FULL_A, SUMMARY, "SESS-2")
    check("a second session increments to 2", "2 session(s)" in r2)

    print("a digest longer than the text it replaces is never returned")
    SHORT = "[t] 2 things\n- a"
    sr.report("short", SHORT, "[t] 2 things")
    check("short render passes through unchanged",
          sr.report("short", SHORT, "[t] 2 things") == SHORT)

    print("keys are isolated")
    check("other key renders full", sr.report("k2", FULL_A, SUMMARY) == FULL_A)

    print("detail pointer is cited once recorded")
    sr.set_detail_path("k3", "/tmp/detail.txt")
    sr.report("k3", FULL_A, SUMMARY)
    check("digest cites detail path", "/tmp/detail.txt" in sr.report("k3", FULL_A, SUMMARY))

    print("FAIL-OPEN: degraded paths return FULL, never the digest")
    check("empty full passes through", sr.report("k", "", SUMMARY) == "")
    os.environ[sr.BYPASS_ENV] = "1"
    check("bypass returns full", sr.report("k", FULL_A, SUMMARY) == FULL_A)
    del os.environ[sr.BYPASS_ENV]

    sr.STATE_DIR = pathlib.Path("/proc/cannot-create-here/sr")
    check("unwritable state returns full", sr.report("k", FULL_A, SUMMARY) == FULL_A)

    sr.STATE_DIR = pathlib.Path(td) / "sr2"
    sr.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (sr.STATE_DIR / "k.json").write_text("{corrupt")
    check("corrupt state returns full", sr.report("k", FULL_A, SUMMARY) == FULL_A)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("all standing_report controls passed")
