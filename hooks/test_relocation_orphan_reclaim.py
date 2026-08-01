#!/usr/bin/env python3
"""Regression test: worktree_safety.reclaim_orphan_dir handles RELOCATION-ORPHANS.

Bug class VAULT-MACHINERY-BLOAT-CHOKES-OBSIDIAN (root: worktree auto-cleanup not
keeping up). When a vault is relocated (e.g. iCloud Drive -> a local disk),
`.claude/worktrees/` is copied wholesale and each copied worktree's `.git` pointer
still references the GONE old path, so `git status` fails and the safe reclaim
classified them `kept-dangling` and NEVER removed them — letting `.claude/worktrees`
reach 100k+ files and melt Obsidian's file watcher. Fix: a dangling/external `.git`
pointer is the relocation / pruned-registration class; the MAIN repo's object DB is
still a definitive recoverability oracle, so snapshot the genuinely-unique files
then remove.

NEGATIVE CONTROLS (the guard must FAIL on the thing it catches):
  - unsaved-unique file MUST be snapshotted before the dir is deleted (no work loss)
  - garbage / unknown-provenance `.git` -> KEPT
  - active dir (touched < idle_min) -> KEPT
Run: python3 hooks/test_relocation_orphan_reclaim.py
"""
import os, subprocess, sys, tempfile, time, shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Reaper artifacts now resolve to the REAL vault (see _lib.worktree_safety.artifact_base),
# so redirect them at import time — otherwise this suite writes its fixtures into the
# user's actual vault. Set before importing anything that resolves a snapshot path.
os.environ.setdefault("WORKTREE_ARTIFACT_ROOT", tempfile.mkdtemp(prefix="wt-artifacts-"))

from _lib.worktree_safety import (  # noqa: E402
    reclaim_orphan_dir, _is_relocation_orphan, snapshot_dir_for,
)


def sh(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)


def make_main():
    d = Path(tempfile.mkdtemp(prefix="wt-main-"))
    sh(d, "init", "-q"); sh(d, "config", "user.email", "t@t"); sh(d, "config", "user.name", "t")
    (d / "committed.md").write_text("COMMITTED CONTENT\n")
    (d / "⚙️ Meta").mkdir(parents=True, exist_ok=True)  # snapshot_dir_for -> vault path
    sh(d, "add", "committed.md"); sh(d, "commit", "-qm", "init")
    (d / ".claude" / "worktrees").mkdir(parents=True)
    return d


def make_orphan(main, slug, gitdir, unique=False, idle=True):
    o = main / ".claude" / "worktrees" / slug
    o.mkdir(parents=True)
    (o / "committed.md").write_text("COMMITTED CONTENT\n")          # recoverable (in object DB)
    if unique:
        (o / "UNSAVED.md").write_text("UNIQUE UNSAVED WORK %s\n" % slug)  # NOT in object DB
    if gitdir is not None:
        (o / ".git").write_text("gitdir: %s\n" % gitdir)
    if idle:
        old = time.time() - 7200
        for root, _, fs in os.walk(o):
            for f in fs:
                os.utime(os.path.join(root, f), (old, old))
        os.utime(o, (old, old))
    return o


fails = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


def main():
    # T1: relocation-orphan, all content recoverable -> removed, nothing snapshotted
    m = make_main()
    o = make_orphan(m, "reloc-clean", "/nonexistent/Desktop/.git/worktrees/reloc-clean")
    check("T1 _is_relocation_orphan==True", _is_relocation_orphan(o, m))
    act, snap = reclaim_orphan_dir(m, o, idle_min=60)
    check("T1 action relocation-orphan-removed", act == "relocation-orphan-removed")
    check("T1 dir gone", not o.exists())
    check("T1 snapped==0", snap == 0)
    shutil.rmtree(m, ignore_errors=True)

    # T2: relocation-orphan WITH unique unsaved file -> snapshot THEN remove (key safety)
    m = make_main()
    o = make_orphan(m, "reloc-dirty", "/nonexistent/old/.git/worktrees/reloc-dirty", unique=True)
    act, snap = reclaim_orphan_dir(m, o, idle_min=60)
    check("T2 action relocation-orphan+removed", act == "relocation-orphan+removed")
    check("T2 snapped>=1", snap >= 1)
    check("T2 dir gone", not o.exists())
    sf = snapshot_dir_for(m) / "reloc-dirty" / "UNSAVED.md"
    check("T2 unique file SNAPSHOTTED before delete", sf.is_file())
    check("T2 snapshot content preserved", sf.is_file() and "UNIQUE UNSAVED WORK" in sf.read_text())
    shutil.rmtree(m, ignore_errors=True)

    # T3 (NEG): garbage .git (no gitdir pointer) -> unknown provenance -> KEPT
    m = make_main()
    o = m / ".claude" / "worktrees" / "garbage"; o.mkdir(parents=True)
    (o / "committed.md").write_text("COMMITTED CONTENT\n")
    (o / "UNSAVED.md").write_text("UNIQUE\n")
    (o / ".git").write_text("this is not a gitdir pointer\n")
    old = time.time() - 7200
    for root, _, fs in os.walk(o):
        for f in fs:
            os.utime(os.path.join(root, f), (old, old))
    os.utime(o, (old, old))
    check("T3 _is_relocation_orphan==False (garbage .git)", not _is_relocation_orphan(o, m))
    act, snap = reclaim_orphan_dir(m, o, idle_min=60)
    check("T3 action kept-dangling", act == "kept-dangling")
    check("T3 dir KEPT (not deleted)", o.exists())
    shutil.rmtree(m, ignore_errors=True)

    # T4 (NEG): relocation-orphan but ACTIVE -> kept-active, never deleted
    m = make_main()
    o = make_orphan(m, "reloc-active", "/nonexistent/.git/worktrees/reloc-active", unique=True, idle=False)
    act, snap = reclaim_orphan_dir(m, o, idle_min=60)
    check("T4 action kept-active", act == "kept-active")
    check("T4 dir KEPT (active not deleted)", o.exists())
    shutil.rmtree(m, ignore_errors=True)

    # T5 (NEG): .git -> EXISTING dir inside main .git = live registration, not relocation
    m = make_main()
    o = m / ".claude" / "worktrees" / "realish"; o.mkdir(parents=True)
    tgt = m / ".git" / "worktrees" / "realish"; tgt.mkdir(parents=True)
    (o / ".git").write_text("gitdir: %s\n" % tgt)
    check("T5 _is_relocation_orphan==False (existing dir inside main .git)", not _is_relocation_orphan(o, m))
    # T5b: dangling pointer INTO own .git (pruned registration) IS reclaimable (classic orphan)
    o2 = m / ".claude" / "worktrees" / "pruned"; o2.mkdir(parents=True)
    (o2 / ".git").write_text("gitdir: %s\n" % (m / ".git" / "worktrees" / "pruned-GONE"))
    check("T5b _is_relocation_orphan==True (dangling into own .git)", _is_relocation_orphan(o2, m))
    shutil.rmtree(m, ignore_errors=True)

    print()
    if fails:
        print("FAILED:", fails)
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
