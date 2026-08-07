#!/usr/bin/env python3
"""
undo-last-close.py — rollback the most recent session close in this worktree.

Reverts the writes from the most recent cascade for the current worktree:
  - Moves the session file to ⚙️ Meta/Sessions/Archive/.undone-{timestamp}/
  - Moves any decision files created in the same minute to a parallel folder
  - Reverts the git commit (if it was a session: commit and is HEAD)
  - Re-runs aggregators to refresh Last Session.md / Decision Log.md

Asks for confirmation before each destructive action. The script never
deletes content — everything is moved to an .undone-* archive that the user
can restore manually.

Usage:
  python3 undo-last-close.py                   # interactive
  python3 undo-last-close.py --yes             # skip confirmations
  python3 undo-last-close.py --dry-run         # show what would happen
  python3 undo-last-close.py --worktree NAME   # specify worktree

Exits 0 always.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir as _find_meta_dir_helper  # noqa: E402


def find_meta_dir(vault: Path) -> Path:
    return _find_meta_dir_helper(vault) or (vault / "Meta")


def derive_worktree() -> str:
    cwd = os.getcwd()
    m = re.search(r"/\.claude/worktrees/([^/]+)", cwd)
    if m:
        return m.group(1)
    git_file = Path(cwd) / ".git"
    if git_file.is_file():
        try:
            text = git_file.read_text(encoding="utf-8")
            m2 = re.search(r"worktrees/([^/\s]+)", text)
            if m2:
                return m2.group(1).strip()
        except OSError:
            pass
    return "main"


def confirm(prompt: str, default_yes: bool, force_yes: bool) -> bool:
    if force_yes:
        return True
    suffix = " [Y/n]" if default_yes else " [y/N]"
    try:
        ans = input(prompt + suffix + " ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not ans:
        return default_yes
    return ans in ("y", "yes")


def find_last_session_file(sessions_dir: Path, worktree: str) -> Path | None:
    """Find the most recently modified session file matching this worktree."""
    if not sessions_dir.is_dir():
        return None
    candidates = []
    for path in sessions_dir.glob(f"*-{worktree}.md"):
        if path.is_file():
            candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def find_decisions_in_window(decisions_dir: Path, anchor: datetime, window_min: int = 10) -> list[Path]:
    """Find decision files created within `window_min` of the anchor time."""
    if not decisions_dir.is_dir():
        return []
    out = []
    for path in decisions_dir.glob("*.md"):
        if not path.is_file():
            continue
        delta = abs(path.stat().st_mtime - anchor.timestamp())
        if delta <= window_min * 60:
            out.append(path)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="skip confirmations")
    ap.add_argument("--dry-run", action="store_true", help="show actions only")
    ap.add_argument("--worktree", help="override worktree name")
    args = ap.parse_args()

    vault = Path(os.environ.get("VAULT_ROOT", os.getcwd()))
    meta = find_meta_dir(vault)
    sessions_dir = meta / "Sessions"
    decisions_dir = meta / "Decisions"
    archive_dir = sessions_dir / "Archive"
    worktree = args.worktree or derive_worktree()

    session_file = find_last_session_file(sessions_dir, worktree)
    if not session_file:
        print(f"No session file found for worktree={worktree} in {sessions_dir}")
        return 0

    print(f"Most recent session for worktree={worktree}:")
    print(f"  {session_file}")
    print(f"  modified {datetime.fromtimestamp(session_file.stat().st_mtime).isoformat()}")

    anchor = datetime.fromtimestamp(session_file.stat().st_mtime)
    decisions = find_decisions_in_window(decisions_dir, anchor)
    if decisions:
        print(f"\nDecision files in same window:")
        for d in decisions:
            print(f"  {d}")
    else:
        print("\nNo decision files in the same window.")

    if not confirm("\nMove these to .undone archive?", default_yes=True, force_yes=args.yes):
        print("Aborted.")
        return 0

    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    target_dir = archive_dir / f".undone-{stamp}"
    if args.dry_run:
        print(f"DRY RUN: would create {target_dir}")
        print(f"DRY RUN: would move {session_file} → {target_dir / session_file.name}")
        for d in decisions:
            print(f"DRY RUN: would move {d} → {target_dir / d.name}")
    else:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(session_file), str(target_dir / session_file.name))
        for d in decisions:
            shutil.move(str(d), str(target_dir / d.name))
        print(f"\nMoved to {target_dir}")

    # Optional: revert git commit if it's HEAD and looks like a session: commit
    if (vault / ".git").exists() or subprocess.call(
        ["git", "-C", str(vault), "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ) == 0:
        head_msg = subprocess.run(
            ["git", "-C", str(vault), "log", "-1", "--pretty=%s"],
            capture_output=True, text=True,
        ).stdout.strip()
        if head_msg.startswith("session: "):
            print(f"\nGit HEAD is: {head_msg}")
            if confirm("Revert this commit (soft reset, files stay archived)?",
                       default_yes=True, force_yes=args.yes):
                if args.dry_run:
                    print("DRY RUN: would run git reset --soft HEAD~1")
                else:
                    subprocess.call(
                        ["git", "-C", str(vault), "reset", "--soft", "HEAD~1"]
                    )
                    print("Reverted.")

    # Re-run aggregators
    agg_sessions = meta / "scripts" / "aggregate-sessions.py"
    agg_decisions = meta / "scripts" / "aggregate-decisions.py"
    if not args.dry_run:
        env = {**os.environ, "VAULT_ROOT": str(vault)}
        if agg_sessions.is_file():
            subprocess.call([sys.executable, str(agg_sessions)],
                            env=env, stdout=subprocess.DEVNULL)
        if agg_decisions.is_file():
            subprocess.call([sys.executable, str(agg_decisions)],
                            env=env, stdout=subprocess.DEVNULL)
        print("Aggregators refreshed.")

    print("\nUndo complete. To fully delete the archive folder later:")
    print(f"  rm -rf '{target_dir}'")
    print("To restore the files instead:")
    print(f"  mv '{target_dir}'/*.md '{sessions_dir}/'  # and matching files into Decisions/")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
