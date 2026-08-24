#!/usr/bin/env python3
"""Surface snapshots from worktrees that no longer exist.

SessionStart hook. Pairs with snapshot-pending-work-on-stop.py — that
hook copies divergent worktree files to
<vault>/⚙️ Meta/Worktree Snapshots/<slug>/<relpath> on every Stop. When
Claude Code archives the worktree (silently, between sessions), the
snapshot stays. This hook tells the user what's recoverable.

Reports ONLY orphaned snapshots — i.e., snapshot dirs whose
corresponding <vault>/.claude/worktrees/<slug>/ no longer exists.
Snapshots for live worktrees are ignored (the worktree itself has the
files).

For each orphan, the output includes:
  - file count
  - snapshot age in days
  - days remaining before scripts/worktree-prune.sh auto-prunes it
  - top file paths inside the snapshot (so the user can decide
    whether the snapshot has anything they care about)

Output goes to stdout as additionalContext so the assistant can surface
it at SessionStart. Silent when no orphans.

Bypass: ORPHAN_SNAPSHOTS_BYPASS=1.

CONFIG (per user, once):
1. Wire into ~/.claude/settings.json SessionStart hooks:
     "SessionStart": [
       {"hooks": [{
         "type": "command",
         "command": "python3 ~/.claude/hooks/surface-orphan-worktree-snapshots.py 2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'"
       }]}
     ]
2. Pair with snapshot-pending-work-on-stop.py at Stop.
3. Pair with scripts/worktree-prune.sh weekly to auto-prune expired
   snapshots (configurable via SNAPSHOT_RETENTION_DAYS).

Vault root resolution order:
  1. CLAUDE_PROJECT_DIR env var (set by Claude Code)
  2. cwd itself if it contains .claude/worktrees/
  3. Walk up from cwd until we find .claude/worktrees/
  4. Bail (no-op) if none resolves
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

SNAPSHOT_REL = "⚙️ Meta/Worktree Snapshots"
WORKTREES_REL = ".claude/worktrees"
DEFAULT_RETENTION_DAYS = 30
TOP_FILES_PER_ORPHAN = 3
SECONDS_PER_DAY = 86400


def find_vault_root(cwd: Path) -> Path | None:
    """Resolve the main vault root.

    1. CLAUDE_PROJECT_DIR env var if set + valid.
    2. cwd itself if it contains .claude/worktrees/.
    3. Walk up cwd looking for the parent that contains .claude/worktrees/.
    """
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        candidate = Path(env_root)
        if (candidate / WORKTREES_REL).is_dir() or candidate.is_dir():
            return candidate

    if (cwd / WORKTREES_REL).is_dir():
        return cwd

    for parent in cwd.parents:
        if (parent / WORKTREES_REL).is_dir():
            return parent

    return None


def _list_files(path: Path) -> list[Path]:
    files: list[Path] = []
    try:
        # sessionstart-walk-bounded: rglob covers ONE orphaned worktree-snapshot
        # dir (machinery sized by worktree count x divergent files), never a data
        # corpus; main() caps output at 10 orphans. Not the cold-start freeze class.
        for entry in path.rglob("*"):
            if entry.is_file():
                files.append(entry)
    except OSError:
        pass
    return files


def _age_days(path: Path) -> int:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0
    return max(0, int((time.time() - mtime) / SECONDS_PER_DAY))


def main() -> int:
    if os.environ.get("ORPHAN_SNAPSHOTS_BYPASS") == "1":
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    cwd = Path.cwd().resolve()
    vault_root = find_vault_root(cwd)
    if vault_root is None:
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    snapshot_root = vault_root / SNAPSHOT_REL
    worktrees_root = vault_root / WORKTREES_REL

    if not snapshot_root.is_dir():
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    try:
        retention_days = int(os.environ.get("SNAPSHOT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    except ValueError:
        retention_days = DEFAULT_RETENTION_DAYS

    orphans = []
    for snap_dir in sorted(snapshot_root.iterdir()):
        if not snap_dir.is_dir():
            continue
        slug = snap_dir.name
        wt_path = worktrees_root / slug
        if wt_path.exists():
            continue  # worktree still live; not an orphan
        files = _list_files(snap_dir)
        if not files:
            continue
        age = _age_days(snap_dir)
        orphans.append((slug, files, snap_dir, age))

    if not orphans:
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    lines = [
        "[orphan-worktree-snapshots] " +
        f"{len(orphans)} archived worktree(s) have snapshotted files you may want to recover:",
        "",
    ]
    for slug, files, snap_dir, age in orphans[:10]:
        days_left = max(0, retention_days - age)
        prune_clause = (
            f"auto-prune in {days_left}d" if days_left > 0 else "auto-prune at next weekly run"
        )
        lines.append(
            f"- `{slug}` — {len(files)} file(s), snapshotted {age}d ago ({prune_clause})"
        )
        top = sorted(files, key=lambda p: str(p))[:TOP_FILES_PER_ORPHAN]
        for f in top:
            try:
                rel = f.relative_to(snap_dir)
            except ValueError:
                rel = f.name
            lines.append(f"    • {rel}")
        if len(files) > TOP_FILES_PER_ORPHAN:
            lines.append(f"    • ... and {len(files) - TOP_FILES_PER_ORPHAN} more")
        lines.append(f"    at `{snap_dir}`")
    if len(orphans) > 10:
        lines.append(f"...and {len(orphans) - 10} more")
    lines.extend([
        "",
        "Recover: copy needed files back to the main vault, or delete the",
        "snapshot dir if obsolete. Auto-prune runs weekly via",
        f"scripts/worktree-prune.sh. Path: `{snapshot_root}/`",
    ])

    body = "\n".join(lines)
    print(json.dumps({
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": body,
        },
    }))
    return 0


if __name__ == "__main__":
    # This CLI carries non-ASCII source and writes to the console, so on a
    # Windows cp1252 codepage the write itself would raise and the warning
    # would be LOST -- the one message that must never go quiet.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
