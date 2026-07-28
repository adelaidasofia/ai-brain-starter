#!/usr/bin/env python3
"""Controls for dev-worktree-prune (MYC-587/677).

This script DELETES directories, so its controls are about what it refuses to
delete at least as much as what it reclaims. The failure being prevented is the
worktree-session-loss class: an abandoned session's uncommitted work removed
because nothing looked before removing.

  * clean + idle + commits on a remote        -> REAP
  * DIRTY                                     -> SNAPSHOT FIRST, then reap, and
                                                 the snapshot must actually
                                                 contain the file's bytes
  * commits on NO remote                      -> KEEP (never auto-removed)
  * recently touched                          -> KEEP (idle floor)
  * live .session-lock                        -> KEEP (a run is using it)
  * a PRIMARY checkout that merely has a dash -> never even a candidate
  * dry-run                                   -> removes nothing at all

Run: python3 scripts/test_dev_worktree_prune.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PRUNE = SCRIPTS / "dev-worktree-prune.py"


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, **kw)


def backdate(path: Path, days: float) -> None:
    """Make a worktree look idle. The classifier reads the newest of the dir's
    and the .git pointer's mtime, so both must move."""
    old = time.time() - days * 86400
    for p in (path, path / ".git"):
        try:
            os.utime(p, (old, old))
        except OSError:
            pass


def run_prune(dev_root: Path, *extra):
    r = subprocess.run(
        [sys.executable, str(PRUNE), "--dev-root", str(dev_root), "--json",
         "--idle-days", "1", *extra],
        capture_output=True, text=True,
        env=dict(os.environ, DEV_REPO_SCAN_DIR=str(SCRIPTS.parent / "hooks")))
    assert r.returncode == 0, f"prune failed: {r.stderr[-800:]}"
    return json.loads(r.stdout)


def verdict_for(doc, name):
    for v in doc["verdicts"]:
        if Path(v["path"]).name == name:
            return v
    return None


def main() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dev = tmp / "dev"
        dev.mkdir()

        # A primary checkout with a remote, plus worktrees hanging off it.
        main_repo = dev / "proj"
        main_repo.mkdir()
        git(main_repo, "init", "-q", "-b", "main")
        git(main_repo, "config", "user.email", "t@example.com")
        git(main_repo, "config", "user.name", "t")
        (main_repo / "a.txt").write_text("a", encoding="utf-8")
        git(main_repo, "add", "a.txt")
        git(main_repo, "commit", "-q", "-m", "base")
        bare = tmp / "origin.git"
        bare.mkdir()
        git(bare, "init", "-q", "--bare")
        git(main_repo, "remote", "add", "origin", str(bare))
        git(main_repo, "push", "-q", "origin", "main")
        git(main_repo, "fetch", "-q", "origin")

        def worktree(name, branch):
            path = dev / name
            assert git(main_repo, "worktree", "add", "-q", "-b", branch,
                       str(path), "main").returncode == 0, f"worktree {name} failed"
            return path

        clean = worktree("proj-clean", "claude/clean")
        dirty = worktree("proj-dirty", "claude/dirty")
        unpushed = worktree("proj-unpushed", "claude/unpushed")
        recent = worktree("proj-recent", "claude/recent")
        locked = worktree("proj-locked", "claude/locked")

        # dirty: an abandoned session's unsaved work — the thing that must survive
        (dirty / "UNSAVED.md").write_text("the only copy of this\n", encoding="utf-8")
        # unpushed: real work committed but on no remote
        (unpushed / "b.txt").write_text("b", encoding="utf-8")
        git(unpushed, "add", "b.txt")
        git(unpushed, "commit", "-q", "-m", "work nobody else has")
        # locked: a session is live in it
        (locked / ".session-lock").write_text("pid\n", encoding="utf-8")

        for w in (clean, dirty, unpushed, locked):
            backdate(w, 30)
        # `recent` deliberately keeps its fresh mtime.
        os.utime(locked / ".session-lock", None)   # lock stays live despite the backdate

        # --- DRY RUN removes nothing --------------------------------------
        doc = run_prune(dev)
        check(doc["totals"]["scanned"] == 5,
              f"expected 5 sibling worktrees, saw {doc['totals']['scanned']}")
        check(verdict_for(doc, "proj") is None,
              "a PRIMARY checkout was treated as a prunable worktree — the "
              "`.git`-is-a-file discriminator is what stops this script from "
              "deleting somebody's actual clone")
        for w in (clean, dirty, unpushed, recent, locked):
            check(w.exists(), f"dry-run REMOVED {w.name} — dry-run must not mutate")

        check(verdict_for(doc, "proj-clean")["verdict"] == "reap",
              "a clean, idle, fully-backed-up worktree was not reaped")
        check(verdict_for(doc, "proj-dirty")["verdict"] == "snapshot-then-reap",
              "a DIRTY worktree was not routed through a snapshot first")
        check(verdict_for(doc, "proj-unpushed")["verdict"] == "surface",
              "a worktree whose commits are on NO remote was marked reapable — "
              "this is the un-backed-up-work loss class")
        check(verdict_for(doc, "proj-recent")["verdict"] == "surface",
              "a freshly-touched worktree was reaped (idle floor not honored)")
        check(verdict_for(doc, "proj-locked")["verdict"] == "surface",
              "a worktree with a LIVE session lock was reaped — removing it out "
              "from under a running job is how a long gate loses its files")

        # --- APPLY: reap, and prove the snapshot holds the real bytes -------
        doc = run_prune(dev, "--apply")
        check(not clean.exists(), "clean worktree was not actually removed")
        check(not dirty.exists(), "dirty worktree was not removed after snapshot")
        check(unpushed.exists() and recent.exists() and locked.exists(),
              "a KEEP-classified worktree was removed by --apply")

        snap = Path(verdict_for(doc, "proj-dirty")["snapshot"])
        saved = snap / "files" / "UNSAVED.md"
        check(saved.is_file(),
              f"the uncommitted file was NOT copied into the snapshot ({snap}) — "
              f"the removal destroyed the only copy")
        if saved.is_file():
            check(saved.read_text(encoding="utf-8") == "the only copy of this\n",
                  "the snapshot exists but does not hold the file's actual bytes")

        # The reaped branches must still exist — a worktree removal must never
        # take commits with it.
        branches = git(main_repo, "branch", "--format=%(refname:short)").stdout.split()
        check("claude/clean" in branches,
              "removing a worktree deleted its BRANCH; the commits went with it")

    if failures:
        print("FAILED — dev-worktree-prune (MYC-587/677):")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("OK - dev-worktree-prune: reaps clean/idle/backed-up, snapshots dirty "
          "before removing (bytes verified), keeps un-backed-up + fresh + locked, "
          "ignores primary checkouts, dry-run is inert, branches survive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
