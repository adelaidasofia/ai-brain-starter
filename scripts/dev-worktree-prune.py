#!/usr/bin/env python3
"""dev-worktree-prune — reclaim orphaned <dev-root>/<repo>-<slug> worktrees (MYC-587/677).

`claude-dev-worktree start` creates a sibling worktree per session; `cleanup`
removes it. Nothing runs cleanup for a session that ended without it — crashed,
context-exhausted, or simply closed — so the siblings accumulate. A 2026-06-07
fleet probe found 60+; they cost disk, and every fleet probe counts each one as
a repo. The vault's own `worktree-prune.sh` deliberately excludes them ("never
removed"), so nothing swept them at all.

What makes this safe: removing a worktree does NOT delete its branch or its
commits — those live in the shared object store. The ONLY thing a `git worktree
remove` can destroy is uncommitted working-tree state. So the classifier turns
on exactly that, plus whether the branch's work is preserved somewhere:

  REAP     : idle, branch merged-or-gone, working tree CLEAN.
  SNAPSHOT : idle, branch merged-or-gone, working tree DIRTY → copy the
             divergence out FIRST (git diff + untracked files), then remove.
             An abandoned session may hold real work; a blind remove is the
             session-loss class this exists to prevent.
  SURFACE  : the branch carries commits that are on NO remote (genuine
             un-backed-up work), a live .session-lock, or anything unclassifiable.
             NEVER auto-removed — surfaced for a human.

DRY-RUN BY DEFAULT. `--apply` performs the removals and writes every snapshot
path to the report, so each is recoverable.

Usage:
  dev-worktree-prune.py                    # dry-run over the whole dev root
  dev-worktree-prune.py --apply
  dev-worktree-prune.py --idle-days 14     # only worktrees untouched this long
  dev-worktree-prune.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _add_lib_to_path() -> bool:
    candidates = [
        os.environ.get("DEV_REPO_SCAN_DIR"),
        # This script's own sibling first: a ~/.claude/hooks copy can be an older
        # deploy, and a stale classifier here decides what gets DELETED.
        str(Path(__file__).resolve().parent.parent / "hooks"),
        str(Path.home() / ".claude" / "skills" / "ai-brain-starter" / "hooks"),
        str(Path.home() / ".claude" / "hooks"),
    ]
    for c in candidates:
        if c and (Path(c) / "_lib" / "dev_repo_scan.py").exists():
            sys.path.insert(0, c)
            return True
    return False


if not _add_lib_to_path():
    print("dev-worktree-prune: could not locate _lib/dev_repo_scan.py "
          "(set DEV_REPO_SCAN_DIR).", file=sys.stderr)
    sys.exit(2)

try:
    from _lib.dev_repo_scan import (  # noqa: E402
        SESSION_LOCK_LIVE_WINDOW_SEC,
        resolve_dev_root,
    )
except ImportError as exc:
    print(f"dev-worktree-prune: the _lib/dev_repo_scan.py on sys.path is older "
          f"than this script ({exc}). Re-run scripts/install-hooks-user-level.py. "
          f"Refusing to delete anything on a classifier I cannot trust.",
          file=sys.stderr)
    sys.exit(2)

DEFAULT_IDLE_DAYS = 7.0
SNAPSHOT_ROOT = Path.home() / ".claude" / "logs" / "dev-worktree-snapshots"

# Verdicts
REAP = "reap"
SNAPSHOT_REAP = "snapshot-then-reap"
SURFACE = "surface"


def _git(cwd, *args, timeout: int = 20):
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return 99, "", str(exc)


def is_linked_worktree(path: Path) -> bool:
    """A LINKED worktree's `.git` is a pointer FILE; a primary checkout's is a
    real directory. This discriminator is what keeps a primary repo — someone's
    actual clone that merely has a dash in its name — out of the candidate set."""
    marker = path / ".git"
    try:
        return marker.is_file()
    except OSError:
        return False


def main_checkout_of(worktree: Path):
    """The primary checkout owning this worktree, or None."""
    rc, common, _ = _git(worktree, "rev-parse", "--git-common-dir")
    if rc != 0 or not common:
        return None
    if not os.path.isabs(common):
        common = os.path.normpath(os.path.join(worktree, common))
    p = Path(common)
    return p.parent if p.name == ".git" else None


def has_live_session_lock(path: Path, now_ts: float) -> bool:
    """A session actively working in this worktree. Removing it out from under a
    live run is how a 45-minute gate loses its files mid-flight."""
    for cand in (path / ".session-lock", path / ".claude" / ".session-lock"):
        try:
            if cand.exists() and (now_ts - cand.stat().st_mtime) < SESSION_LOCK_LIVE_WINDOW_SEC:
                return True
        except OSError:
            continue
    return False


def dirty_paths(worktree: Path) -> list:
    """Uncommitted divergence: modified/staged tracked files + untracked files
    git would not ignore. `--porcelain` so parsing does not depend on locale."""
    rc, out, _ = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    if rc != 0:
        return []
    paths = []
    for line in out.splitlines():
        if len(line) > 3:
            paths.append(line[3:].strip().strip('"'))
    return paths


def branch_of(worktree: Path):
    rc, out, _ = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or not out or out == "HEAD":
        return None
    return out


def unpushed_commits(worktree: Path, branch: str) -> int:
    """Commits on `branch` reachable from no remote. >0 means the work exists
    ONLY here, so the worktree's branch is not safely disposable."""
    rc, out, _ = _git(worktree, "log", branch, "--not", "--remotes", "--format=%H")
    if rc != 0:
        return -1  # unknown → treat as unsafe by the caller
    return len([x for x in out.splitlines() if x.strip()])


