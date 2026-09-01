#!/usr/bin/env python3
"""No reaper may delete a worktree that has a LIVE PROCESS in it.

THE BUG CLASS. Every liveness gate these tools had was a PROXY, and every proxy
reads a BUSY session as a dead one:

  * the session lock records `last_activity_at`, refreshed when a tool call
    STARTS -- so a session sitting inside ONE long call (a test suite, a build,
    a migration) emits nothing for the whole run. Past the liveness window its
    entry is byte-identical to that of a session that exited, and past
    idle-expiry the entry is deleted outright. The lock's recorded pid cannot
    rescue it: that is the ephemeral hook process's pid, dead when written.
  * `is_idle()` / idle-days read mtime, and "nothing written here for an hour"
    is also exactly what a long build whose output goes elsewhere looks like.

So the harder a worktree is being worked in, the deader it looks. Observed in
the field: a worktree removed mid-test-run, several commits deep, by a sibling
session's start-up sweep.

WHAT EACH TOOL'S BLOCK PINS. These are CALL-SITE controls, not helper controls:
a correct helper that nothing calls is precisely the failure worth catching, so
every guarded remover gets its own four legs.

  [positive]      the same fixture IS deleted when nothing is running. Without
                  it a negative control passes for the wrong reason -- a tool
                  broken into never deleting anything would look perfect.
  [negative]      a real child process whose cwd is a SUBDIRECTORY of the
                  worktree (a test runner sits in one, not at the root) makes
                  the tool refuse. Its readiness wait reads the process table
                  DIRECTLY, never through the code under test: routing it
                  through the probe would let a broken probe fail the HELPER
                  instead of the assertion that matters.
  [fail-closed]   an unusable probe refuses. "No process found" and "could not
                  look" must never be the same answer to a caller that deletes.
  [importable]    the probe actually resolved in this layout. A None probe
                  refuses forever, which is safe but paralyses cleanup, so it
                  has to be a test failure rather than a quiet fleet.

Every fixture lives inside the test's own TemporaryDirectory, and every artifact
path these tools resolve (snapshot roots, cleanup logs, reclaim manifests) is
redirected there in setUp -- a suite that writes into the real recovery store is
indistinguishable from a real rescued worktree exactly when someone is looking
for one.

Run: python3 hooks/test_worktree_process_liveness.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HOOK_DIR = Path(__file__).resolve().parent
REPO_DIR = HOOK_DIR.parent
sys.path.insert(0, str(HOOK_DIR))

from _lib import dev_repo_scan as drs  # noqa: E402
from _lib import worktree_safety as ws  # noqa: E402

CHILD_READY_TIMEOUT_S = 15
CHILD_LIFETIME_S = 300  # outlives the suite; killed in cleanup either way


def _load(path: Path, name: str):
    """Import a hyphenated hook/script by path so its call sites can be driven."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ended = _load(HOOK_DIR / "remove-ended-worktree.py", "remove_ended_worktree")
cap = _load(HOOK_DIR / "enforce-worktree-cap.py", "enforce_worktree_cap")
reclaim = _load(REPO_DIR / "scripts" / "worktree-reclaim.py", "worktree_reclaim")
prune = _load(REPO_DIR / "scripts" / "dev-worktree-prune.py", "dev_worktree_prune")
build = _load(REPO_DIR / "scripts" / "dev-build-reclaim.py", "dev_build_reclaim")


