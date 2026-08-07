#!/usr/bin/env python3
"""Snapshot worktree pending work to main vault before cleanup hooks run.

Stop hook. MUST run FIRST in the Stop chain — earlier than any hook that
makes the worktree LOOK clean (e.g. autoprep/reconcile hooks that stage
or prune worktree-local copies of files already preserved at main).

Once a worktree looks clean to Claude Code's archive flow, the harness
silently archives the session worktree on the next session boundary.
This hook is the recovery side of that contract: silence is fine if and
only if real work is preserved first.

Mechanism: when inside a vault worktree with pending changes that
DIVERGE from main vault (i.e., not byte-identical), copies them to
<main-vault>/⚙️ Meta/Worktree Snapshots/<worktree-slug>/<relpath>. Files
byte-identical to main are skipped — cleanup hooks handle those safely.

The snapshot directory survives worktree deletion because it lives at
the main vault path, not under .claude/worktrees/. Surface them at
SessionStart via surface-orphan-worktree-snapshots.py (sibling hook).
Prune them weekly via scripts/worktree-prune.sh once the retention
window expires.

Idempotent. Same file snapshotted multiple Stops just overwrites. No-op
outside worktrees.

Bypass: SNAPSHOT_PENDING_BYPASS=1.

CONFIG (per user, once):
1. Wire into ~/.claude/settings.json Stop hooks. Place it FIRST so it
   runs before any autoprep/reconcile/auto-commit hook that might make
   the worktree look clean:
     "Stop": [
       {"hooks": [{
         "type": "command",
         "command": "python3 ~/.claude/hooks/snapshot-pending-work-on-stop.py 2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'"
       }]}
     ]
2. Pair with surface-orphan-worktree-snapshots.py at SessionStart.
3. Pair with scripts/worktree-prune.sh weekly to auto-prune expired
   snapshots (configurable via SNAPSHOT_RETENTION_DAYS).

Vault root resolution order:
  1. CLAUDE_PROJECT_DIR env var (set by Claude Code)
  2. Walk up from cwd until we find .claude/worktrees/<slug>/
  3. Bail (no-op) if neither resolves
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SNAPSHOT_REL = "⚙️ Meta/Worktree Snapshots"
LOG_REL = "⚙️ Meta/logs/snapshot-pending.log"


def find_main_vault(cwd: Path) -> Path | None:
    """Resolve the main vault root: env var first, then walk-up from cwd.

    Returns None if neither path can locate a vault.
    """
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        candidate = Path(env_root)
        if candidate.is_dir():
            return candidate

    parts = cwd.parts
    try:
        idx = parts.index(".claude")
    except ValueError:
        return None
    if len(parts) <= idx + 1 or parts[idx + 1] != "worktrees":
        return None
    return Path(*parts[:idx])


def _hash(path: Path) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _log(log_path: Path, slug: str, snapped: list[str], skipped_identical: int) -> None:
    if not snapped:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(
                f"[{ts}] {slug}: snapshot {len(snapped)} file(s) "
                f"(skipped {skipped_identical} byte-identical-to-main)\n"
            )
            for p in snapped[:30]:
                f.write(f"  + {p}\n")
            if len(snapped) > 30:
                f.write(f"  ...and {len(snapped) - 30} more\n")
    except OSError:
        pass


def main() -> int:
    if os.environ.get("SNAPSHOT_PENDING_BYPASS") == "1":
        return 0

    cwd = Path.cwd().resolve()
    cwd_str = str(cwd).replace("\\", "/")  # marker must match Windows paths too
    if "/.claude/worktrees/" not in cwd_str:
        return 0

    slug = cwd_str.split("/.claude/worktrees/", 1)[1].split("/", 1)[0]
    if not slug:
        return 0

    vault_root = find_main_vault(cwd)
    if vault_root is None:
        return 0

    snapshot_dir = vault_root / SNAPSHOT_REL / slug
    log_path = vault_root / LOG_REL

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-z"],
            cwd=cwd,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 0

    if result.returncode != 0:
        return 0

    raw = result.stdout
    if not raw:
        return 0

    entries = [e for e in raw.split(b"\x00") if e]

    snapped: list[str] = []
    skipped_identical = 0

    for entry in entries:
        if len(entry) < 4:
            continue
        try:
            relpath = entry[3:].decode("utf-8", errors="replace")
        except Exception:
            continue
        if not relpath:
            continue

        wt_file = cwd / relpath
        if not wt_file.is_file():
            continue

        main_file = vault_root / relpath
        wt_hash = _hash(wt_file)
        main_hash = _hash(main_file) if main_file.is_file() else None

        if wt_hash is None:
            continue
        if wt_hash == main_hash:
            skipped_identical += 1
            continue

        snap_target = snapshot_dir / relpath
        try:
            snap_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(wt_file, snap_target)
            snapped.append(relpath)
        except OSError:
            continue

    _log(log_path, slug, snapped, skipped_identical)
    return 0


if __name__ == "__main__":
    sys.exit(main())
