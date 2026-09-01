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
# exit-contract: ADVISORY -- dry-run by default and its KEEP rows are a
#   human-decision queue about un-backed-up worktrees

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
    from _lib.dev_repo_scan import (  # noqa: E402
        has_live_session_lock as _shared_has_live_session_lock,
    )
except ImportError as exc:
    print(f"dev-worktree-prune: the _lib/dev_repo_scan.py on sys.path is older "
          f"than this script ({exc}). Re-run scripts/install-hooks-user-level.py. "
          f"Refusing to delete anything on a classifier I cannot trust.",
          file=sys.stderr)
    sys.exit(2)

# Fail-CLOSED process-liveness gate, resolved defensively so an older deployed
# _lib makes this tool REFUSE removals rather than fail to import and quietly
# revert to the unguarded path.
try:
    from _lib.worktree_safety import process_busy_reason as _process_busy_reason
except Exception:  # pragma: no cover - depends on the deployed layout
    _process_busy_reason = None  # type: ignore[assignment]


def _busy(path: Path, cache: dict | None = None) -> str | None:
    """Why `path` must not be removed, or None. An absent probe REFUSES."""
    if _process_busy_reason is None:
        return ("process-liveness helper unavailable in this _lib deploy; "
                "refusing every removal")
    return _process_busy_reason(path, cache=cache)

DEFAULT_IDLE_DAYS = 7.0
# Overridable so a test never writes into the operator's real snapshot store —
# a test fixture sitting in the recovery directory is indistinguishable from a
# real rescued worktree exactly when someone is looking for one.
SNAPSHOT_ROOT = Path(
    os.environ.get("DEV_WORKTREE_SNAPSHOT_ROOT")
    or (Path.home() / ".claude" / "logs" / "dev-worktree-snapshots")
)

# Verdicts
REAP = "reap"
SNAPSHOT_REAP = "snapshot-then-reap"
SURFACE = "surface"


