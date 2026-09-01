#!/usr/bin/env python3
"""Remove the just-ended session's scratch worktree — the event-driven fix
for git-worktree pileup.

THE BUG THIS FIXES
------------------
Stock Claude Code creates a fresh worktree under `.claude/worktrees/<slug>/`
per session but never removes it. The weekly prune script only deletes
*branches* whose worktree dir is already gone — nothing removes the DIRECTORY.
So worktrees accumulate, each a full vault checkout, until a cloud-sync daemon
(iCloud/OneDrive/Dropbox) or the disk falls over (observed: 102 worktrees /
1.29M files / ~25GB). Scheduled cleanup of an unbounded resource is the
anti-pattern; this removes each worktree at the moment its session ends.

SAFETY (see _lib/worktree_safety.py for the full guarantee)
-----------------------------------------------------------
  * Only acts on `claude/<slug>` SCRATCH branches — never a deliberate
    feature-branch worktree you created on purpose.
  * Snapshots any genuinely-unsaved content (not in git's object DB) to
    `⚙️ Meta/Worktree Snapshots/<slug>/` first. If ANY such file can't be
    copied, REFUSES to delete (fail safe).
  * `git worktree remove` keeps the branch ref, so committed work survives.
  * Runs LAST in the SessionEnd chain (after reconcile/scrub) so it doesn't
    pull the rug from under sibling hooks.

Bypass: KEEP_WORKTREE_ON_END=1  (keep every worktree — opt back into manual cleanup)

WIRING (SessionEnd, last):
  "SessionEnd": [
    {"hooks": [{
      "type": "command",
      "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/remove-ended-worktree.py 2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'"
    }]}
  ]
Pairs with snapshot-pending-work-on-stop.py (Stop) and enforce-worktree-cap.py
(SessionStart, backstop for sessions that crash before SessionEnd fires).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))

from _lib import worktree_safety as _ws  # noqa: E402
from _lib.worktree_safety import (  # noqa: E402
    append_cleanup_log,
    current_worktree,
    find_main_repo,
    git,
    remove_worktree,
    snapshot_unrecoverable,
)

# Fail-closed process-liveness gate. Resolved by getattr, not by a hard import,
# so an older deployed _lib copy makes this hook REFUSE removals rather than
# fail to load and silently revert to the unguarded behaviour.
process_busy_reason = getattr(_ws, "process_busy_reason", None)


def _busy_reason(path: Path) -> str | None:
    """Why `path` must not be deleted, or None. A missing helper refuses."""
    if process_busy_reason is None:
        return ("process-liveness helper unavailable in this _lib deploy; "
                "refusing every removal")
    return process_busy_reason(path)


def _log(main_repo: Path, msg: str) -> None:
    """Record one reaping event.

    Path resolution lives in _lib so this hook and enforce-worktree-cap.py cannot
    drift apart again: they previously held two copies of the same buggy
    `main_repo / LOG_REL` join, so fixing one left the other still writing vault
    artifacts into whichever product repo it reaped.
    """
    append_cleanup_log(main_repo, msg)


def _done() -> int:
    print(json.dumps({"continue": True, "suppressOutput": True}))
    return 0


def main() -> int:
    if os.environ.get("KEEP_WORKTREE_ON_END") == "1":
        return _done()

    cw = current_worktree()
    if cw is None:
        return _done()  # not in a worktree — nothing to clean
    worktree, slug = cw

    main_repo = find_main_repo()
    if main_repo is None or main_repo.resolve() == worktree.resolve():
        return _done()

    # Only auto-remove throwaway claude/<slug> scratch worktrees. A deliberate
    # feature-branch worktree is left untouched.
    try:
        br = git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
        branch = br.stdout.decode().strip()
    except Exception:
        return _done()
    if not branch.startswith("claude/"):
        _log(main_repo, f"keep {slug}: branch {branch!r} is not claude/* scratch")
        return _done()

    # The session is over by definition at SessionEnd — a DETACHED CHILD of it
    # is not. This hook targets whatever worktree the hook PROCESS's cwd is in,
    # so a background gate the session launched (a test suite, a build) is still
    # standing in the tree we are about to unlink. Our own ancestor chain is
    # excluded by the probe, so anything it still reports here is foreign.
    # Checked BEFORE the recovery scan: no point walking a tree we will keep.
    busy = _busy_reason(worktree)
    if busy:
        _log(main_repo, f"REFUSE remove {slug}: {busy}. Worktree kept.")
        return _done()

    snapped, recoverable, all_safe = snapshot_unrecoverable(main_repo, worktree, slug)
    if not all_safe:
        _log(main_repo, f"REFUSE remove {slug}: a genuinely-unsaved file could "
                        f"not be snapshotted (snapped={snapped}). Worktree kept.")
        return _done()

    # chdir out of the worktree before removing it.
    try:
        os.chdir(main_repo)
    except OSError:
        pass

    if remove_worktree(main_repo, worktree, force=True):
        _log(main_repo, f"removed {slug} (branch {branch} kept; "
                        f"snapshotted {snapped} unsaved, {recoverable} recoverable-from-git)")
    else:
        _log(main_repo, f"WARN: git worktree remove failed for {slug}")
    return _done()


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except Exception:
        # Never let cleanup break session teardown.
        print(json.dumps({"continue": True, "suppressOutput": True}))
        sys.exit(0)
