#!/usr/bin/env python3
"""dev-drift-report — GENUINE un-backed-up drift across the dev root (MYC-683/2070).

Reports the HARM, not a proxy. Counting commits whose SHA is on no remote
(`git log --branches --not --remotes`) conflates three very different things and
cried wolf at ~25x:

  - GENUINE_UNIQUE  : real divergent work backed up nowhere -> the ONLY real drift
  - TRUE_MERGED/SQUASH_MERGED_STALE : content already on origin (squash artifacts)
  - RELIC           : pre-history-rewrite orphans (obsolete, not stranded)

A cry-wolf banner trains everyone to ignore it, which defeats the point. So the
cheap SHA pre-filter finds the suspect branches, each is CLASSIFIED, and only the
genuine count is headlined; the drainable buckets are demoted to one line
pointing at dev-repo-reaper.py. A delta-over-24h signal says whether genuine
drift is GROWING (the real alarm) or holding steady.

WHY THIS IS NOT A SessionStart HOOK. The scan is fleet-wide git I/O (~15s cold
over ~56 repos). Paying that at every session start costs a process and seconds
on the interactive path, and a surfacer that overruns its hook timeout is killed
and reports nothing -- which reads exactly like a clean fleet. So the expensive
leg runs once in the daily maintenance pass with --write-state, and the
SessionStart surface (dev-hub-refresh-on-session-start.py, which already reads a
state file about this same fleet) renders it for free: same guarantee, no added
fan-out (docs/adr/0004-footprint-sla-gate.md).

Client-safe posture (MYC-2070): the dev root is $ABS_DEV_ROOT / the `dev_root`
key in ~/.claude/dev-hygiene.json / ~/dev, and a repo that is DELIBERATELY
local-only opts out with a `.no-remote-ok` file (or the config `no_remote_ok`
list) so it is not surfaced forever.

Usage:
  dev-drift-report.py                 # print the section ("" = clean fleet)
  dev-drift-report.py --write-state   # also persist it for the SessionStart surface
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# This lives in scripts/ now, so reach the hooks/_lib package explicitly. Own
# sibling FIRST: a ~/.claude/hooks copy can be an older deploy, and a stale
# classifier here would report a clean fleet it cannot actually see.
for _cand in (
    os.environ.get("DEV_REPO_SCAN_DIR"),
    str(Path(__file__).resolve().parent.parent / "hooks"),
    str(Path.home() / ".claude" / "skills" / "ai-brain-starter" / "hooks"),
    str(Path.home() / ".claude" / "hooks"),
):
    if _cand and (Path(_cand) / "_lib" / "dev_repo_scan.py").exists():
        sys.path.insert(0, _cand)
        break

try:
    from _lib.dev_repo_scan import (  # type: ignore
        BranchClass,
        ClaimState,
        collect_claim_bearing,
        collect_drift,
        discover_primary_repos,
        fetch_repos_capped,
        format_claim_section,
        format_narrow_refspec_section,
        narrow_refspec_repos,
    )
except Exception as _exc:  # a lib older than this script -> fail LOUD, never silent
    print(f"dev-drift-report: _lib/dev_repo_scan.py is missing or older than this "
          f"script ({_exc}). Re-run scripts/install-hooks-user-level.py.",
          file=sys.stderr)
    sys.exit(2)

# Env-overridable for the same reason as CLAIM_BRANCH_CACHE_PATH below and
# DRIFT_STATE_PATH further down: these are the 24h trend SAMPLES, so an
# unpinned caller scoped to a different dev root (a test, a client install)
# both reads the operator's real history and APPENDS fixture samples to it,
# skewing the "Δ over 24h" figure on a surface a human acts on.
STATE_PATH = Path(
    os.environ.get("DEV_DRIFT_TREND_STATE")
    or (Path.home() / ".claude" / ".drift-surfacer-state.json")
)
STATE_RETENTION_S = 7 * 24 * 3600

# Rendered claim section + the unpushed-claim count, cached with a TTL. The scan
# is fleet-wide git I/O (~15s cold over 56 repos) and this hook runs at EVERY
# session start; a surfacer that overruns its timeout is killed and reports
# nothing, which reads identically to a clean fleet. Branch state moves on the
# order of hours, so re-deriving it per session buys nothing — the existing
# fetch in this same hook is capped at 4h on the same reasoning.
# Overridable for the same reason DEV_DRIFT_STATE below is: this cache decides
# what the surfacer RENDERS. Unpinned, a test that drives this script reads the
# operator's real fleet scan (a 30-minute TTL means it is usually warm) and
# splices it into the fixture render — and on a cold cache WRITES the fixture
# scan back into the operator's real cache, silently replacing a live
# stranded-work verdict for the next half hour. Both directions were live:
# measured 2026-09-01, a 24-minute-old real cache put 35 memory-runtime-pro
# claim branches into a run whose ABS_DEV_ROOT was a 6-repo temp dir.
CLAIM_CACHE_PATH = Path(
    os.environ.get("CLAIM_BRANCH_CACHE_PATH")
    or (Path.home() / ".claude" / ".claim-branch-cache.json")
)
CLAIM_CACHE_TTL_S = 30 * 60


def _claim_cached() -> tuple[str | None, int] | None:
    """(section, n_unpushed_claims) from a fresh cache, or None when stale."""
    try:
        raw = json.loads(CLAIM_CACHE_PATH.read_text(encoding="utf-8"))
        if time.time() - float(raw["ts"]) < CLAIM_CACHE_TTL_S:
            return raw.get("section") or None, int(raw.get("n_unpushed", 0))
    except Exception:
        pass
    return None


def _claim_cache_write(section: str | None, n_unpushed: int) -> None:
    try:
        CLAIM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CLAIM_CACHE_PATH.write_text(
            json.dumps({"ts": time.time(), "section": section, "n_unpushed": n_unpushed}),
            encoding="utf-8",
        )
    except Exception:
        pass  # cache is an optimization, never a correctness dependency


DRIFT_STATE_PATH = Path(
    os.environ.get("DEV_DRIFT_STATE")
    or (Path.home() / ".claude" / ".dev-drift-state.json")
)


class _Done(Exception):
    """Carries the finished section out of the render path's early exits."""

    def __init__(self, message=None):
        Exception.__init__(self)
        self.message = message or ""


