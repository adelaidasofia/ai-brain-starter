#!/usr/bin/env python3
"""Tests for hooks/surface-stalled-git-operation.py.

The guard exists because a paused git operation blocks every session in a repo
while nothing reports it (five measured stalls: 7 days, 22h, ~6h, another, and
a 5h one that stranded 19 commits behind a detached HEAD).

So the load-bearing case here is the NEGATIVE CONTROL: a stale operation MUST
produce a report. A check that has only ever been run against a clean repo is
indistinguishable from one that is inert, and inert is exactly the failure this
guard was written to end.

Also pins the composition. The module is deliberately not its own SessionStart
entry (footprint SLA gate, ADR-0004), so if the call from
surface-stranded-session-artifacts.py is ever dropped, the guard silently stops
firing on every install. That is a regression no test of this module alone
would catch, so it is asserted directly.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
TARGET = HOOKS / "surface-stalled-git-operation.py"
HOST = HOOKS / "surface-stranded-session-artifacts.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load(TARGET, "_stalled_git_op_under_test")

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _repo(td: str) -> str:
    subprocess.run(["git", "init", "-q", td], check=True)
    subprocess.run(["git", "-C", td, "commit", "-q", "--allow-empty", "-m", "seed"],
                   check=True, env=_GIT_ENV)
    return td


def _git_dir(repo: str) -> Path:
    out = subprocess.run(["git", "-C", repo, "rev-parse", "--absolute-git-dir"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    return Path(out.stdout.strip())


def _age(path: Path, minutes: float) -> None:
    old = time.time() - minutes * 60
    os.utime(path, (old, old))


class TestStalledGitOperation(unittest.TestCase):
    def test_clean_repo_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(MOD.build_report(_repo(td)))

    def test_young_operation_is_silent(self):
        """A rebase started a minute ago is work in progress, not an incident."""
        with tempfile.TemporaryDirectory() as td:
            repo = _repo(td)
            (_git_dir(repo) / "rebase-merge").mkdir(parents=True)
            self.assertIsNone(MOD.build_report(repo))

    def test_stale_operation_fires(self):
        """NEGATIVE CONTROL. If this ever passes silently the guard is inert."""
        with tempfile.TemporaryDirectory() as td:
            repo = _repo(td)
            marker = _git_dir(repo) / "rebase-merge"
            marker.mkdir(parents=True)
            _age(marker, MOD.STALL_MINUTES + 5)
            report = MOD.build_report(repo)
            self.assertIsNotNone(report, "a stale operation produced no report")
            self.assertIn("IN FLIGHT", report)
            self.assertIn("rebase", report)

    def test_reports_a_non_rebase_operation_too(self):
        """The 2026-08-24 stall was a rebase, but a merge or cherry-pick strands
        work identically. Pinning one shape would leave the others invisible."""
        for marker_name, expect in (("MERGE_HEAD", "merge"),
                                    ("CHERRY_PICK_HEAD", "cherry-pick")):
            with self.subTest(marker=marker_name):
                with tempfile.TemporaryDirectory() as td:
                    repo = _repo(td)
                    m = _git_dir(repo) / marker_name
                    m.write_text("x", encoding="utf-8")
                    _age(m, MOD.STALL_MINUTES + 5)
                    report = MOD.build_report(repo)
                    self.assertIsNotNone(report, f"{marker_name} not caught")
                    self.assertIn(expect, report)

    def test_detached_head_is_called_out(self):
        """Detached HEAD is the dangerous half: commits land off the branch."""
        with tempfile.TemporaryDirectory() as td:
            repo = _repo(td)
            sha = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace").stdout.strip()
            subprocess.run(["git", "-C", repo, "checkout", "-q", "--detach", sha],
                           check=True, env=_GIT_ENV)
            marker = _git_dir(repo) / "rebase-merge"
            marker.mkdir(parents=True)
            _age(marker, MOD.STALL_MINUTES + 5)
            self.assertIn("DETACHED", MOD.build_report(repo))

    def test_not_a_repo_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(MOD.build_report(td))

    def test_self_test_passes(self):
        r = subprocess.run([sys.executable, str(TARGET), "--test"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestCompositionIntoTheHostHook(unittest.TestCase):
    """The module is not wired as its own SessionStart entry, so the call from
    the host hook IS its activation. Losing that call is a silent total
    regression on every install."""

    def test_host_hook_still_calls_it(self):
        self.assertIn("surface-stalled-git-operation",
                      HOST.read_text(encoding="utf-8"),
                      "host hook no longer invokes the stalled-op check")

    def test_host_hook_emits_the_report(self):
        """End to end: run the host hook in a repo with a stale operation and
        assert the finding reaches the real systemMessage envelope -- not just
        the helper."""
        with tempfile.TemporaryDirectory() as td:
            repo = _repo(td)
            marker = _git_dir(repo) / "rebase-merge"
            marker.mkdir(parents=True)
            _age(marker, MOD.STALL_MINUTES + 5)
            r = subprocess.run([sys.executable, str(HOST)], cwd=repo, input="{}",
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
            self.assertTrue(r.stdout.strip(), "host hook emitted nothing")
            msg = json.loads(r.stdout)["systemMessage"]
            self.assertIn("stalled-git-op", msg)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    unittest.main()