def _git(cwd, *args, timeout: int = 20):
    try:
        # encoding pinned: `text=True` alone decodes with the console code page,
        # and on a non-UTF-8 Windows console a path with any non-ASCII byte
        # raises before this returns — on the tool that decides what to DELETE.
        r = subprocess.run(["git", "-C", str(cwd), *args],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
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
    live run is how a 45-minute gate loses its files mid-flight.

    Delegates to the shared `_lib.has_live_session_lock`. This function used to
    carry its own probe of `<path>/.session-lock` — a filename that, measured
    2026-08-06, exists NOWHERE under the dev root. session-lock.py writes
    `.claude/.session-lock.json` at the git-common-dir root, so a sibling
    worktree's lock is not inside the worktree. The guard was dead in production
    while reading as "no session here", on a tool that deletes whole worktrees.

    An OSError while probing a lock reads as LOCKED, not unlocked. "I could not
    tell" and "nobody is here" are different answers, and only one of them is
    safe to act on when the action is deletion.
    """
    return _shared_has_live_session_lock(path, now_ts)
    return False


def dirty_paths(worktree: Path):
    """(paths, known) — uncommitted divergence, and whether git could tell us.

    `known` is False when `git status` failed (index lock contention, a timeout,
    a broken worktree). That is NOT the same as a clean tree, and collapsing the
    two would let a failed status call read as "nothing to lose" and delete
    someone's uncommitted work. The caller SURFACEs instead.
    `--porcelain` so parsing does not depend on locale.
    """
    rc, out, _ = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    if rc != 0:
        return [], False
    paths = []
    for line in out.splitlines():
        if len(line) > 3:
            paths.append(line[3:].strip().strip('"'))
    return paths, True


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

    if branch is None:
        # Detached HEAD. Skipping the un-backed-up check here would be the worst
        # possible place to skip it: a detached HEAD's commits are reachable from
        # NO ref, so once the worktree is gone only the reflog holds them. Every
        # other verdict in this file survives a mistake because the branch keeps
        # the commits; this one does not.
        v["reason"] = ("detached HEAD — its commits are on no branch, so nothing "
                       "but the reflog would survive the removal")
        return v

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

    dirty, known = dirty_paths(worktree)
    if not known:
        v["reason"] = ("`git status` failed here, so whether it holds uncommitted "
                       "work is UNKNOWN — never the same as clean")
        return v
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
    """Copy the uncommitted divergence somewhere durable.

    Returns (dest, failed_paths). An abandoned session's unsaved work is the ONLY
    thing a worktree removal can destroy, so this runs BEFORE any removal — and a
    non-empty `failed` means the copy is INCOMPLETE and the removal must not
    happen (a partial snapshot plus a removal is still data loss).
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = SNAPSHOT_ROOT / f"{worktree.name}-{stamp}"
    if not apply:
        return dest, []
    dest.mkdir(parents=True, exist_ok=True)
    # The patch covers tracked modifications; the file copies cover untracked
    # ones (which no diff can reconstruct).
    rc, diff, _ = _git(worktree, "diff", "HEAD")
    if rc == 0 and diff:
        (dest / "uncommitted.patch").write_text(diff + "\n", encoding="utf-8")
    failed = []
    for rel in paths:
        src = worktree / rel
        try:
            if not src.is_file():
                continue  # a dir or vanished entry carries no bytes to preserve
            out = dest / "files" / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
        except OSError as exc:
            # Skipping a file we could not copy and then removing the worktree
            # DESTROYS it. Record the failure; the caller refuses the removal.
            failed.append(f"{rel}: {exc}")
    (dest / "ORIGIN.txt").write_text(
        f"worktree: {worktree}\nsnapshotted: {stamp}\npaths: {len(paths)}\n"
        f"failed: {len(failed)}\n" + "".join(f"  {f}\n" for f in failed),
        encoding="utf-8")
    return dest, failed


def remove(worktree: Path, apply: bool, probe_cache: dict | None = None) -> tuple:
    """`git worktree remove` via the OWNING checkout, never `rm -rf`, so git's
    own bookkeeping stays consistent. --force only after a snapshot exists.

    Last gate before the unlink: the PROCESS TABLE. Every other input to the
    classify/remove decision is a proxy that reads a busy tree as an abandoned
    one — the idle-days threshold is mtime, and the session-lock probe depends
    on a writer that stops emitting during one long call. This one observes,
    fails closed, and is taken HERE rather than at classify time, because on a
    shared machine a classification is minutes stale by the time it is acted on.
    """
    if not apply:
        return True, "dry-run"
    busy = _busy(worktree, probe_cache)
    if busy:
        return False, busy
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
    # One process-table reading for the whole apply pass, built at the first
    # removal — deliberately NOT at classify time, which can be minutes earlier.
    probe_cache: dict = {}
    for v in verdicts:
        if v["verdict"] not in (REAP, SNAPSHOT_REAP):
            continue
        wt = Path(v["path"])
        # Ahead of the snapshot: no point walking a tree we are going to keep,
        # and a dry run must report the same refusal an apply would hit.
        busy = _busy(wt, probe_cache)
        if busy:
            v["removed"] = False
            v["note"] = busy
            failed.append(v)
            continue
        if v["verdict"] == SNAPSHOT_REAP:
            paths, known = dirty_paths(wt)
            if not known:
                v["note"] = "git status became unreadable between plan and apply"
                failed.append(v)
                continue
            dest, snap_failed = snapshot(wt, paths, args.apply)
            v["snapshot"] = str(dest)
            if snap_failed:
                # Incomplete snapshot -> refuse. Better a worktree that stays
                # than one removed with its only copy of something half-saved.
                v["note"] = (f"snapshot INCOMPLETE ({len(snap_failed)} file(s) "
                             f"could not be copied) — refusing to remove")
                v["removed"] = False
                failed.append(v)
                continue
            snapped.append(v)
        ok, note = remove(wt, args.apply, probe_cache)
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