def _emit(message: str | None = None) -> None:
    raise _Done(message)


def _delta_24h(genuine_now: int) -> int | None:
    """Append the current genuine count; return growth vs the newest sample
    older than 24h (None if no such baseline yet). Fail-open."""
    now = time.time()
    samples = []
    try:
        if STATE_PATH.exists():
            samples = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if not isinstance(samples, list):
                samples = []
    except Exception:
        samples = []
    baseline = None
    for ts, count in samples:
        if now - ts >= 24 * 3600:
            baseline = count  # samples are appended in order; last qualifying wins
    samples.append([now, genuine_now])
    samples = [s for s in samples if now - s[0] <= STATE_RETENTION_S]
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(samples))
    except Exception:
        pass
    return None if baseline is None else genuine_now - baseline


def _render() -> None:
    # FULL FLEET, worktree-deduped — every git repo under the dev root, not
    # just the ones touched in the last hour (MYC-1894: the active-window filter
    # is the coverage hole that hid three whole un-backed-up repos, one +13).
    repos = discover_primary_repos()
    if not repos:
        _emit()

    # Fetch-first (frequency-capped + fail-loud). Coverage below runs EVERY
    # session regardless of the cap; only network freshness is throttled, and a
    # repo whose fetch failed is still scanned + surfaced (never silently dropped).
    fetch_failed, _did_fetch = fetch_repos_capped(repos)

    genuine, n_topic = collect_drift(repos)

    # Repos that CANNOT verify their own backup state (single-branch clones).
    # Reported as its own advisory instead of being counted as risk: "unknown"
    # and "at risk" are different messages, and merging them is how a banner
    # earns being ignored.
    narrow_section = format_narrow_refspec_section(narrow_refspec_repos(repos))

    # MYC-3290: claim-bearing branches are promoted OUT of the suppressed count
    # and headlined individually. Computed before the early exits below, because
    # a fleet with zero lost-work risk can still be sitting on finished work no
    # other session can see — the MYC-86 shape (pushed, no PR) trips no
    # loss-risk detector at all, so it would exit silently here.
    cached = _claim_cached()
    if cached is not None:
        claim_section, n_unpushed_claims = cached
    else:
        scan_stats: dict = {}
        claims = collect_claim_bearing(repos, stats=scan_stats)
        claim_section = format_claim_section(
            claims, truncated=bool(scan_stats.get("truncated"))
        )
        n_unpushed_claims = sum(1 for c in claims if c.state == ClaimState.UNPUSHED)
        _claim_cache_write(claim_section, n_unpushed_claims)

    # Don't bill the reader twice for the same branch: an UNPUSHED claim branch
    # is exactly what _count_topic_branches_with_unpushed already counted. The
    # pushed-no-PR ones were never in that count (their commits are on a remote).
    n_topic = max(0, n_topic - n_unpushed_claims)

    if not genuine and not n_topic and not claim_section:
        _emit(narrow_section or None)

    delta = _delta_24h(len(genuine))

    stale_note = ""
    if fetch_failed:
        stale_note = (
            f" ({len(fetch_failed)} repo(s) could not be fetched — their refs may "
            f"be stale; counts shown from last-known refs.)"
        )

    # Topic/session branches with local-only commits → one demoted line + reaper
    # pointer, so the banner stays a signal (no cry-wolf on the ~150 a full-fleet
    # scan turns up — the EXACT failure MYC-683 exists to prevent).
    topic_note = ""
    if n_topic:
        topic_note = (
            f" ({n_topic} topic/session branch(es) also carry local-only commits — "
            f"NOT headlined here; `dev-repo-reaper.py` classifies + drains them.)"
        )

    if not genuine:
        # Nothing at lost-work risk — only topic-branch / drainable drift remains.
        # The claim section still leads when present: "safe from loss" and
        # "visible to another session" are different guarantees, and this is
        # precisely the state the MYC-86 near-miss sat in.
        base = (
            "[unpushed-commits-surface]\n\n"
            f"No high-risk un-backed-up drift (no repo missing a remote, no unpushed "
            f"work on a main branch).{topic_note}{stale_note}"
        )
        parts = [x for x in (claim_section, base, narrow_section) if x]
        _emit("\n\n".join(parts))

    n_no_remote = sum(1 for *_h, cls in genuine if cls == BranchClass.NO_REMOTE)
    lines = [
        "[unpushed-commits-surface]",
        "",
        (
            f"{len(genuine)} item(s) at real LOST-WORK risk — a repo with no remote, "
            f"or unpushed commits on a main branch. Push/back-up now.{topic_note}"
            f"{stale_note}"
        ),
    ]
    if n_no_remote:
        lines.append(
            f"\n⚠ {n_no_remote} of these are repos with NO remote at all — there is "
            f"nowhere to push until you add one (create a backup remote, then push)."
        )
    if delta is not None:
        arrow = f"+{delta}" if delta > 0 else str(delta)
        trend = (
            "GROWING — push/merge is falling behind branch creation"
            if delta > 0 else "holding/shrinking"
        )
        lines.append(f"\nΔ over 24h: {arrow} genuine item(s) ({trend}).")
    lines.append("")
    for repo, branch, n, cls in genuine[:8]:
        plural = "s" if n != 1 else ""
        where = (
            "backed up nowhere — NO REMOTE"
            if cls == BranchClass.NO_REMOTE
            else "not on any remote"
        )
        lines.append(f"- `{repo}` :: `{branch}` ({n} commit{plural}, {where})")
    if len(genuine) > 8:
        lines.append(f"- ... and {len(genuine) - 8} more")
    lines += [
        "",
        "**Per item:** has a remote → `git push origin <branch>`; no remote → add "
        "a backup remote then push, OR `git branch -D <branch>` if abandoned.",
        "Bug class: `LOCAL-COMMITS-DRIFT-UNPUSHED` / `NO-REMOTE-BACKUP`. Durable "
        "close-loop: MYC-683 + MYC-1894 (`dev-repo-reaper.py` + weekly cron).",
    ]
    body = "\n".join(lines)
    parts = [x for x in (claim_section, body, narrow_section) if x]
    _emit("\n\n".join(parts))


def build_section() -> str:
    """The rendered drift section, or "" when nothing needs a human."""
    try:
        _render()
    except _Done as done:
        return done.message
    return ""


def write_state(section: str, path: Path = None) -> Path:
    """Persist the section for the SessionStart surface to render for free.

    Written even when the fleet is CLEAN: the reader must be able to tell
    "measured, nothing wrong" from "never measured", and an absent file must
    never be reported as a clean fleet.
    """
    path = path or DRIFT_STATE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "section": section}),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(f"dev-drift-report: could not write {path}: {exc}", file=sys.stderr)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Report un-backed-up drift across the dev root.")
    ap.add_argument("--write-state", action="store_true",
                    help="persist the section for the SessionStart surface")
    args = ap.parse_args()
    section = build_section()
    if args.write_state:
        write_state(section)
    if section:
        print(section)
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): these CLIs print em dashes and warning
    # glyphs, and an unguarded non-ASCII print raises UnicodeEncodeError there --
    # the caller then reads the empty output as "nothing to report", which on a
    # drift/reap surface means "clean fleet". Force UTF-8 so it cannot happen.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
