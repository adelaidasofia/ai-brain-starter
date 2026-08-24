#!/usr/bin/env python3
"""Behavior controls for the claim-bearing surface's HONESTY properties.

The surface names branches whose work is claimed against a tracked issue but
invisible to every other session. Three ways it can lie, each measured on a real
fleet before this test existed:

  * SILENT TRUNCATION — it carries a wall-clock budget, and when the budget
    fired it dropped repos while the banner still read as a complete answer.
    29 entries reported when the true count was 109, nothing in the output to
    tell them apart. "No claims found" and "I ran out of time looking" are
    different answers and only one is safe to act on.
  * FALSE "BACKED UP NOWHERE" — `--not --remotes` reads LOCAL tracking refs,
    which a single-branch clone never creates for a topic branch. 15 of 56 repos
    were in that state; a branch provably on origin (identical sha via
    `ls-remote`) still counted as unpushed. A false loss-risk sits in the same
    list as the real ones and dilutes them.
  * SHIPPED WORK REPORTED AS STRANDED — squash-merges guarantee the tip is never
    an ancestor of the default branch, so ref topology always says "ahead" even
    for work that landed hours ago.

Each fix must not blunt the guard, so every case is paired with its inverse: the
genuinely-truncated / genuinely-unpushed / genuinely-unmerged case must still
fire. A one-directional test would pass for a surface that reports nothing.

Run: python3 hooks/test_claim_surface_honesty.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import dev_repo_scan as drs  # noqa: E402

OLD = "2020-01-01T00:00:00"


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, **kw)


def make_repo(root: Path) -> Path:
    """A repo with a real bare origin, so pushed-vs-unpushed is genuine."""
    repo, remote = root / "repo", root / "origin.git"
    repo.mkdir(parents=True, exist_ok=True)
    assert git(remote, "init", "-q", "--bare", str(remote)).returncode == 0 or True
    subprocess.run(["git", "init", "-q", "--bare", str(remote)],
                   capture_output=True, check=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "f.txt")
    env = dict(os.environ, GIT_AUTHOR_DATE=OLD, GIT_COMMITTER_DATE=OLD)
    git(repo, "commit", "-q", "-m", "base", env=env)
    git(repo, "remote", "add", "origin", str(remote))
    assert git(repo, "push", "-q", "-u", "origin", "main").returncode == 0, "push failed"
    git(repo, "remote", "set-head", "origin", "main")
    return repo


def add_branch(repo: Path, branch: str, *, push: bool, subject="work") -> None:
    git(repo, "checkout", "-q", "-b", branch, "main")
    (repo / "f.txt").write_text(f"change-{branch}\n", encoding="utf-8")
    git(repo, "add", "f.txt")
    env = dict(os.environ, GIT_AUTHOR_DATE=OLD, GIT_COMMITTER_DATE=OLD)
    git(repo, "commit", "-q", "-m", subject, env=env)
    if push:
        assert git(repo, "push", "-q", "origin", branch).returncode == 0, "push failed"
    git(repo, "checkout", "-q", "main")


def scan(repo: Path, *, pr_heads=None, budget=None):
    """(section, stats). pr_heads=None keeps the real fail-closed gh lookup."""
    if pr_heads is not None:
        drs.fetch_pr_head_branches = lambda _repo: frozenset(pr_heads)
    stats: dict = {}
    kw = {"stats": stats}
    if budget is not None:
        kw["budget_sec"] = budget
    entries = drs.collect_claim_bearing([repo], **kw)
    section = drs.format_claim_section(entries, truncated=bool(stats.get("truncated")))
    return section or "", stats, entries


def main() -> int:
    import tempfile
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    real_pr_lookup = drs.fetch_pr_head_branches

    # [1] truncation is VISIBLE, never a silent cap.
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        add_branch(repo, "claude/myc-9999-fixture", push=False)
        section, stats, _ = scan(repo, budget=0.001)
        check(stats.get("truncated") is True, "a starved scan did not record truncated")
        check("PARTIAL" in section,
              "SILENT CAP: a budget-starved scan rendered as a complete answer")

    # [2] a COMPLETE scan must not cry partial (the inverse — no cry-wolf).
    drs.fetch_pr_head_branches = real_pr_lookup
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        add_branch(repo, "claude/myc-9999-fixture", push=False)
        section, stats, _ = scan(repo)
        check(not stats.get("truncated"), "a complete scan reported truncated")
        check("PARTIAL" not in section, "a complete scan falsely claimed partial coverage")
        check("MYC-9999" in section, "a complete scan lost the finding entirely")

    # [3] single-branch clone: a branch ON origin is not called backed-up-nowhere.
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        add_branch(repo, "claude/myc-9999-pushed", push=True)
        git(repo, "config", "--unset-all", "remote.origin.fetch")
        git(repo, "config", "--add", "remote.origin.fetch",
            "+refs/heads/main:refs/remotes/origin/main")
        git(repo, "update-ref", "-d", "refs/remotes/origin/claude/myc-9999-pushed")
        section, _, _ = scan(repo, pr_heads={"claude/myc-9999-pushed"})
        check("UNPUSHED" not in section,
              "FALSE 'backed up nowhere' for a branch provably on origin")

    # [4] genuinely unpushed under the SAME narrow refspec must still fire.
    drs.fetch_pr_head_branches = real_pr_lookup
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        add_branch(repo, "claude/myc-9999-really-unpushed", push=False)
        git(repo, "config", "--unset-all", "remote.origin.fetch")
        git(repo, "config", "--add", "remote.origin.fetch",
            "+refs/heads/main:refs/remotes/origin/main")
        section, _, _ = scan(repo)
        check("UNPUSHED" in section,
              "remote-confirmation swallowed a GENUINELY unpushed branch")

    # [5] squash-merged work is not a stranded claim.
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        add_branch(repo, "claude/myc-9999-shipped", push=False)
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--squash", "claude/myc-9999-shipped")
        env = dict(os.environ, GIT_AUTHOR_DATE=OLD, GIT_COMMITTER_DATE=OLD)
        git(repo, "commit", "-q", "-m", "squash-land (MYC-9999)", env=env)
        assert git(repo, "push", "-q", "origin", "main").returncode == 0
        section, _, _ = scan(repo)
        check("MYC-9999" not in section,
              "a squash-merged branch still reads as claimed-and-stranded")

    # [6] a NOT-merged branch under identical conditions must still fire.
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        add_branch(repo, "claude/myc-9999-not-shipped", push=False)
        section, _, _ = scan(repo)
        check("MYC-9999" in section,
              "the content check swallowed genuinely unmerged work")

    if failures:
        print("FAILED — claim-surface honesty:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("OK - claim surface: truncation visible (and no false partial), "
          "remote-confirmed unpushed (and real loss-risk still fires), "
          "shipped work excluded (and unmerged work still fires)")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    # A hook that print()s non-ASCII raises UnicodeEncodeError on a cp1252
    # console: the gate then fails silently OPEN, or denies the tool call with
    # no legible cause. Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