def idle_days(path: Path, now_ts: float) -> float:
    """Days since the worktree was last touched. Uses the newest of the dir's own
    mtime and its .git pointer's — a dir mtime alone misses in-file edits."""
    newest = 0.0
    for p in (path, path / ".git"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    if newest == 0.0:
        return 0.0
    return max(0.0, (now_ts - newest) / 86400)


def classify(worktree: Path, idle_floor: float, now_ts: float) -> dict:
    """One worktree → {verdict, reason, ...}. Fail-safe: anything unclear SURFACES."""
    v = {"path": str(worktree), "verdict": SURFACE, "reason": "", "branch": None,
         "dirty": 0, "idle_days": round(idle_days(worktree, now_ts), 1)}

    if has_live_session_lock(worktree, now_ts):
        v["reason"] = "a session is live in this worktree"
        return v
    if v["idle_days"] < idle_floor:
        v["reason"] = f"touched {v['idle_days']}d ago (floor {idle_floor}d)"
        return v

    main = main_checkout_of(worktree)
    if main is None or not main.exists():
        v["reason"] = ("git no longer resolves its main checkout — dangling "
                       "metadata, keeping (never reason about what you can't read)")
        return v

    branch = branch_of(worktree)
    v["branch"] = branch

    if branch is not None:
        n = unpushed_commits(worktree, branch)
        if n < 0:
            v["reason"] = "could not determine whether its commits are backed up"
            return v
        if n > 0:
            v["reason"] = (f"`{branch}` carries {n} commit(s) on no remote — "
                           f"un-backed-up work, push or drop it deliberately")
            return v
        # Merged-or-gone check: with zero un-backed-up commits the branch content
        # is already on a remote, which is the property that matters. A branch
        # that vanished upstream lands here too (nothing left to preserve).

    dirty = dirty_paths(worktree)
    v["dirty"] = len(dirty)
    if dirty:
        v["verdict"] = SNAPSHOT_REAP
        v["reason"] = (f"{len(dirty)} uncommitted path(s) — snapshot them out "
                       f"first, then remove")
        return v

    v["verdict"] = REAP
    v["reason"] = "clean, idle, and its commits are on a remote"
    return v


def snapshot(worktree: Path, paths: list, apply: bool):
    """Copy the uncommitted divergence somewhere durable. Returns the dest path
    (or a would-be path in dry-run). An abandoned session's unsaved work is the
    ONLY thing a worktree removal can destroy, so this runs BEFORE any removal."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = SNAPSHOT_ROOT / f"{worktree.name}-{stamp}"
    if not apply:
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    # The patch covers tracked modifications; the file copies cover untracked
    # ones (which no diff can reconstruct).
    rc, diff, _ = _git(worktree, "diff", "HEAD")
    if rc == 0 and diff:
        (dest / "uncommitted.patch").write_text(diff + "\n", encoding="utf-8")
    for rel in paths:
        src = worktree / rel
        try:
            if not src.is_file():
                continue
            out = dest / "files" / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
        except OSError:
            continue
    (dest / "ORIGIN.txt").write_text(
        f"worktree: {worktree}\nsnapshotted: {stamp}\npaths: {len(paths)}\n",
        encoding="utf-8")
    return dest


def remove(worktree: Path, apply: bool) -> tuple:
    """`git worktree remove` via the OWNING checkout, never `rm -rf`, so git's
    own bookkeeping stays consistent. --force only after a snapshot exists."""
    if not apply:
        return True, "dry-run"
    main = main_checkout_of(worktree)
    if main is None:
        return False, "no main checkout"
    rc, _, err = _git(main, "worktree", "remove", "--force", str(worktree), timeout=60)
    if rc != 0:
        return False, err[:200]
    _git(main, "worktree", "prune")
    return True, "removed"


def find_candidates(dev_root: Path) -> list:
    try:
        children = sorted(p for p in dev_root.iterdir() if p.is_dir())
    except OSError:
        return []
    return [c for c in children if is_linked_worktree(c)]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reclaim orphaned <dev-root>/<repo>-<slug> worktrees safely.")
    ap.add_argument("--apply", action="store_true",
                    help="perform the removals (default: dry-run)")
    ap.add_argument("--idle-days", type=float, default=DEFAULT_IDLE_DAYS,
                    help=f"only worktrees untouched this long (default {DEFAULT_IDLE_DAYS})")
    ap.add_argument("--dev-root", help="override the dev root for this run")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    dev_root = Path(args.dev_root).expanduser() if args.dev_root else resolve_dev_root()
    now_ts = time.time()
    verdicts = [classify(w, args.idle_days, now_ts) for w in find_candidates(dev_root)]

    reaped, snapped, failed = [], [], []
    for v in verdicts:
        if v["verdict"] not in (REAP, SNAPSHOT_REAP):
            continue
        wt = Path(v["path"])
        if v["verdict"] == SNAPSHOT_REAP:
            dest = snapshot(wt, dirty_paths(wt), args.apply)
            v["snapshot"] = str(dest)
            snapped.append(v)
        ok, note = remove(wt, args.apply)
        v["removed"] = ok
        v["note"] = note
        (reaped if ok else failed).append(v)

    surfaced = [v for v in verdicts if v["verdict"] == SURFACE]

    if args.json:
        print(json.dumps({"applied": args.apply, "dev_root": str(dev_root),
                          "verdicts": verdicts,
                          "totals": {"scanned": len(verdicts), "reaped": len(reaped),
                                     "snapshotted": len(snapped),
                                     "surfaced": len(surfaced), "failed": len(failed)}},
                         indent=2))
        return 0

    out = [f"dev-worktree-prune {'APPLY' if args.apply else 'DRY-RUN'} — "
           f"{len(verdicts)} sibling worktree(s) under {dev_root}",
           f"REAP: {len(reaped)} ({len(snapped)} snapshotted first)  |  "
           f"KEEP (human decision): {len(surfaced)}"]
    for v in reaped:
        extra = f"  [snapshot -> {v['snapshot']}]" if v.get("snapshot") else ""
        out.append(f"  REAP     {Path(v['path']).name}  ({v['reason']}){extra}")
    for v in failed:
        out.append(f"  FAILED   {Path(v['path']).name}: {v['note']}")
    for v in surfaced:
        out.append(f"  KEEP     {Path(v['path']).name}  — {v['reason']}")
    if not args.apply and reaped:
        out.append("\nRe-run with --apply to remove them (dirty ones are "
                   "snapshotted to ~/.claude/logs/dev-worktree-snapshots first).")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): these CLIs print em dashes and warning
    # glyphs, and an unguarded non-ASCII print raises UnicodeEncodeError there --
    # the caller then reads the empty output as "nothing to report", which on a
    # drift/reap surface means "clean fleet". Force UTF-8 so it cannot happen.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
