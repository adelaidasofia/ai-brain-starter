#!/usr/bin/env python3
"""One-shot SAFE worktree reclaim — registered-cap trim + orphan-dir sweep.

Steady-state worktree hygiene is the lifecycle hooks' job (remove-ended-worktree
at SessionEnd, enforce-worktree-cap at SessionStart). This is the explicit,
auditable reclaim for an EXISTING pileup — and the engine the auto-remediation
companion calls. It does two things the hooks alone don't:

  1. Trims registered `claude/<slug>` worktrees over the cap right now (same
     safety as enforce-worktree-cap, on demand).
  2. Sweeps ORPHAN DIRS — dirs under `.claude/worktrees/` that git no longer
     registers. The cap/remove hooks can't see these (they read
     `git worktree list`), so orphan dirs accumulate invisibly. This is what
     let the vault reach 19 orphan dirs with the hooks already live.

Non-destructive by construction:
  * committed work stays on its `claude/<slug>` branch (`git worktree remove`
    keeps the ref);
  * genuinely-unsaved files are snapshotted to the canonical snapshot dir BEFORE
    any removal (definitive object-DB recoverability test, fail-safe);
  * a worktree/dir we cannot reason about is KEPT and reported, never deleted;
  * active worktrees (touched < --idle-min ago) and the current session's
    worktree are never touched;
  * only `claude/<slug>` scratch worktrees are auto-removed — a deliberate
    feature-branch worktree is always kept.

Usage:
  worktree-reclaim.py [--repo PATH] [--cap N] [--idle-min M] [--dry-run] [--json]

  --repo      main checkout to clean (default: resolve from cwd / CLAUDE_PROJECT_DIR)
  --cap       keep at most N registered worktrees (default: $WORKTREE_MAX or 12)
  --idle-min  a worktree/dir touched within this many minutes is "active" (default 60)
  --dry-run   classify + report only; remove nothing
  --json      machine-readable report
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# Lib lives at <repo>/hooks/_lib in the ai-brain-starter layout, or colocated
# next to this script in the flattened ~/.claude/hooks layout.
for _cand in (_HERE.parent / "hooks", _HERE):
    if (_cand / "_lib" / "worktree_safety.py").is_file():
        sys.path.insert(0, str(_cand))
        break

from _lib.worktree_safety import (  # noqa: E402
    current_worktree,
    find_main_repo,
    git,
    is_idle,
    is_scratch_worktree,
    list_orphan_dirs,
    list_worktrees,
    reclaim_orphan_dir,
    remove_worktree,
    snapshot_unrecoverable,
)


def _branch(wt: Path) -> str:
    try:
        return git(wt, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=15).stdout.decode().strip()
    except Exception:
        return ""


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Safe one-shot worktree reclaim.")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--cap", type=int, default=int(os.environ.get("WORKTREE_MAX", 12)))
    ap.add_argument("--idle-min", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    main_repo = Path(args.repo).expanduser().resolve() if args.repo else find_main_repo()
    if main_repo is None or not (main_repo / WORKTREES_SEG_LOCAL).is_dir():
        print(f"no .claude/worktrees under {main_repo}", file=sys.stderr)
        return 1

    cur = current_worktree()
    cur_path = cur[0].resolve() if cur else None

    report: dict = {
        "repo": str(main_repo),
        "cap": args.cap,
        "dry_run": args.dry_run,
        "registered_before": 0,
        "registered_removed": [],
        "registered_kept": [],
        "orphans_before": 0,
        "orphans": {},
    }

    # --- 1. registered SCRATCH worktrees: trim idle claude/* over cap, oldest first ---
    # Deliberate ~/dev/<repo>-<slug> sibling worktrees are never auto-removed.
    regs = [w for w in (list_worktrees(main_repo) or []) if is_scratch_worktree(w)]
    report["registered_before"] = len(regs)
    over = max(0, len(regs) - args.cap)
    removed = 0
    for wt in sorted(regs, key=_mtime):
        if removed >= over:
            break
        if cur_path and wt.resolve() == cur_path:
            continue
        if not _branch(wt).startswith("claude/"):
            continue
        if not is_idle(wt, args.idle_min):
            report["registered_kept"].append(f"{wt.name}: active")
            continue
        if args.dry_run:
            report["registered_removed"].append(f"{wt.name}: would-remove")
            removed += 1
            continue
        snapped, _rec, all_safe = snapshot_unrecoverable(main_repo, wt, wt.name)
        if not all_safe:
            report["registered_kept"].append(f"{wt.name}: unsafe-snapshot")
            continue
        if remove_worktree(main_repo, wt, force=True):
            report["registered_removed"].append(f"{wt.name}: removed (snap {snapped})")
            removed += 1
        else:
            report["registered_kept"].append(f"{wt.name}: remove-failed")

    # --- 2. orphan dirs: snapshot-then-remove (or keep+report) ---
    orphans = list_orphan_dirs(main_repo)
    report["orphans_before"] = len(orphans)
    for od in orphans:
        if cur_path and od.resolve() == cur_path:
            report["orphans"][od.name] = "kept-current"
            continue
        if args.dry_run:
            if not is_idle(od, args.idle_min):
                report["orphans"][od.name] = "would-keep-active"
                continue
            try:
                st = git(od, ["status", "--porcelain"], timeout=60)
                if st.returncode != 0:
                    report["orphans"][od.name] = "would-keep-dangling"
                elif st.stdout.strip():
                    report["orphans"][od.name] = "would-snapshot+remove(dirty)"
                else:
                    report["orphans"][od.name] = "would-remove(clean)"
            except Exception:
                report["orphans"][od.name] = "would-keep-dangling"
            continue
        action, snapped = reclaim_orphan_dir(main_repo, od, args.idle_min)
        report["orphans"][od.name] = action + (f" (snap {snapped})" if snapped else "")

    # prune dangling git metadata (safe: only clears refs to gone worktrees)
    if not args.dry_run:
        try:
            git(main_repo, ["worktree", "prune"])
        except Exception:
            pass
        report["registered_after"] = len(list_worktrees(main_repo) or [])
        report["orphans_after"] = len(list_orphan_dirs(main_repo))

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        ra = report.get("registered_after", "(dry)")
        oa = report.get("orphans_after", "(dry)")
        print(f"repo: {main_repo}  (cap {args.cap}, idle {args.idle_min}m"
              + (", DRY-RUN" if args.dry_run else "") + ")")
        print(f"registered: {report['registered_before']} -> {ra}  "
              f"(removed {len(report['registered_removed'])}, kept {len(report['registered_kept'])})")
        for x in report["registered_removed"][:12]:
            print(f"  - {x}")
        if len(report["registered_removed"]) > 12:
            print(f"  - ... +{len(report['registered_removed']) - 12} more")
        for x in report["registered_kept"][:8]:
            print(f"  = {x}")
        print(f"orphans: {report['orphans_before']} -> {oa}")
        for name, act in list(report["orphans"].items())[:50]:
            print(f"  {act:30} {name}")
    return 0


# Imported lazily to keep the lib the single source of the segment constant.
from _lib.worktree_safety import WORKTREES_SEG as WORKTREES_SEG_LOCAL  # noqa: E402


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
