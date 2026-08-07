#!/usr/bin/env python3
"""
recover-last-close.py — recover from a partially-completed session close.

If a previous close left a partial-flag at ~/.claude/.cascade-partial-*.json
(e.g., session body empty + Haiku fallback unavailable), this script retries
the fallback now that ANTHROPIC_API_KEY is available, OR opens the session
file in the user's editor for manual completion.

Usage:
  python3 recover-last-close.py             # auto-pick most recent partial
  python3 recover-last-close.py --list      # list all partial flags
  python3 recover-last-close.py --session-id <id>
  python3 recover-last-close.py --manual    # open session file in $EDITOR

Always exits 0 (graceful).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def list_partials() -> list[dict]:
    flag_dir = Path.home() / ".claude"
    if not flag_dir.is_dir():
        return []
    out = []
    for path in sorted(flag_dir.glob(".cascade-partial-*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["_flag_path"] = str(path)
            out.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return out


def find_transcript_for_session(session_id: str) -> Path | None:
    """Best-effort: search ~/.claude/projects/*/sessions/ for the matching transcript."""
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return None
    for project in projects.iterdir():
        if not project.is_dir():
            continue
        for sessions_dir in project.glob("**/sessions"):
            if not sessions_dir.is_dir():
                continue
            for transcript in sessions_dir.glob(f"*{session_id}*"):
                if transcript.is_file():
                    return transcript
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list partial flags")
    ap.add_argument("--session-id", help="recover specific session")
    ap.add_argument("--manual", action="store_true",
                    help="open the session file in $EDITOR for manual completion")
    args = ap.parse_args()

    partials = list_partials()
    if args.list:
        if not partials:
            print("No partial-close flags found.")
            return 0
        for p in partials:
            print(f"  session_id={p.get('session_id')}")
            print(f"  session_file={p.get('session_file')}")
            print(f"  reason={p.get('reason')}")
            print()
        return 0

    if not partials:
        print("No partial-close flags to recover. Nothing to do.")
        return 0

    target = None
    if args.session_id:
        for p in partials:
            if p.get("session_id") == args.session_id:
                target = p
                break
        if target is None:
            print(f"No partial flag for session_id={args.session_id}")
            return 0
    else:
        target = partials[0]

    session_file = Path(target.get("session_file") or "")
    flag_path = Path(target.get("_flag_path") or "")

    if args.manual:
        editor = os.environ.get("EDITOR", "vim")
        if not session_file.is_file():
            print(f"Session file does not exist: {session_file}")
            return 0
        try:
            subprocess.call([editor, str(session_file)])
            print(f"\nIf the file is now filled in, remove the flag with:")
            print(f"  rm '{flag_path}'")
        except Exception as e:
            print(f"Failed to open editor: {e}")
        return 0

    # Auto-recover: re-run the fallback with current env (which presumably has the API key)
    transcript = find_transcript_for_session(target.get("session_id", ""))
    if not transcript:
        print(f"Could not locate transcript for session_id={target.get('session_id')}")
        print(f"Try --manual to fill in {session_file} by hand.")
        return 0

    fallback = Path(__file__).parent / "session-close-fallback.py"
    if not fallback.is_file():
        print(f"Fallback script missing: {fallback}")
        return 0

    print(f"Re-running fallback for session_id={target.get('session_id')}")
    print(f"  session_file={session_file}")
    print(f"  transcript={transcript}")
    rc = subprocess.call([
        sys.executable, str(fallback),
        "--session-id", target.get("session_id", ""),
        "--transcript-path", str(transcript),
    ])
    if rc == 0:
        print("Fallback complete. Verify the session file content and remove the flag if good:")
        print(f"  rm '{flag_path}'")
    else:
        print(f"Fallback exited with code {rc}.")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