class _Fake:
    """A git result. `git()` here captures bytes, so stdout is bytes."""

    def __init__(self, returncode: int = 0, stdout: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


class Base(unittest.TestCase):
    """Containment + a real busy child. Nothing here touches a real vault."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

        # Every artifact root these tools resolve, redirected into this test's
        # own dir. Read at CALL time, so patching here is enough.
        env = mock.patch.dict(
            os.environ,
            {
                "WORKTREE_ARTIFACT_ROOT": str(self.root / "artifacts"),
                "DEV_BUILD_RECLAIM_LOG_DIR": str(self.root / "reclaim-logs"),
                "DEV_WORKTREE_SNAPSHOT_ROOT": str(self.root / "wt-snapshots"),
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        # Read at IMPORT time in dev-worktree-prune, so the env var above cannot
        # reach it; patch the resolved constant too.
        snap = mock.patch.object(prune, "SNAPSHOT_ROOT", self.root / "wt-snapshots")
        snap.start()
        self.addCleanup(snap.stop)
        # Belt: no cleanup-log line may escape even if a root resolves oddly.
        log = mock.patch.object(ws, "append_cleanup_log")
        log.start()
        self.addCleanup(log.stop)

        # The process-table reading must not survive between tests. A reading
        # taken before a child was spawned would make the negative control fail
        # for a reason that has nothing to do with the code under test.
        ws._PROBE_CACHE.clear()
        self.addCleanup(ws._PROBE_CACHE.clear)

    def make_worktree(self, name: str = "victim") -> Path:
        wt = self.root / name
        (wt / "nested").mkdir(parents=True)
        return wt

    def busy_child(self, cwd: Path) -> subprocess.Popen:
        """A real process parked in `cwd`, proven live by the OS itself.

        The readiness wait deliberately does NOT call process_cwd_inside: a
        broken probe must fail the assertion under test, not this helper.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({CHILD_LIFETIME_S})"],
            cwd=str(cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def _stop() -> None:
            proc.kill()
            proc.wait(timeout=10)

        self.addCleanup(_stop)

        target = str(cwd.resolve())
        deadline = time.time() + CHILD_READY_TIMEOUT_S
        while time.time() < deadline:
            out = subprocess.run(
                ["lsof", "-d", "cwd", "-F", "n", "-p", str(proc.pid)],
                capture_output=True,
                text=True,
            ).stdout
            if any(ln[1:] == target for ln in out.splitlines() if ln[:1] == "n"):
                return proc
            time.sleep(0.1)
        self.fail(f"the OS never reported the child's cwd as {target}")

    def broken_probe(self):
        """Patch the ONE primitive every consumer funnels through."""
        return mock.patch.object(
            ws, "process_cwd_inside", side_effect=OSError("lsof unavailable")
        )

    @staticmethod
    @contextlib.contextmanager
    def quiet():
        """These CLIs print a full report; keep the gate log readable."""
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            yield


# ---------------------------------------------------------------------------
# The primitive itself.
# ---------------------------------------------------------------------------
class ProbeSemanticsTest(Base):
    def test_a_subdirectory_counts_as_inside(self) -> None:
        wt = self.make_worktree()
        self.assertFalse(
            ws.process_cwd_inside(wt, _cache={}),
            "nothing is running in a freshly-made fixture",
        )
        self.busy_child(wt / "nested")
        self.assertTrue(
            ws.process_cwd_inside(wt, _cache={}),
            "a test runner sits in a SUBDIRECTORY; that must count as in-use",
        )

    def test_a_sibling_sharing_a_path_prefix_does_not_count(self) -> None:
        """Without the separator one live session locks the whole fleet."""
        wt = self.make_worktree("repo-slug")
        sibling = self.root / "repo"  # a prefix of "repo-slug" by construction
        sibling.mkdir()
        self.busy_child(wt / "nested")
        cache: dict = {}
        self.assertTrue(ws.process_cwd_inside(wt, _cache=cache))
        self.assertFalse(
            ws.process_cwd_inside(sibling, _cache=cache),
            "`repo` must not match a process sitting in `repo-slug`",
        )

    def test_our_own_ancestor_chain_is_excluded(self) -> None:
        """A session-end sweep must still be able to retire its own tree.

        Deterministic by construction: we park OUR OWN process in a directory
        nothing else on the machine can be in, so the only occupant is us.
        """
        chain = ws._own_process_chain()
        self.assertIn(os.getpid(), chain, "our own pid must be in our own chain")
        self.assertGreater(len(chain), 1, "the chain must reach at least a parent")

        mine = self.root / "only-us"
        mine.mkdir()
        origin = Path.cwd()
        self.addCleanup(os.chdir, str(origin))
        os.chdir(str(mine))

        with mock.patch.object(ws, "_own_process_chain", return_value=set()):
            unfiltered = ws.process_cwd_inside(mine, _cache={})
        self.assertTrue(
            unfiltered,
            "control: with no exclusion the dir IS occupied -- so the False "
            "below is the exclusion working, not an empty process table",
        )
        self.assertFalse(
            ws.process_cwd_inside(mine, _cache={}),
            "our own chain must never veto the cleanup of the tree we run in",
        )

    def test_the_probe_does_not_observe_its_own_probe_process(self) -> None:
        """A spawned child inherits our cwd, and lsof lists ITSELF.

        Left unanchored the probe sees its own `lsof` standing in the tree, and
        a sweep launched from inside the tree it is retiring refuses forever --
        the ancestor-chain exclusion defeated by a descendant created one line
        earlier. Anchoring the probe at the filesystem root is the fix, and this
        pins it: nothing but us is ever in this directory.
        """
        mine = self.root / "probe-anchor"
        mine.mkdir()
        origin = Path.cwd()
        self.addCleanup(os.chdir, str(origin))
        os.chdir(str(mine))
        self.assertFalse(
            ws.process_cwd_inside(mine, _cache={}),
            "the probe reported its own subprocess as an occupant",
        )

    def test_descendants_are_NOT_excluded(self) -> None:
        """A detached gate still running is exactly what needs protecting."""
        wt = self.make_worktree("gated")
        self.busy_child(wt / "nested")  # our own direct child
        self.assertTrue(
            ws.process_cwd_inside(wt, _cache={}),
            "a child of ours holding the tree must still veto its deletion",
        )

    def test_no_output_is_UNKNOWN_and_raises_rather_than_empty(self) -> None:
        with mock.patch.object(
            ws.subprocess, "run", return_value=_Fake(0, b"")
        ), self.assertRaises(OSError):
            ws._live_cwds()

    def test_the_cache_makes_it_one_probe_per_sweep(self) -> None:
        cache: dict = {}
        with mock.patch.object(ws, "_live_cwds", return_value=[]) as spy:
            ws.process_cwd_inside(self.root / "a", _cache=cache)
            ws.process_cwd_inside(self.root / "b", _cache=cache)
            ws.process_cwd_inside(self.root / "c", _cache=cache)
        self.assertEqual(spy.call_count, 1, "one lsof per sweep, not per worktree")

    def test_reason_wrapper_fails_closed_on_an_unusable_probe(self) -> None:
        wt = self.make_worktree()
        self.assertIsNone(
            ws.process_busy_reason(wt, cache={}), "positive control: clean tree"
        )
        with self.broken_probe():
            reason = ws.process_busy_reason(wt, cache={})
        self.assertIsNotNone(reason, "an unusable probe must REFUSE, not allow")
        self.assertIn("unavailable", reason)

    def test_a_platform_with_no_probe_defers_instead_of_refusing_forever(self) -> None:
        """Fail-closed protects a BROKEN probe; it must not disable a platform.

        Windows has no `lsof` and no stdlib route to another process's cwd.
        Refusing every reap there would hand that user back the unbounded
        worktree pileup these hooks exist to prevent -- so an UNSUPPORTED
        platform says so once and lets the remaining gates decide. A missing
        `lsof` on POSIX stays an anomaly, and stays fail-closed.
        """
        wt = self.make_worktree()
        ws._UNSUPPORTED_NOTED[0] = False
        self.addCleanup(lambda: ws._UNSUPPORTED_NOTED.__setitem__(0, False))
        with mock.patch.object(ws.os, "name", "nt"), \
                mock.patch.object(ws.shutil, "which", return_value=None), \
                self.broken_probe():
            self.assertFalse(ws.probe_supported())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertIsNone(ws.process_busy_reason(wt, cache={}))
            self.assertIn("no process-cwd probe", err.getvalue(),
                          "an unsupported platform must say so, not go quiet")
        with mock.patch.object(ws.os, "name", "posix"), \
                mock.patch.object(ws.shutil, "which", return_value=None):
            self.assertTrue(
                ws.probe_supported(),
                "on POSIX a missing lsof is an anomaly the probe must refuse over",
            )
            with self.broken_probe():
                self.assertIsNotNone(ws.process_busy_reason(wt, cache={}))


# ---------------------------------------------------------------------------
# _lib/worktree_safety.py -- remove_worktree()
# ---------------------------------------------------------------------------
class RemoveWorktreeTest(Base):
    """The discriminator is whether `git worktree remove` was ISSUED.

    The helper reports the side effect (is the dir gone), so with a faked git
    the return value cannot distinguish "refused" from "ran and the dir stayed".
    """

    def setUp(self) -> None:
        super().setUp()
        self.calls: list = []

        def fake_git(repo, args, timeout=ws.GIT_TIMEOUT):
            self.calls.append(list(args))
            return _Fake(0)

        g = mock.patch.object(ws, "git", side_effect=fake_git)
        g.start()
        self.addCleanup(g.stop)

    def test_positive_control_issues_the_removal_when_nothing_runs(self) -> None:
        wt = self.make_worktree()
        ws.remove_worktree(self.root, wt)
        self.assertTrue(
            any(a[:2] == ["worktree", "remove"] for a in self.calls),
            "a clean worktree must still be removable",
        )

    def test_never_removes_a_worktree_with_a_live_process(self) -> None:
        wt = self.make_worktree()
        self.busy_child(wt / "nested")
        got = ws.remove_worktree(self.root, wt)
        self.assertEqual(self.calls, [], "no git command may be issued at all")
        self.assertFalse(got, "and it must not report a removal")

    def test_probe_failure_fails_closed(self) -> None:
        wt = self.make_worktree()
        with self.broken_probe():
            got = ws.remove_worktree(self.root, wt)
        self.assertEqual(self.calls, [])
        self.assertFalse(got)

    def test_probe_is_importable(self) -> None:
        self.assertIsNotNone(getattr(ws, "process_cwd_inside", None))
        self.assertIsNotNone(getattr(ws, "process_busy_reason", None))


# ---------------------------------------------------------------------------
# _lib/worktree_safety.py -- reclaim_orphan_dir() / _reclaim_disconnected_orphan()
# ---------------------------------------------------------------------------
class ReclaimOrphanDirTest(Base):
    def setUp(self) -> None:
        super().setUp()
        # A clean status: every file is recoverable, so the dir is reclaimable.
        g = mock.patch.object(ws, "git", return_value=_Fake(0, b""))
        g.start()
        self.addCleanup(g.stop)
        i = mock.patch.object(ws, "is_idle", return_value=True)
        i.start()
        self.addCleanup(i.stop)

    def test_positive_control_removes_a_clean_idle_orphan(self) -> None:
        orphan = self.make_worktree("orphan")
        action, _ = ws.reclaim_orphan_dir(self.root, orphan)
        self.assertIn("removed", action)
        self.assertFalse(orphan.exists(), "the orphan dir must actually be gone")

    def test_never_removes_an_orphan_with_a_live_process(self) -> None:
        orphan = self.make_worktree("orphan")
        self.busy_child(orphan / "nested")
        action, _ = ws.reclaim_orphan_dir(self.root, orphan)
        self.assertEqual(action, "kept-busy")
        self.assertTrue(orphan.exists())
        self.assertNotIn(
            "removed", action, "callers detect a removal with `'removed' in action`"
        )

    def test_disconnected_orphan_path_is_guarded_at_its_own_rmtree(self) -> None:
        """Its only caller is guarded too; a deletion path is gated where it deletes."""
        orphan = self.make_worktree("reloc")
        self.busy_child(orphan / "nested")
        action, _ = ws._reclaim_disconnected_orphan(self.root, orphan, "reloc")
        self.assertEqual(action, "kept-busy")
        self.assertTrue(orphan.exists())

    def test_probe_failure_fails_closed(self) -> None:
        orphan = self.make_worktree("orphan")
        with self.broken_probe():
            action, _ = ws.reclaim_orphan_dir(self.root, orphan)
        self.assertEqual(action, "kept-busy")
        self.assertTrue(orphan.exists())


# ---------------------------------------------------------------------------
# hooks/remove-ended-worktree.py (SessionEnd)
# ---------------------------------------------------------------------------
class RemoveEndedWorktreeTest(Base):
    """The session is over by definition at SessionEnd. A DETACHED CHILD of it
    is not, and this hook targets whatever tree the hook process's cwd is in.
    """

    def setUp(self) -> None:
        super().setUp()
        self.wt = self.make_worktree("ended")
        self.removed: list = []

    def _run(self) -> None:
        self.removed.clear()

        def _remove(main_repo, worktree, force=True):
            self.removed.append(Path(worktree).name)
            return True

        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(ended, "current_worktree",
                                  return_value=(self.wt, "ended")), \
                mock.patch.object(ended, "find_main_repo", return_value=self.root), \
                mock.patch.object(ended, "git",
                                  return_value=_Fake(0, b"claude/ended")), \
                mock.patch.object(ended, "snapshot_unrecoverable",
                                  return_value=(0, 0, True)), \
                mock.patch.object(ended, "remove_worktree", side_effect=_remove), \
                mock.patch.object(ended, "_log"), \
                mock.patch.object(ended, "_done", return_value=0), \
                mock.patch.object(os, "chdir"):
            ended.main()

    def test_positive_control_removes_when_nothing_is_running(self) -> None:
        self._run()
        self.assertEqual(self.removed, ["ended"])

    def test_never_removes_a_worktree_with_a_live_process(self) -> None:
        self.busy_child(self.wt / "nested")
        self._run()
        self.assertEqual(
            self.removed, [], "a detached child of the ended session still holds it"
        )

    def test_probe_failure_fails_closed(self) -> None:
        with self.broken_probe():
            self._run()
        self.assertEqual(self.removed, [])

    def test_probe_is_importable(self) -> None:
        self.assertIsNotNone(
            ended.process_busy_reason,
            "probe unresolved -> this hook refuses forever and cleanup stops",
        )


# ---------------------------------------------------------------------------
# hooks/enforce-worktree-cap.py (SessionStart) -- all three removal paths
# ---------------------------------------------------------------------------
class EnforceWorktreeCapTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.root / "repo"
        (self.repo / ".claude" / "worktrees").mkdir(parents=True)
        self.wts = []
        for name in ("alpha", "beta", "gamma"):
            wt = self.repo / ".claude" / "worktrees" / name
            (wt / "nested").mkdir(parents=True)
            self.wts.append(wt)
        self.removed: list = []
        self.reclaimed: list = []
        cap._PROBE_CACHE.clear()
        self.addCleanup(cap._PROBE_CACHE.clear)

    def _remove(self, main_repo, worktree, force=True):
        self.removed.append(Path(worktree).name)
        return True

    def _reclaim(self, main_repo, orphan, idle_min=60):
        self.reclaimed.append(Path(orphan).name)
        return ("removed", 0)

    def _drive(self, *, live_cwds, orphans, cap_max):
        """Every gate but the process probe forced OPEN, so only it can refuse."""
        self.removed.clear()
        self.reclaimed.clear()
        with mock.patch.dict(os.environ, {"WORKTREE_MAX": str(cap_max)}, clear=False), \
                mock.patch.object(cap, "find_main_repo", return_value=self.repo), \
                mock.patch.object(cap, "current_worktree", return_value=None), \
                mock.patch.object(cap, "list_worktrees", return_value=list(self.wts)), \
                mock.patch.object(cap, "is_scratch_worktree", return_value=True), \
                mock.patch.object(cap, "_branch", side_effect=lambda p: "claude/x"), \
                mock.patch.object(cap, "is_idle", return_value=True), \
                mock.patch.object(cap, "live_session_cwds", return_value=live_cwds), \
                mock.patch.object(cap, "list_orphan_dirs", return_value=orphans), \
                mock.patch.object(cap, "reclaim_orphan_dir", side_effect=self._reclaim), \
                mock.patch.object(cap, "snapshot_unrecoverable",
                                  return_value=(0, 0, True)), \
                mock.patch.object(cap, "remove_worktree", side_effect=self._remove), \
                mock.patch.object(cap, "_log"), \
                mock.patch.object(cap, "_emit", return_value=0):
            cap.main()

    # -- path 1: the dead-session backstop -----------------------------------
    def test_backstop_positive_control(self) -> None:
        self._drive(live_cwds=set(), orphans=[], cap_max=99)
        self.assertTrue(self.removed, "a dead-session worktree must be reclaimable")

    def test_backstop_never_reaps_a_busy_worktree(self) -> None:
        self.busy_child(self.wts[0] / "nested")
        self._drive(live_cwds=set(), orphans=[], cap_max=99)
        self.assertNotIn(self.wts[0].name, self.removed)

    # -- path 2: the orphan-dir sweep ----------------------------------------
    def test_orphan_sweep_positive_control(self) -> None:
        orphan = self.repo / ".claude" / "worktrees" / "orphan"
        (orphan / "nested").mkdir(parents=True)
        self._drive(live_cwds=set(), orphans=[orphan], cap_max=99)
        self.assertIn("orphan", self.reclaimed)

    def test_orphan_sweep_never_reclaims_a_busy_dir(self) -> None:
        orphan = self.repo / ".claude" / "worktrees" / "orphan"
        (orphan / "nested").mkdir(parents=True)
        self.busy_child(orphan / "nested")
        self._drive(live_cwds=set(), orphans=[orphan], cap_max=99)
        self.assertEqual(self.reclaimed, [])

    # -- path 3: the count cap (the one that force-removes) -------------------
    def test_cap_loop_positive_control(self) -> None:
        self._drive(live_cwds=None, orphans=[], cap_max=1)
        self.assertTrue(self.removed, "over-cap idle worktrees must be reclaimable")

    def test_cap_loop_never_removes_a_busy_worktree(self) -> None:
        self.busy_child(self.wts[0] / "nested")
        self._drive(live_cwds=None, orphans=[], cap_max=1)
        self.assertNotIn(
            self.wts[0].name,
            self.removed,
            "the cap loop force-removes; a live process must veto it",
        )

    def test_probe_failure_blocks_every_removal_path(self) -> None:
        orphan = self.repo / ".claude" / "worktrees" / "orphan"
        (orphan / "nested").mkdir(parents=True)
        with self.broken_probe():
            self._drive(live_cwds=set(), orphans=[orphan], cap_max=1)
        self.assertEqual(self.removed, [], "an unusable probe must fail CLOSED")
        self.assertEqual(self.reclaimed, [])

    def test_probe_is_importable(self) -> None:
        self.assertIsNotNone(cap.process_busy_reason)


# ---------------------------------------------------------------------------
# scripts/worktree-reclaim.py -- both destructive paths
# ---------------------------------------------------------------------------
class WorktreeReclaimTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.root / "repo"
        (self.repo / ".claude" / "worktrees").mkdir(parents=True)
        self.wt = self.repo / ".claude" / "worktrees" / "alpha"
        (self.wt / "nested").mkdir(parents=True)
        self.orphan = self.repo / ".claude" / "worktrees" / "orphan"
        (self.orphan / "nested").mkdir(parents=True)
        self.removed: list = []
        self.reclaimed: list = []
        reclaim._PROBE_CACHE.clear()
        self.addCleanup(reclaim._PROBE_CACHE.clear)

    def _drive(self) -> None:
        self.removed.clear()
        self.reclaimed.clear()

        def _remove(main_repo, worktree, force=True):
            self.removed.append(Path(worktree).name)
            return True

        def _reclaim(main_repo, orphan, idle_min):
            self.reclaimed.append(Path(orphan).name)
            return ("removed", 0)

        argv = ["worktree-reclaim.py", "--repo", str(self.repo), "--cap", "0"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(reclaim, "current_worktree", return_value=None), \
                mock.patch.object(reclaim, "list_worktrees", return_value=[self.wt]), \
                mock.patch.object(reclaim, "is_scratch_worktree", return_value=True), \
                mock.patch.object(reclaim, "_branch",
                                  side_effect=lambda p: "claude/x"), \
                mock.patch.object(reclaim, "is_idle", return_value=True), \
                mock.patch.object(reclaim, "list_orphan_dirs",
                                  return_value=[self.orphan]), \
                mock.patch.object(reclaim, "snapshot_unrecoverable",
                                  return_value=(0, 0, True)), \
                mock.patch.object(reclaim, "remove_worktree", side_effect=_remove), \
                mock.patch.object(reclaim, "reclaim_orphan_dir",
                                  side_effect=_reclaim), \
                mock.patch.object(reclaim, "git", return_value=_Fake(0, b"")):
            with self.quiet():
                reclaim.main()

    def test_positive_control_removes_and_reclaims(self) -> None:
        self._drive()
        self.assertEqual(self.removed, ["alpha"])
        self.assertEqual(self.reclaimed, ["orphan"])

    def test_never_removes_a_busy_registered_worktree(self) -> None:
        self.busy_child(self.wt / "nested")
        self._drive()
        self.assertEqual(self.removed, [])
        self.assertEqual(
            self.reclaimed, ["orphan"], "and the unaffected path still works"
        )

    def test_never_reclaims_a_busy_orphan_dir(self) -> None:
        self.busy_child(self.orphan / "nested")
        self._drive()
        self.assertEqual(self.reclaimed, [])
        self.assertEqual(self.removed, ["alpha"])

    def test_probe_failure_fails_closed(self) -> None:
        with self.broken_probe():
            self._drive()
        self.assertEqual(self.removed, [])
        self.assertEqual(self.reclaimed, [])

    def test_probe_is_importable(self) -> None:
        self.assertIsNotNone(reclaim.process_busy_reason)


# ---------------------------------------------------------------------------
# _lib/dev_repo_scan.py -- execute_reap() (the engine behind dev-repo-reaper.py)
# ---------------------------------------------------------------------------
class ExecuteReapTest(Base):
    """Merge-state says the CONTENT is safe. It says nothing about whether
    someone is standing in the directory right now.
    """

    def setUp(self) -> None:
        super().setUp()
        self.wt = self.make_worktree("merged")
        self.calls: list = []

        def fake_git(repo, *args, **kwargs):
            self.calls.append(list(args))
            return (0, "", "")

        g = mock.patch.object(drs, "_git", side_effect=fake_git)
        g.start()
        self.addCleanup(g.stop)
        ls = mock.patch.object(drs, "_ls_unregister_stale_bundles")
        ls.start()
        self.addCleanup(ls.stop)

    def _plan(self):
        return drs.ReapPlan(
            repo=self.root,
            reap_worktrees=[
                drs.WorktreeTarget(path=self.wt, branch="claude/x", sha="deadbee")
            ],
        )

    def _removals(self) -> list:
        return [a for a in self.calls if a[:2] == ["worktree", "remove"]]

    def test_positive_control_reaps_when_nothing_runs(self) -> None:
        manifest = drs.execute_reap(self._plan(), apply=True)
        self.assertTrue(self._removals(), "a merged clean worktree must be reapable")
        self.assertNotIn("skipped", manifest["worktrees"][0])

    def test_never_reaps_a_worktree_with_a_live_process(self) -> None:
        self.busy_child(self.wt / "nested")
        manifest = drs.execute_reap(self._plan(), apply=True)
        self.assertEqual(self._removals(), [])
        self.assertIn(
            "skipped",
            manifest["worktrees"][0],
            "the manifest must record WHY, not silently drop the entry",
        )

    def test_probe_failure_fails_closed(self) -> None:
        with self.broken_probe():
            drs.execute_reap(self._plan(), apply=True)
        self.assertEqual(self._removals(), [])

    def test_probe_is_importable(self) -> None:
        self.assertIsNotNone(drs._process_busy_reason)


# ---------------------------------------------------------------------------
# scripts/dev-worktree-prune.py -- remove()
# ---------------------------------------------------------------------------
class DevWorktreePruneTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.wt = self.make_worktree("repo-slug")
        self.calls: list = []

        def fake_git(cwd, *args, timeout=20):
            self.calls.append(list(args))
            return (0, "", "")

        g = mock.patch.object(prune, "_git", side_effect=fake_git)
        g.start()
        self.addCleanup(g.stop)
        m = mock.patch.object(prune, "main_checkout_of", return_value=self.root)
        m.start()
        self.addCleanup(m.stop)

    def _removals(self) -> list:
        return [a for a in self.calls if a[:2] == ["worktree", "remove"]]

    def test_positive_control_removes_when_nothing_runs(self) -> None:
        ok, note = prune.remove(self.wt, True, {})
        self.assertTrue(ok, note)
        self.assertTrue(self._removals())

    def test_never_removes_a_worktree_with_a_live_process(self) -> None:
        self.busy_child(self.wt / "nested")
        ok, note = prune.remove(self.wt, True, {})
        self.assertFalse(ok)
        self.assertEqual(self._removals(), [], "no git removal may be issued")
        self.assertIn("live process", note)

    def test_probe_failure_fails_closed(self) -> None:
        with self.broken_probe():
            ok, note = prune.remove(self.wt, True, {})
        self.assertFalse(ok)
        self.assertEqual(self._removals(), [])
        self.assertIn("unavailable", note)

    def test_probe_is_importable(self) -> None:
        self.assertIsNotNone(prune._process_busy_reason)


# ---------------------------------------------------------------------------
# scripts/dev-build-reclaim.py -- plan time AND apply time
# ---------------------------------------------------------------------------
class DevBuildReclaimTest(Base):
    """Its bottom pressure rung sets the idle threshold to zero days, which
    every worktree passes -- so this gate and the session lock are all that
    stand between an actively-compiling tree and losing its build cache.

    The probe reads the WORKTREE, never the artifact dir: a compiler sits in the
    source root and writes into `target/`, so probing `target/` would answer
    "nobody home" for exactly the build worth protecting.
    """

    def setUp(self) -> None:
        super().setUp()
        self.wt = self.root / "repo-slug"
        (self.wt / "nested").mkdir(parents=True)
        (self.wt / "src").mkdir()
        (self.wt / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.artifact = self.wt / "node_modules"
        (self.artifact / "pkg").mkdir(parents=True)
        (self.artifact / "pkg" / "index.js").write_text("//\n", encoding="utf-8")
        lock = mock.patch.object(build, "has_live_session_lock", return_value=False)
        lock.start()
        self.addCleanup(lock.stop)

    # -- plan time ------------------------------------------------------------
    def test_plan_positive_control_lists_the_artifact(self) -> None:
        items, _ = build.plan(self.root, 0.0, time.time())
        self.assertEqual([Path(i["path"]) for i in items], [self.artifact])

    def test_plan_skips_a_worktree_with_a_live_process(self) -> None:
        self.busy_child(self.wt / "nested")
        items, skipped = build.plan(self.root, 0.0, time.time())
        self.assertEqual(items, [], "a compiling worktree keeps its build cache")
        self.assertTrue(
            any("live process" in s for s in skipped),
            "and it is reported, never silently dropped",
        )

    def test_plan_fails_closed(self) -> None:
        with self.broken_probe():
            items, skipped = build.plan(self.root, 0.0, time.time())
        self.assertEqual(items, [])
        self.assertTrue(any("unavailable" in s for s in skipped))

    # -- apply time (re-probed, never reusing the plan-time reading) ----------
    def _drive_apply(self) -> list:
        deleted: list = []
        items = [{"worktree": str(self.wt), "path": str(self.artifact),
                  "idle_days": 9.0}]
        argv = ["dev-build-reclaim.py", "--apply", "--dev-root", str(self.root)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(build, "plan", return_value=(list(items), [])), \
                mock.patch.object(build, "ladder_idle_days", return_value=0.0), \
                mock.patch.object(build, "dir_size_mb", return_value=1), \
                mock.patch.object(build, "remove_artifact",
                                  side_effect=lambda p: deleted.append(Path(p).name)):
            with self.quiet():
                build.main()
        return deleted

    def test_apply_positive_control_deletes_when_nothing_runs(self) -> None:
        self.assertEqual(self._drive_apply(), ["node_modules"])

    def test_apply_never_deletes_from_a_busy_worktree(self) -> None:
        """A build can START between planning and applying: sizing hundreds of
        gigabytes takes minutes, so the apply loop takes its own reading."""
        self.busy_child(self.wt / "nested")
        self.assertEqual(self._drive_apply(), [])

    def test_apply_fails_closed(self) -> None:
        with self.broken_probe():
            self.assertEqual(self._drive_apply(), [])

    def test_probe_is_importable(self) -> None:
        self.assertIsNotNone(build._process_busy_reason)


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313): this file's
    # docstrings and assertion messages carry non-ASCII, and printing that on a
    # cp1252 console raises UnicodeEncodeError -- a control that dies before it
    # can report is the one failure a control must not have.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    unittest.main(verbosity=2)
