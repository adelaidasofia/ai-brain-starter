#!/usr/bin/env python3
"""Surface LaunchAgents that run at normal priority — SessionStart (macOS only).

A background maintenance job left at normal scheduling priority competes with
WindowServer for CPU and disk. ONE is harmless. A FLEET is not: past roughly a
dozen jobs the UI starves and the machine presents as FROZEN — while memory and
disk stay healthy, so it reads as a crash and misdirects the diagnosis at RAM.

Measured incident (2026-08-19): two hard freezes on a 10-core host, load 43.78
then 109, with 2GB RAM free and swap flat. 68 user LaunchAgents, 16 firing at
least hourly and one every 60s, none declaring itself background. Load — not
memory — was the whole story.

The fix is one key. ProcessType=Background gives nice 10 + throttled CPU;
LowPriorityIO keeps disk reads behind anything interactive. This hook is the
DETECT side: it never edits a plist, it names the ones missing the declaration.

Fail-open by construction: non-macOS, missing dir, or any read error exits 0
with no output. A surfacing hook must never block a session.

Config:
  UNNICED_AGENTS_MIN_COUNT  min offenders before surfacing (default 3)
  UNNICED_AGENTS_CAP_HOURS  hours between reports (default 24)
Bypass: UNNICED_LAUNCHAGENTS_BYPASS=1
"""
from __future__ import annotations

import json
import os
import plistlib
import sys
import time
from pathlib import Path

AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
STAMP = Path.home() / ".claude" / ".unniced-launchagents.stamp"
DEFAULT_MIN_COUNT = 3
DEFAULT_CAP_HOURS = 24.0

# A job is considered priority-declared when ANY of these is present. Nice is
# the explicit form; ProcessType=Background implies nice 10 + throttled I/O.
# LowPriorityIO alone only covers disk, so it does NOT count on its own.
_BACKGROUND_TYPES = {"Background", "Adaptive"}


def is_priority_declared(plist: dict) -> bool:
    """Pure decision: does this parsed plist declare a non-competing priority?

    True when the job carries an explicit Nice, or a ProcessType that launchd
    treats as non-interactive. LowPriorityIO alone is NOT sufficient — it only
    de-prioritises disk, leaving the job competing for CPU with the UI.
    """
    if not isinstance(plist, dict):
        return False
    if isinstance(plist.get("Nice"), int):
        return True
    return plist.get("ProcessType") in _BACKGROUND_TYPES


def _emit(msg: str | None) -> None:
    if msg:
        print(json.dumps({"systemMessage": msg}))
    else:
        print(json.dumps({"continue": True, "suppressOutput": True}))
    sys.exit(0)


def _capped() -> bool:
    """True when a report was already emitted inside the cap window."""
    try:
        hours = float(os.environ.get("UNNICED_AGENTS_CAP_HOURS", DEFAULT_CAP_HOURS))
    except ValueError:
        hours = DEFAULT_CAP_HOURS
    try:
        return (time.time() - STAMP.stat().st_mtime) < hours * 3600
    except OSError:
        return False


def main() -> None:
    if os.environ.get("UNNICED_LAUNCHAGENTS_BYPASS"):
        _emit(None)
    if sys.platform != "darwin" or not AGENTS_DIR.is_dir():
        _emit(None)
    if _capped():
        _emit(None)

    try:
        min_count = int(os.environ.get("UNNICED_AGENTS_MIN_COUNT", DEFAULT_MIN_COUNT))
    except ValueError:
        min_count = DEFAULT_MIN_COUNT

    offenders: list[str] = []
    total = 0
    for p in sorted(AGENTS_DIR.glob("*.plist")):
        try:
            # Builtin open(), not p.open(): binary mode, and check-utf8-file-io
            # reads the mode from args[1], so a Path.open("rb") reads to it as
            # text mode and trips a false positive. Same bytes either way.
            with open(p, "rb") as fh:
                data = plistlib.load(fh)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue  # unreadable/corrupt plist is not this hook's business
        total += 1
        if not is_priority_declared(data):
            offenders.append(p.stem)

    if len(offenders) < min_count:
        _emit(None)

    shown = ", ".join(offenders[:5])
    more = f" (+{len(offenders) - 5} more)" if len(offenders) > 5 else ""
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.touch()
    except OSError:
        pass

    _emit(
        f"[launchd-priority] {len(offenders)} of {total} LaunchAgents run at normal "
        f"priority: {shown}{more}. Background jobs at normal priority compete with "
        "the UI for CPU and disk; past ~a dozen the machine can starve WindowServer "
        "and present as FROZEN while RAM and disk look fine. Fix: add "
        "<key>ProcessType</key><string>Background</string> + "
        "<key>LowPriorityIO</key><true/> to each plist, then reload it. Reload jobs "
        "with RunAtLoad=true one at a time — reloading them together fires them all "
        f"at once. Bypass: UNNICED_LAUNCHAGENTS_BYPASS=1"
    )


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
    try:
        main()
    except Exception:  # fail-open: a surfacing hook never blocks a session
        _emit(None)
