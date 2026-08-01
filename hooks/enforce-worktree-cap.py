#!/usr/bin/env python3
"""Keep the worktree count near the live-session count — at SessionStart.

The session-end removal hook (remove-ended-worktree.py) keeps the count low in
the normal case. This is the BACKSTOP for the abnormal case: a session that
crashes or is killed before SessionEnd fires leaves its worktree behind. Over
enough crashes those accumulate again — and on a large vault each worktree is a
FULL checkout (thousands of watched files), so waiting for a blunt count cap to
fill before reclaiming is far too slow (the file watcher melts long first).

DESIGN: two layers, reclaim-then-ALLOW, never block.

  Layer A — per-session backstop (runs EVERY session, regardless of count):
    * DEAD-SESSION reap — a registered `claude/<slug>` scratch worktree whose
      owning session is no longer live (per `.claude/.session-lock.json`) is
      reclaimed promptly, regardless of the cap. Liveness is read from the lock;
      a live session's worktree (even idle) and the current one are NEVER touched.
      This is what actually keeps up with crashes — the count tracks live
      sessions instead of drifting up to the cap.
    * ORPHAN-DIR sweep — dirs under `.claude/worktrees/` that git no longer
      registers (relocation-copied / half-created) are reclaimed (relocation-aware,
      snapshot-safe). The cap loop can't see these (it reads `git worktree list`).

  Layer B — count cap (ceiling for many concurrent LIVE sessions):
    Power users run many concurrent sessions; a hard block on creation would break
    that. So when the count is over the cap we reclaim the OLDEST idle `claude/<slug>`
    scratch worktrees (snapshot first, keep branch refs) until back under it.
    Active worktrees (touched <60min), the current one, and deliberate
    feature-branch worktrees are never touched. If everything over the cap is
    active, we surface a note and leave them — correctness over the cap.

Cap:            WORKTREE_MAX env (default 12).
Dead-idle gate: WORKTREE_DEAD_IDLE_MIN env (default 60, matching the cap reaper) —
                a dead-session/orphan worktree must be untouched this long AND its
                session must be absent from the lock's recency window before reclaim.
Bypass:         WORKTREE_CAP_BYPASS=1.

LIVENESS NOTE: per session-lock.py, the only trustworthy liveness signal is
`last_activity_at` recency (the recorded pid is the ephemeral hook pid, dead on
arrival). live_session_cwds() therefore uses recency; this hook never reaps a cwd
that is active in that window, nor the current session's worktree.

WIRING (SessionStart):
  "SessionStart": [
    {"hooks": [{
      "type": "command",
      "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/enforce-worktree-cap.py 2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'"
    }]}
  ]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))

from _lib.worktree_safety import (  # noqa: E402
    append_cleanup_log,
    current_worktree,
    find_main_repo,
    git,
    is_idle,
    is_scratch_worktree,
    list_orphan_dirs,
    list_worktrees,
    live_session_cwds,
    reclaim_orphan_dir,
    remove_worktree,
    snapshot_unrecoverable,
)

DEFAULT_CAP = 12


def _log(main_repo: Path, msg: str) -> None:
    """Record one cap/reclaim event. See remove-ended-worktree.py:_log — path
    resolution is shared in _lib precisely so these two cannot diverge again.
    """
    append_cleanup_log(main_repo, f"cap: {msg}")


def _emit(ctx: str | None) -> int:
    if ctx:
        print(json.dumps({"continue": True, "additionalContext": ctx}))
    else:
        print(json.dumps({"continue": True, "suppressOutput": True}))
    return 0


def _branch(wt: Path) -> str:
    try:
        return git(wt, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=15).stdout.decode().strip()
    except Exception:
        return ""


def _per_session_backstop(main_repo: Path, cur_path: Path | None) -> tuple[list[str], dict[str, str]]:
    """Reclaim what the count-cap alone cannot keep up with — runs EVERY session,
    regardless of the total count. Returns (reaped_dead, swept_orphans).

      1. DEAD-SESSION worktrees: a registered `claude/<slug>` scratch worktree whose
         owning session is no longer live (machine crash / kill before SessionEnd
         fired). Reaped regardless of the cap — but ONLY when liveness is knowable
         (`live_session_cwds` is not None) and the worktree is neither a live
         session's nor the current one. On a big vault each worktree is a full
         checkout, so waiting for the cap (12) to trip before reclaiming crashed
         worktrees is far too slow; liveness lets us reclaim them at the next
         SessionStart instead.
      2. ORPHAN DIRS: dirs under `.claude/worktrees/` that git no longer registers
         (relocation-copied / half-created). `reclaim_orphan_dir` is relocation-aware
         and fail-safe (snapshots genuinely-unsaved content; keeps anything it cannot
         reason about). The cap loop can't see these (it reads `git worktree list`).

    Both paths are idle-gated + snapshot-safe. Fail-safe: any error is swallowed so a
    SessionStart is never blocked by cleanup.
    """
    reaped: list[str] = []
    swept: dict[str, str] = {}
    # Default 60 min matches the cap reaper's idle threshold (a value this codebase
    # already trusts as "safe to reclaim"). Liveness here is two gates, both of
    # which must say "gone": (a) the session's cwd is NOT active in the lock within
    # live_session_cwds()'s recency window (~35 min; pid is informational only — see
    # session-lock.py), AND (b) no file modified in dead_idle minutes. A live-but-idle
    # session trips neither until it has been genuinely silent AND untouched that long.
    try:
        dead_idle = max(1, int(os.environ.get("WORKTREE_DEAD_IDLE_MIN", 60)))
    except ValueError:
        dead_idle = 60

    # 1. dead-session registered scratch worktrees (liveness-gated)
    try:
        live = live_session_cwds(main_repo)
        if live is not None:  # None = liveness unknown → never reap on this basis
            for wt in [w for w in (list_worktrees(main_repo) or []) if is_scratch_worktree(w)]:
                try:
                    rp = wt.resolve()
                except OSError:
                    continue
                if cur_path and rp == cur_path:
                    continue
                if str(rp) in live:
                    continue  # a live session owns it — never pull the rug
                if not _branch(wt).startswith("claude/"):
                    continue  # deliberate feature-branch worktree
                if not is_idle(wt, idle_min=dead_idle):
                    continue  # touched recently — let it settle / SessionEnd handle it
                slug = wt.name
                snapped, _r, all_safe = snapshot_unrecoverable(main_repo, wt, slug)
                if not all_safe:
                    continue
                if remove_worktree(main_repo, wt, force=True):
                    reaped.append(slug)
                    _log(main_repo, f"reaped dead-session {slug} (no live owner, "
                                    f"idle>={dead_idle}m, snapshotted {snapped} unsaved)")
    except Exception:
        pass

    # 2. orphan dirs git no longer registers (relocation-aware, fail-safe)
    try:
        for od in list_orphan_dirs(main_repo):
            try:
                if cur_path and od.resolve() == cur_path:
                    continue
            except OSError:
                continue
            action, snapped = reclaim_orphan_dir(main_repo, od, idle_min=dead_idle)
            swept[od.name] = action
            if "removed" in action:
                _log(main_repo, f"orphan-dir {od.name}: {action} (snap {snapped})")
    except Exception:
        pass

    return reaped, swept


def main() -> int:
    if os.environ.get("WORKTREE_CAP_BYPASS") == "1":
        return _emit(None)

    try:
        cap = max(1, int(os.environ.get("WORKTREE_MAX", DEFAULT_CAP)))
    except ValueError:
        cap = DEFAULT_CAP

    main_repo = find_main_repo()
    if main_repo is None:
        return _emit(None)

    cur = current_worktree()
    cur_path = cur[0].resolve() if cur else None

    notes: list[str] = []

    # Per-session backstop (runs regardless of the count): reap worktrees whose
    # owning session has died + sweep orphan dirs git no longer registers. This is
    # what keeps the count near the live-session count, instead of letting crashed
    # worktrees pile up until the cap finally trips — far too slow at a full
    # checkout each.
    reaped, swept = _per_session_backstop(main_repo, cur_path)
    if reaped:
        notes.append(
            f"[worktree-backstop] Reclaimed {len(reaped)} dead-session worktree(s) "
            f"(no live owner; branches + any unsaved work preserved): "
            + ", ".join(reaped[:8]) + (f" +{len(reaped) - 8} more" if len(reaped) > 8 else "") + "."
        )
    swept_removed = [n for n, a in swept.items() if "removed" in a]
    if swept_removed:
        notes.append(
            f"[worktree-backstop] Swept {len(swept_removed)} orphan dir(s) git no longer tracked: "
            + ", ".join(swept_removed[:8])
            + (f" +{len(swept_removed) - 8} more" if len(swept_removed) > 8 else "") + "."
        )

    # Count cap — the ceiling for many concurrent LIVE sessions. Only SCRATCH
    # worktrees count; deliberate ~/dev/<repo>-<slug> siblings are never touched.
    wts = [w for w in (list_worktrees(main_repo) or []) if is_scratch_worktree(w)]
    if len(wts) <= cap:
        return _emit("\n".join(notes) if notes else None)

    # Cheap sort: oldest dir-mtime first. Deep checks happen lazily on removals.
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    candidates = sorted(wts, key=_mtime)
    need = len(wts) - cap
    removed: list[str] = []

    for wt in candidates:
        if len(removed) >= need:
            break
        if cur_path and wt.resolve() == cur_path:
            continue
        if not _branch(wt).startswith("claude/"):
            continue  # never auto-remove a deliberate worktree
        if not is_idle(wt, idle_min=60):
            continue
        slug = wt.name
        snapped, _recoverable, all_safe = snapshot_unrecoverable(main_repo, wt, slug)
        if not all_safe:
            continue
        if remove_worktree(main_repo, wt, force=True):
            removed.append(slug)
            _log(main_repo, f"reclaimed {slug} (over cap {cap}, snapshotted {snapped} unsaved)")

    remaining = len([w for w in (list_worktrees(main_repo) or []) if is_scratch_worktree(w)])
    if removed:
        notes.append(
            f"[worktree-cap] Reclaimed {len(removed)} idle worktree(s) to stay under cap {cap} "
            f"(branches + any unsaved work preserved): " + ", ".join(removed[:8])
            + (f" +{len(removed) - 8} more" if len(removed) > 8 else "") + f". Now {remaining}."
        )
    elif remaining > cap:
        notes.append(
            f"[worktree-cap] {remaining} worktrees (cap {cap}) but all over-cap ones are active "
            f"or deliberate — none safe to reclaim now. They'll be cleaned at their SessionEnd, "
            f"or run: python3 scripts/worktree-prune.sh"
        )
    return _emit("\n".join(notes) if notes else None)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({"continue": True, "suppressOutput": True}))
        sys.exit(0)
