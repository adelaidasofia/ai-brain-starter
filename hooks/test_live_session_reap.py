#!/usr/bin/env python3
"""Regression test: liveness-aware per-session backstop in enforce-worktree-cap.

Bug class VAULT-MACHINERY-BLOAT-CHOKES-OBSIDIAN (root: worktree auto-cleanup not
keeping up). A blunt count cap (default 12) lets crashed worktrees pile up — at a
FULL checkout each, the file watcher melts long before the cap trips. Fix: at every
SessionStart, reap registered `claude/<slug>` scratch worktrees whose owning session
is DEAD (per `.claude/.session-lock.json`), regardless of the count cap, so the count
tracks live sessions instead of drifting up to the cap.

NEGATIVE CONTROLS (the guard must NOT reap what is still in use):
  - a LIVE session's worktree (even idle) -> KEPT
  - the CURRENT session's worktree        -> KEPT
  - a deliberate non-claude/* branch wt    -> KEPT
  - liveness UNKNOWN (no/garbage lock)     -> reap NOTHING on the dead-session basis
  - unsaved-unique file in a dead wt       -> SNAPSHOTTED before the dir is removed
Run: python3 hooks/test_live_session_reap.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import shutil
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
# Reaper artifacts now resolve to the REAL vault (see _lib.worktree_safety.artifact_base),
# so redirect them at import time — otherwise this suite writes its fixtures into the
# user's actual vault. Set before importing anything that resolves a snapshot path.
os.environ.setdefault("WORKTREE_ARTIFACT_ROOT", tempfile.mkdtemp(prefix="wt-artifacts-"))

from _lib.worktree_safety import live_session_cwds, snapshot_dir_for  # noqa: E402

# Load the hyphenated hook module by path to test its _per_session_backstop().
_spec = importlib.util.spec_from_file_location("enforce_cap", HOOK_DIR / "enforce-worktree-cap.py")
enforce_cap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(enforce_cap)

DEAD_PID = 2 ** 31 - 1  # almost certainly not a running process


def sh(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False, text=True)


def make_main():
    d = Path(tempfile.mkdtemp(prefix="wt-live-"))
    sh(d, "init", "-q", "-b", "main")
    sh(d, "config", "user.email", "t@t")
    sh(d, "config", "user.name", "t")
    (d / "committed.md").write_text("COMMITTED\n")
    (d / "⚙️ Meta").mkdir(parents=True, exist_ok=True)  # snapshot_dir_for -> vault path
    sh(d, "add", "committed.md")
    sh(d, "commit", "-qm", "init")
    (d / ".claude" / "worktrees").mkdir(parents=True)
    return d


def add_wt(main, slug, branch):
    wt = main / ".claude" / "worktrees" / slug
    sh(main, "worktree", "add", "-q", "-b", branch, str(wt), "HEAD")
    return wt


def make_idle(p, secs=7200):
    """Backdate every file (incl. the .git pointer file) so is_idle() sees it idle."""
    old = time.time() - secs
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                os.utime(os.path.join(root, f), (old, old))
            except OSError:
                pass
    try:
        os.utime(p, (old, old))
    except OSError:
        pass


def write_lock(main, entries):
    """entries: {cwd: {"pid":..., "last_activity_at":...}} -> session-lock.json"""
    lock = main / ".claude" / ".session-lock.json"
    sessions = {f"sid-{i}": {"cwd": cwd, **meta} for i, (cwd, meta) in enumerate(entries.items())}
    lock.write_text(json.dumps({"version": 2, "sessions": sessions}))


fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


def main():
    now = time.time()

    # ---- live_session_cwds() unit behaviour ----
    m = make_main()
    alive = add_wt(m, "alive", "claude/alive")
    dead = add_wt(m, "dead", "claude/dead")
    write_lock(m, {
        str(alive.resolve()): {"pid": os.getpid(), "last_activity_at": now - 9999},   # pid alive
        str(dead.resolve()): {"pid": DEAD_PID, "last_activity_at": now - 9999},        # pid dead + stale
    })
    live = live_session_cwds(m)
    check("live_session_cwds returns a set", isinstance(live, set))
    check("live includes alive-pid cwd", str(alive.resolve()) in live)
    check("live EXCLUDES dead-pid+stale cwd", str(dead.resolve()) not in live)

    # recency overrides a dead pid (recently active => treated live, over-preserve)
    write_lock(m, {str(dead.resolve()): {"pid": DEAD_PID, "last_activity_at": now - 60}})
    check("live includes recently-active even if pid dead", str(dead.resolve()) in live_session_cwds(m))

    # missing / garbage lock => None (UNKNOWN), never empty-set
    (m / ".claude" / ".session-lock.json").unlink()
    check("no lock -> None (unknown, not empty set)", live_session_cwds(m) is None)
    (m / ".claude" / ".session-lock.json").write_text("}{ not json")
    check("garbage lock -> None", live_session_cwds(m) is None)
    shutil.rmtree(m, ignore_errors=True)

    # ---- _per_session_backstop(): reap dead, keep everything in use ----
    m = make_main()
    alive = add_wt(m, "alive", "claude/alive")
    dead = add_wt(m, "dead", "claude/dead")
    cur = add_wt(m, "current", "claude/current")
    delib = add_wt(m, "feature", "feature/keep-me")     # deliberate non-claude branch
    (dead / "UNSAVED.md").write_text("UNIQUE UNSAVED WORK\n")  # must be snapshotted, not lost
    for w in (alive, dead, cur, delib):
        make_idle(w)
    write_lock(m, {
        str(alive.resolve()): {"pid": os.getpid(), "last_activity_at": now - 9999},  # LIVE
        str(dead.resolve()): {"pid": DEAD_PID, "last_activity_at": now - 9999},       # DEAD
        # 'current' intentionally absent from lock — protected by cur_path, not liveness
    })

    reaped, swept = enforce_cap._per_session_backstop(m, cur.resolve())

    check("dead-session worktree REAPED", "dead" in reaped)
    check("dead worktree dir gone", not dead.exists())
    snap = snapshot_dir_for(m) / "dead" / "UNSAVED.md"
    check("dead wt unsaved file SNAPSHOTTED before reap", snap.is_file()
          and "UNIQUE UNSAVED WORK" in snap.read_text())
    # NEGATIVE CONTROLS
    check("NEG live-session worktree KEPT", "alive" not in reaped and alive.exists())
    check("NEG current-session worktree KEPT", "current" not in reaped and cur.exists())
    check("NEG deliberate non-claude/* worktree KEPT", "feature" not in reaped and delib.exists())
    shutil.rmtree(m, ignore_errors=True)

    # ---- liveness UNKNOWN => reap NOTHING on the dead-session basis ----
    m = make_main()
    d1 = add_wt(m, "looksdead", "claude/looksdead")
    make_idle(d1)
    # no session-lock written at all
    reaped, swept = enforce_cap._per_session_backstop(m, None)
    check("NEG unknown-liveness reaps nothing (no lock)", reaped == [] and d1.exists())
    shutil.rmtree(m, ignore_errors=True)

    # ---- orphan-dir sweep is wired into the backstop (relocation-orphan removed) ----
    m = make_main()
    orph = m / ".claude" / "worktrees" / "reloc"
    orph.mkdir(parents=True)
    (orph / "committed.md").write_text("COMMITTED\n")  # recoverable from main object DB
    (orph / ".git").write_text("gitdir: /nonexistent/old/.git/worktrees/reloc\n")  # dangling
    make_idle(orph)
    reaped, swept = enforce_cap._per_session_backstop(m, None)
    check("orphan-dir swept by backstop", "reloc" in swept and "removed" in swept["reloc"])
    check("orphan dir gone", not orph.exists())
    shutil.rmtree(m, ignore_errors=True)

    print()
    if fails:
        print("FAILED:", fails)
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
