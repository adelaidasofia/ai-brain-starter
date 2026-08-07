#!/usr/bin/env python3
"""Controls for the abandoned-git-lock reclaim in ai-brain-auto-update.py.

MYC-3175. A git process killed mid-operation strands `.git/index.lock`; from
then on EVERY `git pull` fails with "Unable to create index.lock" and the
install silently stops receiving updates — including guard and security fixes.
Observed live 2026-07-20: a 0-byte index.lock dated Jul 17 had been failing
every pull for 3 days with no signal.

The reclaim must be aggressive enough to fix that and conservative enough never
to stomp a live git. Both halves are controlled here:

  FIRES (the catch)
    1. a stale index.lock is removed
    2. stale HEAD.lock / shallow.lock are removed too
    3. after the reclaim a real `git pull --ff-only` SUCCEEDS  <- end-to-end
    4. it works through a `.git`-as-pointer-file worktree

  DOES NOT FIRE (no corruption)
    5. a FRESH lock is left alone (a real concurrent git operation)
    6. a lock HELD by a live process is left alone, even when old
    7. a repo with no lock is untouched
    8. a non-repo path is a safe no-op
    9. the age threshold is env-tunable

Stdlib + git only. Exit 0 = all pass.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The reclaim is CANONICAL in hooks/_lib/git_locks.py and shared with the ~/dev
# hub fleet (MYC-3175 item 4), so test it there — testing a consumer's private
# copy is what lets two copies drift apart unnoticed.
_gl_spec = importlib.util.spec_from_file_location(
    "abs_git_locks", HERE.parent / "hooks" / "_lib" / "git_locks.py")
au = importlib.util.module_from_spec(_gl_spec)
sys.modules["abs_git_locks"] = au
_gl_spec.loader.exec_module(au)
# The public names, aliased to the private ones this suite was written against.
au._reclaim_stale_git_locks = au.reclaim_stale_git_locks
au._git_dir = au.git_dir
au._lock_is_held = au.lock_is_held

# The updater must still REACH the canonical reclaim — the import could silently
# fall through to its fail-open stub and every heal would quietly stop happening.
_au_spec = importlib.util.spec_from_file_location(
    "abs_au", HERE / "ai-brain-auto-update.py")
_au = importlib.util.module_from_spec(_au_spec)
sys.modules["abs_au"] = _au
_au_spec.loader.exec_module(_au)

PASS = 0
FAIL = 0


def ok(label):
    global PASS
    PASS += 1
    print(f"PASS  {label}")


def bad(label, why):
    global FAIL
    FAIL += 1
    print(f"FAIL  {label} :: {why}")


def mkrepo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    (root / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "f.txt"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=env)
    return root


def plant(repo: Path, name: str, age_sec: float) -> Path:
    lock = repo / ".git" / name
    lock.write_text("")
    t = time.time() - age_sec
    os.utime(lock, (t, t))
    return lock


TMP = Path(tempfile.mkdtemp())

# --- 1. stale index.lock removed -------------------------------------------
r = mkrepo(TMP / "stale")
lock = plant(r, "index.lock", 7200)
got = au._reclaim_stale_git_locks(r)
if not lock.exists() and "index.lock" in got:
    ok("1. stale index.lock (2h) is reclaimed")
else:
    bad("1. stale index.lock", f"exists={lock.exists()} returned={got}")

# --- 2. other stale git locks removed --------------------------------------
r = mkrepo(TMP / "stale-multi")
plant(r, "HEAD.lock", 7200)
plant(r, "shallow.lock", 7200)
got = au._reclaim_stale_git_locks(r)
if set(got) >= {"HEAD.lock", "shallow.lock"}:
    ok("2. stale HEAD.lock + shallow.lock reclaimed")
else:
    bad("2. other locks", f"returned={got}")

# --- 3. END-TO-END: a real pull works again after the reclaim ---------------
# THE regression. Before: pull fails forever. After: reclaim then pull succeeds.
env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
origin = mkrepo(TMP / "origin")
clone = TMP / "clone"
subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, env=env)
(origin / "new.txt").write_text("y")
subprocess.run(["git", "-C", str(origin), "add", "new.txt"], check=True, env=env)
subprocess.run(["git", "-C", str(origin), "commit", "-qm", "second"], check=True, env=env)

stuck = plant(clone, "index.lock", 7200)
pre = subprocess.run(["git", "-C", str(clone), "pull", "--ff-only", "-q"],
                     capture_output=True, text=True, env=env)
if pre.returncode == 0:
    bad("3. premise", "pull SUCCEEDED with a planted lock — the test proves nothing")
else:
    au._reclaim_stale_git_locks(clone)
    post = subprocess.run(["git", "-C", str(clone), "pull", "--ff-only", "-q"],
                          capture_output=True, text=True, env=env)
    if post.returncode == 0 and (clone / "new.txt").exists():
        ok("3. END-TO-END: pull blocked by the lock, succeeds after the reclaim")
    else:
        bad("3. end-to-end", f"post-reclaim pull rc={post.returncode} {post.stderr[:120]}")

# --- 4. worktree (.git is a pointer FILE, not a dir) -----------------------
r = mkrepo(TMP / "wtmain")
wt = TMP / "wtlinked"
subprocess.run(["git", "-C", str(r), "worktree", "add", "-q", str(wt)],
               check=True, capture_output=True, env=env)
gd = au._git_dir(wt)
if gd is None or not gd.exists():
    bad("4. worktree gitdir", f"resolved to {gd}")
else:
    wl = gd / "index.lock"
    wl.write_text("")
    t = time.time() - 7200
    os.utime(wl, (t, t))
    got = au._reclaim_stale_git_locks(wt)
    if not wl.exists() and "index.lock" in got:
        ok("4. worktree (.git pointer file) lock reclaimed")
    else:
        bad("4. worktree", f"exists={wl.exists()} returned={got}")

# --- 5. FRESH lock left alone ----------------------------------------------
r = mkrepo(TMP / "fresh")
lock = plant(r, "index.lock", 30)
got = au._reclaim_stale_git_locks(r)
if lock.exists() and not got:
    ok("5. FRESH lock (30s) left alone — a real concurrent git is not stomped")
else:
    bad("5. fresh lock", f"exists={lock.exists()} returned={got}")

# --- 6. HELD lock left alone even when OLD ---------------------------------
# The corruption case. A live process holds the fd; lsof must veto the age test.
r = mkrepo(TMP / "held")
lock = plant(r, "index.lock", 7200)
holder = subprocess.Popen(
    [sys.executable, "-c",
     f"f=open({str(lock)!r},'r'); import time; time.sleep(30)"])
time.sleep(1.5)
try:
    if au._lock_is_held(lock) is None:
        ok("6. HELD lock: SKIPPED (no lsof on this box — age test alone governs)")
    else:
        got = au._reclaim_stale_git_locks(r)
        if lock.exists() and not got:
            ok("6. HELD lock left alone despite being 2h old — no corruption")
        else:
            bad("6. held lock", f"REMOVED A HELD LOCK: exists={lock.exists()} got={got}")
finally:
    holder.kill()
    holder.wait()

# --- 7. no lock -> untouched ------------------------------------------------
r = mkrepo(TMP / "clean")
got = au._reclaim_stale_git_locks(r)
if not got:
    ok("7. repo with no lock is untouched")
else:
    bad("7. clean repo", f"returned={got}")

# --- 8. non-repo -> safe no-op ---------------------------------------------
p = TMP / "notarepo"
p.mkdir()
try:
    got = au._reclaim_stale_git_locks(p)
    ok("8. non-repo path is a safe no-op") if not got else bad("8. non-repo", got)
except Exception as e:
    bad("8. non-repo", f"raised {e!r}")

# --- 9. threshold is env-tunable -------------------------------------------
r = mkrepo(TMP / "tunable")
lock = plant(r, "index.lock", 120)
os.environ["ABS_STALE_GIT_LOCK_AGE_SEC"] = "60"
try:
    got = au._reclaim_stale_git_locks(r)
    if not lock.exists() and got:
        ok("9. ABS_STALE_GIT_LOCK_AGE_SEC lowers the threshold")
    else:
        bad("9. tunable", f"exists={lock.exists()} returned={got}")
finally:
    del os.environ["ABS_STALE_GIT_LOCK_AGE_SEC"]

# --- 10. the updater is WIRED to the canonical reclaim, not its fail-open stub -
# Without this the import could silently fall through and every heal would stop
# happening, with all nine cases above still green (they test the lib directly).
if getattr(_au, "_reclaim_stale_git_locks", None) is not None and \
        _au._reclaim_stale_git_locks.__doc__ == au.reclaim_stale_git_locks.__doc__:
    ok("10. ai-brain-auto-update resolves the CANONICAL reclaim (not the stub)")
else:
    bad("10. updater wiring",
        "the updater fell back to its fail-open stub — heals would silently stop")

print()
print(f"=== summary: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
