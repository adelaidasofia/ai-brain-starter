#!/usr/bin/env python3
"""Reclaim ABANDONED git lock files. The canonical implementation (MYC-3175).

A git process killed mid-operation strands `.git/index.lock`. From then on every
operation that takes the index lock — `pull`, `merge --ff-only`, `checkout` —
fails with "Unable to create index.lock". Nothing self-heals, so a managed clone
silently stops updating while looking perfectly healthy: a valid repo at a valid
commit. Observed live 2026-07-20: a 0-byte index.lock dated Jul 17 had failed
every pull for 3 days, unnoticed.

ONE implementation, every consumer that fast-forwards a managed clone:
  * scripts/ai-brain-auto-update.py  — the install clone
  * hooks/_lib/dev_repo_scan.py      — the ~/dev hub fleet
A second copy would rot the moment one is fixed; that is the same
deployed!=committed drift class this exists to prevent.

CONSERVATIVE BY CONSTRUCTION. Removing a lock a live git holds corrupts the
repo, so a lock is reclaimed only when BOTH hold:
  * older than ABS_STALE_GIT_LOCK_AGE_SEC (default 3600). No real git operation
    holds an index lock for an hour; a live one clears in seconds.
  * not held by any live process, per lsof. Where lsof is unavailable the age
    test alone decides — and on Windows the unlink itself raises PermissionError
    while a process holds the file, a stronger check than lsof needing no code.

Stdlib only. Never raises: a failure to heal must never break the caller.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

# Locks that block a fast-forward. Others (config.lock, packed-refs.lock) are
# held so briefly that an abandoned one is not a realistic freeze source.
GIT_LOCK_NAMES = ("index.lock", "HEAD.lock", "shallow.lock")
DEFAULT_MIN_AGE_SEC = 3600.0


def git_dir(repo: Path) -> "Path | None":
    """The real .git directory, WITHOUT shelling out to git — which is exactly
    what a stuck lock can break. Handles `.git` as a directory (normal clone)
    and as a `gitdir:` pointer FILE (linked worktree)."""
    dot = Path(repo) / ".git"
    try:
        if dot.is_dir():
            return dot
        if dot.is_file():
            txt = dot.read_text(encoding="utf-8", errors="replace").strip()
            if txt.startswith("gitdir:"):
                p = Path(txt.split(":", 1)[1].strip())
                return p if p.is_absolute() else (Path(repo) / p).resolve()
    except OSError:
        return None
    return None


def lock_is_held(lock: Path) -> "bool | None":
    """True if a LIVE process holds the lock, False if provably not, None if we
    cannot tell (lsof absent). Unknown stays unknown — never downgraded to
    False — so the caller falls back to the age test rather than assuming safe.
    """
    try:
        r = subprocess.run(["lsof", "-t", str(lock)],
                           capture_output=True, text=True, timeout=5)
        return bool(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return None


def reclaim_stale_git_locks(repo: Path) -> list:
    """Remove abandoned git locks in `repo`. Returns the names reclaimed (for
    surfacing), never raises. See module docstring for the safety contract."""
    try:
        min_age = float(os.environ.get("ABS_STALE_GIT_LOCK_AGE_SEC",
                                       str(DEFAULT_MIN_AGE_SEC)))
    except (ValueError, TypeError):
        min_age = DEFAULT_MIN_AGE_SEC
    gd = git_dir(repo)
    if gd is None:
        return []
    reclaimed = []
    for name in GIT_LOCK_NAMES:
        lock = gd / name
        try:
            if not lock.is_file():
                continue
            if (time.time() - lock.stat().st_mtime) <= min_age:
                continue  # recent -> assume a real concurrent git operation
            if lock_is_held(lock) is True:
                continue  # a live process owns it; never stomp it
            lock.unlink()
            reclaimed.append(name)
        except OSError:
            # Windows raises PermissionError while the file is genuinely held.
            # Leaving it alone is the correct, safe outcome.
            continue
    return reclaimed
