#!/usr/bin/env python3
"""Guard: refuse a vault path that resolves inside a consumer cloud-sync root.

A git-backed Obsidian vault placed inside iCloud / OneDrive / Dropbox / Google
Drive / Box melts the OS sync daemon (fileproviderd/bird/cloudd on macOS): the
high-churn `.git/` + per-session worktree checkouts generate millions of file
events the sync client tries to upload, pegging CPU and freezing the machine.
The vault belongs on a real local disk; the index belongs server-side.

This is the SINGLE source of truth for that check — install (phase-01-welcome),
diagnose.sh/.ps1, and the SessionStart footprint signal all route through the
same `detect_cloud_sync()` so there is no drift between surfaces.

Usage:
  check-cloud-sync.py <vault-path>          # human-readable verdict + remedy
  check-cloud-sync.py --porcelain <path>    # one machine-readable line

Exit codes:
  0  OK    — path is on a local disk
  1  RISK  — path resolves inside a consumer cloud-sync root (the freeze class)
  2  USAGE — bad arguments

Porcelain first token: OK_LOCAL | CLOUD_SYNC_RISK:<service>
"""
from __future__ import annotations

import sys
from pathlib import Path

# detect_cloud_sync lives in the shared worktree-safety lib. Resolve it whether
# this script runs from the repo (hooks/_lib) or the deployed skill dir.
# Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # Python 3.7+
    except (AttributeError, ValueError):
        pass
_HERE = Path(__file__).resolve().parent
for _cand in (_HERE.parent / "hooks", _HERE.parent):
    if (_cand / "_lib" / "worktree_safety.py").is_file():
        sys.path.insert(0, str(_cand))
        break
else:
    # deployed layout: ~/.claude/skills/ai-brain-starter/hooks/_lib
    _dep = Path.home() / ".claude" / "skills" / "ai-brain-starter" / "hooks"
    if (_dep / "_lib" / "worktree_safety.py").is_file():
        sys.path.insert(0, str(_dep))

try:
    from _lib.worktree_safety import detect_cloud_sync
except Exception as e:  # pragma: no cover - import-path failure is itself a finding
    print(f"CHECK_UNAVAILABLE: could not import detect_cloud_sync ({e})", file=sys.stderr)
    # Fail OPEN on import error (don't block an install over a path glitch), but
    # say so loudly so it isn't a silent no-op.
    print("OK_LOCAL")
    sys.exit(0)


def _file_count(p: Path) -> int:
    """Cheap-ish file count (capped) so a big existing vault reads louder."""
    n = 0
    try:
        for _root, _dirs, files in __import__("os").walk(p):
            n += len(files)
            if n > 50000:
                return n
    except OSError:
        pass
    return n


def main(argv: list[str]) -> int:
    porcelain = False
    args = [a for a in argv if a != "--porcelain"]
    if "--porcelain" in argv:
        porcelain = True
    if len(args) != 1:
        print("usage: check-cloud-sync.py [--porcelain] <vault-path>", file=sys.stderr)
        return 2

    raw = Path(args[0]).expanduser()
    try:
        path = raw.resolve()
    except OSError:
        path = raw

    service = detect_cloud_sync(path)

    if service:
        if porcelain:
            print(f"CLOUD_SYNC_RISK:{service}")
            return 1
        count = _file_count(path) if path.is_dir() else 0
        size_note = f" It already holds ~{count} files." if count > 0 else ""
        print(f"FAIL  This path is inside {service}, a consumer cloud-sync folder.{size_note}")
        print(f"      A git-backed vault here melts the sync daemon (pegged CPU / frozen machine).")
        print(f"      Move it to a real local path, e.g. ~/Brain or ~/vaults/<name>.")
        print(f"      Already installed there? See docs/CLOUD_SYNC.md to move it out safely.")
        return 1

    if porcelain:
        print("OK_LOCAL")
        return 0
    print(f"OK    {path} is on a local disk (not a consumer cloud-sync root).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
