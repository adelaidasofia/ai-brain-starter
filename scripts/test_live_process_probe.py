#!/usr/bin/env python3
"""Controls for the live-process liveness probe (MYC-3533).

`has_live_session_lock` is the ONLY thing standing between an actively-compiling
worktree and `dev-build-reclaim.py` deleting its `target/`, or
`dev-worktree-prune.py` deleting the whole worktree. Until 2026-08-26 every one
of its probes read a lock FILE, so the guard was alive only where session-lock.py
was actually writing. Measured that day: one busy repo's lock held ZERO
session entries and had not been written in 3210 minutes, while THREE live
sessions held that repo's worktrees as their CWD. The prune classified two
actively-edited worktrees REAP.

  [0] live process in the tree     -> LOCKED, with no lock file anywhere
  [1] live process in a SUBDIR     -> LOCKED (a session may cd deeper)
  [2] sibling prefix worktree      -> NOT locked (`repo` must not match
                                      `repo-slug`; without the separator one
                                      live session locks the whole fleet)
  [3] measured, nobody home        -> falls through to the lock probes
  [4] probe UNAVAILABLE (None)     -> falls through AND warns; never reads as idle
  [5] unresolvable path            -> LOCKED (cannot inspect what we would delete)
  [6] three-state contract         -> set() and None are not interchangeable
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from _lib import dev_repo_scan as drs  # noqa: E402

FAILED = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        FAILED.append(name)


def _reset_cache():
    drs._PROC_CWD_CACHE.update(at=0.0, value=None)
    drs._PROC_PROBE_WARNED[0] = False


def with_cwds(value):
    """Force live_process_cwds to a fixed answer (set or None)."""
    _reset_cache()
    drs.live_process_cwds = lambda now_ts=None: value


def main():
    real = drs.live_process_cwds
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wt = root / "repo-slug"
            (wt / "src").mkdir(parents=True)
            sibling = root / "repo"          # prefix of "repo-slug" by design
            sibling.mkdir()

            # [0] a live process whose cwd IS the worktree, no lock file at all
            with_cwds({str(wt)})
            check("[0] live process in the tree -> LOCKED",
                  drs.has_live_session_lock(wt, 1.0) is True)

            # [1] cwd deeper inside the worktree still counts
            with_cwds({str(wt / "src")})
            check("[1] live process in a subdir -> LOCKED",
                  drs.has_live_session_lock(wt, 1.0) is True)

            # [2] THE prefix bug: a session in `repo` must not lock `repo-slug`
            with_cwds({str(sibling)})
            check("[2] sibling prefix does NOT lock",
                  drs.has_live_session_lock(wt, 1.0) is False)
            with_cwds({str(wt)})
            check("[2b] and the converse holds too",
                  drs.has_live_session_lock(sibling, 1.0) is False)

            # [3] measured and empty -> fall through (no locks here -> False)
            with_cwds(set())
            check("[3] measured-empty falls through to lock probes",
                  drs.has_live_session_lock(wt, 1.0) is False)

            # [4] unavailable -> fall through, but WARN. A silent fallback is how
            #     "could not measure" becomes "idle, safe to delete".
            with_cwds(None)
            import io
            import contextlib
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                res = drs.has_live_session_lock(wt, 1.0)
            check("[4] unavailable probe does not read as LOCKED-false-positive",
                  res is False)
            check("[4b] unavailable probe WARNS once (fail loud, not silent)",
                  "probe unavailable" in err.getvalue())

            # [5] a path we cannot resolve is LOCKED, never idle
            with_cwds({str(wt)})
            drs.live_process_cwds = lambda now_ts=None: {str(wt)}

            class Boom(type(Path())):
                pass

            saved = os.path.realpath

            def boom(p):
                raise OSError("unresolvable")

            os.path.realpath = boom
            try:
                check("[5] unresolvable path -> LOCKED",
                      drs._process_is_live_in(wt, 1.0) is True)
            finally:
                os.path.realpath = saved

            # [6] the three-state contract is the whole point
            drs.live_process_cwds = real
            _reset_cache()
            live = drs.live_process_cwds(None)
            check("[6] real probe returns a set or None, never a bare falsy int",
                  live is None or isinstance(live, set))
    finally:
        drs.live_process_cwds = real
        _reset_cache()

    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}):")
        for f in FAILED:
            print(f"  x {f}")
        return 1
    print("OK - live-process probe: observes a session with no lock file; counts "
          "subdirectories; refuses to match a sibling on a path prefix; treats "
          "'could not measure' as neither locked nor idle, and says so out loud.")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
