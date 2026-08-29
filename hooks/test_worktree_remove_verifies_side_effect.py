#!/usr/bin/env python3
"""Negative control for remove_worktree(): a half-removal must not report success.

THE BUG CLASS. `git worktree remove` unlinks the working tree in raw readdir
order. Killed partway — by a timeout — it leaves a contiguous PREFIX of the
checkout deleted while `.git`, HEAD and the admin record survive. The worktree
still lists as registered, so the next run re-damages it. Tracked files come
back with `git checkout -- .`; untracked ones (`.env.local`) do not come back
at all.

Two independent defects made that reachable:
  1. the call inherited GIT_TIMEOUT (120s), so a large checkout could be killed
     mid-unlink;
  2. success was read off `returncode == 0` alone, so a half-removal that git
     reported 0 for was recorded as a clean removal.

These tests pin the SIDE EFFECT (is the directory actually gone), not git's
self-report. Run: python3 hooks/test_worktree_remove_verifies_side_effect.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import worktree_safety as ws  # noqa: E402


class _FakeCompleted:
    def __init__(self, returncode):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = b""


def _with_fake_git(returncode, record):
    def fake_git(repo, args, timeout=ws.GIT_TIMEOUT):
        record["timeout"] = timeout
        record["args"] = args
        return _FakeCompleted(returncode)
    return fake_git


def test_half_removal_is_not_success():
    """git says 0 but the dir survives -> must report False."""
    real = ws.git
    rec = {}
    try:
        ws.git = _with_fake_git(0, rec)
        with tempfile.TemporaryDirectory() as td:
            wt = Path(td) / "survivor"
            wt.mkdir()                      # dir still there == half-removal
            got = ws.remove_worktree(Path(td), wt)
        assert got is False, (
            "remove_worktree reported SUCCESS while the worktree directory "
            "still exists — a half-removal recorded as a removal"
        )
    finally:
        ws.git = real


def test_real_removal_is_success():
    """Positive control: dir actually gone -> True. Guards against a fix that just returns False."""
    real = ws.git
    rec = {}
    try:
        ws.git = _with_fake_git(0, rec)
        with tempfile.TemporaryDirectory() as td:
            wt = Path(td) / "gone"          # never created == really removed
            got = ws.remove_worktree(Path(td), wt)
        assert got is True, "a genuine removal must still report True"
    finally:
        ws.git = real


def test_removal_is_not_timeout_bounded():
    """The unlink must not be killable mid-flight."""
    real = ws.git
    rec = {}
    try:
        ws.git = _with_fake_git(0, rec)
        with tempfile.TemporaryDirectory() as td:
            wt = Path(td) / "gone"
            ws.remove_worktree(Path(td), wt)
        assert rec.get("timeout") is None, (
            f"remove_worktree passed timeout={rec.get('timeout')!r}; a bounded "
            "timeout can kill `git worktree remove` mid-unlink and destroy "
            "untracked files permanently"
        )
    finally:
        ws.git = real


def test_dangling_symlink_is_not_success():
    """A leftover dangling symlink at the worktree path is not a clean removal."""
    real = ws.git
    rec = {}
    try:
        ws.git = _with_fake_git(0, rec)
        with tempfile.TemporaryDirectory() as td:
            wt = Path(td) / "linky"
            wt.symlink_to(Path(td) / "nonexistent-target")
            got = ws.remove_worktree(Path(td), wt)
        assert got is False, (
            "a dangling symlink still at the worktree path was reported as a "
            "clean removal (Path.exists() follows symlinks; use lexists)"
        )
    finally:
        ws.git = real


def test_nonzero_returncode_is_failure():
    real = ws.git
    rec = {}
    try:
        ws.git = _with_fake_git(1, rec)
        with tempfile.TemporaryDirectory() as td:
            wt = Path(td) / "gone"
            assert ws.remove_worktree(Path(td), wt) is False
    finally:
        ws.git = real


def main():
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313): this file's
    # docstring and assertion text carry non-ASCII, and a print() of that on a
    # cp1252 console raises UnicodeEncodeError -- the control would die before
    # it could report, which is the one failure a negative control must not have.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
