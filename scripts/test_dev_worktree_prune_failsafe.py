#!/usr/bin/env python3
"""Fail-SAFE controls for dev-worktree-prune's error paths (MYC-587/677).

The happy paths are covered by test_dev_worktree_prune.py. These cover the ones
that only run when something goes WRONG, which is where a deleting tool does its
damage. Each was a real fail-UNSAFE default found in the pre-push doubt-pass:

  * `git status` fails            -> was read as "clean tree" -> would remove
                                     uncommitted work. Must SURFACE: "unknown"
                                     and "nothing to lose" are different answers.
  * a lock file cannot be stat'd  -> was read as "no live session" -> would
                                     remove a worktree out from under a running
                                     job. Must read as LOCKED.
  * a file cannot be copied into
    the snapshot                  -> was silently skipped, then the worktree was
                                     removed -> the only copy destroyed. Must
                                     refuse the removal.

Driven in-process (not via subprocess) so the failing syscall can be injected
precisely rather than simulated by corrupting a fixture.

Run: python3 scripts/test_dev_worktree_prune_failsafe.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location(
        "dev_worktree_prune", SCRIPTS / "dev-worktree-prune.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mod = load()
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mod.SNAPSHOT_ROOT = tmp / "snapshots"   # never the operator's real store
        wt = tmp / "repo-slug"
        (wt / ".claude").mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
        now = time.time()

        real_git = mod._git

        # --- 1. `git status` failure must SURFACE, never read as clean --------
        def git_status_fails(cwd, *args, **kw):
            if args and args[0] == "status":
                return 99, "", "index.lock held"
            if args and args[0] == "rev-parse" and "--git-common-dir" in args:
                return 0, str(tmp / "main" / ".git"), ""
            if args and args[0] == "rev-parse":
                return 0, "claude/x", ""
            if args and args[0] == "log":
                return 0, "", ""          # branch fully backed up
            return real_git(cwd, *args, **kw)

        (tmp / "main" / ".git").mkdir(parents=True)
        mod._git = git_status_fails
        # Backdate so the idle floor is not what surfaces it.
        import os
        old = now - 30 * 86400
        os.utime(wt, (old, old))
        os.utime(wt / ".git", (old, old))
        v = mod.classify(wt, 7.0, now)
        check(v["verdict"] == mod.SURFACE,
              f"a failed `git status` classified as {v['verdict']!r} — an "
              f"unreadable tree must never be treated as having nothing to lose")
        check("UNKNOWN" in v["reason"] or "unknown" in v["reason"].lower(),
              f"the reason does not say the state was unknown: {v['reason']!r}")

        # ...and the same call reports `known=False`, not an empty clean list.
        paths, known = mod.dirty_paths(wt)
        check(known is False and paths == [],
              "dirty_paths did not distinguish 'git failed' from 'tree clean'")
        mod._git = real_git

        # --- 1b. a DETACHED HEAD must SURFACE --------------------------------
        # Every other verdict here survives a mistake because the branch keeps
        # the commits. A detached HEAD does not: its commits are reachable from
        # no ref, so once the worktree is gone only the reflog holds them. The
        # un-backed-up check is keyed on a branch name, so a detached HEAD would
        # otherwise SKIP it entirely — the worst possible place to skip it.
        def git_detached(cwd, *args, **kw):
            if args and args[0] == "rev-parse" and "--git-common-dir" in args:
                return 0, str(tmp / "main" / ".git"), ""
            if args and args[0] == "rev-parse":
                return 0, "HEAD", ""       # detached
            if args and args[0] == "status":
                return 0, "", ""           # clean tree
            if args and args[0] == "log":
                return 0, "", ""           # would look "fully backed up"
            return real_git(cwd, *args, **kw)

        mod._git = git_detached
        v = mod.classify(wt, 7.0, now)
        mod._git = real_git
        check(v["verdict"] == mod.SURFACE,
              f"a DETACHED-HEAD worktree classified as {v['verdict']!r} — its "
              f"commits are on no branch, so removal leaves only the reflog")
        check("detached" in v["reason"].lower(),
              f"the reason does not name the detached HEAD: {v['reason']!r}")

        # --- 2. an unreadable lock reads as LOCKED ---------------------------
        lock = wt / ".session-lock"
        lock.write_text("pid\n", encoding="utf-8")
        real_stat = Path.stat

        def stat_boom(self, *a, **k):
            if self.name == ".session-lock":
                raise OSError(13, "Permission denied")
            return real_stat(self, *a, **k)

        Path.stat = stat_boom
        try:
            locked = mod.has_live_session_lock(wt, now)
        finally:
            Path.stat = real_stat
        check(locked is True,
              "an unreadable .session-lock read as NOT locked — the tool would "
              "delete a worktree a live session is using")

        # --- 3. an incomplete snapshot must refuse the removal ---------------
        src = wt / "UNSAVED.md"
        src.write_text("only copy\n", encoding="utf-8")
        real_copy = mod.shutil.copy2

        def copy_boom(*a, **k):
            raise OSError(28, "No space left on device")

        mod.shutil.copy2 = copy_boom
        try:
            dest, failed = mod.snapshot(wt, ["UNSAVED.md"], apply=True)
        finally:
            mod.shutil.copy2 = real_copy
        check(failed,
              "a snapshot whose file copy FAILED reported success — the caller "
              "would then remove the worktree and destroy the only copy")
        check((dest / "ORIGIN.txt").is_file(),
              "no ORIGIN.txt written, so the partial snapshot is unattributable")
        if (dest / "ORIGIN.txt").is_file():
            check("failed: 1" in (dest / "ORIGIN.txt").read_text(encoding="utf-8"),
                  "ORIGIN.txt does not record the failed copy")

        # A CLEAN snapshot still reports no failures (positive control, so the
        # check above cannot pass by always returning a non-empty list).
        dest2, failed2 = mod.snapshot(wt, ["UNSAVED.md"], apply=True)
        check(not failed2, f"a healthy snapshot reported failures: {failed2}")
        check((dest2 / "files" / "UNSAVED.md").read_text(encoding="utf-8") == "only copy\n",
              "the healthy snapshot did not preserve the file's bytes")

    if failures:
        print("FAILED — dev-worktree-prune fail-safe paths (MYC-587/677):")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("OK - fail-safe paths: unreadable status SURFACEs, unreadable lock "
          "reads as locked, incomplete snapshot refuses removal, healthy "
          "snapshot still preserves bytes")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): non-ASCII in the failure output.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
