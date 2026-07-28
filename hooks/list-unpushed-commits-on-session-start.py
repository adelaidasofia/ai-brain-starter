#!/usr/bin/env python3
"""SessionStart hook: surface GENUINE un-backed-up drift across ~/dev (MYC-683).

This is the HARM metric, not a proxy. The original version counted commits
whose exact SHA was on no remote (`git log --branches --not --remotes`). That
conflates three very different things and cried wolf at ~25x:

  - GENUINE_UNIQUE  : real divergent work backed up nowhere  -> the ONLY real drift
  - TRUE_MERGED/SQUASH_MERGED_STALE : content already on origin (squash artifacts)
  - RELIC           : pre-history-rewrite orphans (obsolete, not stranded)

A cry-wolf banner trains everyone to ignore it, which defeats the whole point.
So we run the cheap SHA pre-filter (find_unpushed_local_commits) to get the
suspect branches, then CLASSIFY each (classify_branch) and headline only the
GENUINE_UNIQUE count. The content-safe + relic buckets are noted separately and
pointed at `dev-repo-reaper.py`, which drains them safely. A Δ-over-24h signal
flags whether genuine drift is GROWING (the real alarm) vs holding steady.

Codified MYC-683. Pairs with list-wip-stashes-on-session-start.py.

Client-safe posture (MYC-2070): the dev root is $ABS_DEV_ROOT / the `dev_root`
key in ~/.claude/dev-hygiene.json / ~/dev, and a repo that is DELIBERATELY
local-only opts out with a `.no-remote-ok` file (or the config's `no_remote_ok`
list) so it is not surfaced every session forever.

WIRING (SessionStart):
  python3 ${CLAUDE_PLUGIN_ROOT}/hooks/list-unpushed-commits-on-session-start.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from _lib.dev_repo_scan import (  # type: ignore
        BranchClass,
        ClaimState,
        collect_claim_bearing,
        collect_drift,
        discover_primary_repos,
        fetch_repos_capped,
        format_claim_section,
    )
except Exception:
    print(json.dumps({}))
    sys.exit(0)

STATE_PATH = Path.home() / ".claude" / ".drift-surfacer-state.json"
STATE_RETENTION_S = 7 * 24 * 3600

# Rendered claim section + the unpushed-claim count, cached with a TTL. The scan
# is fleet-wide git I/O (~15s cold over 56 repos) and this hook runs at EVERY
# session start; a surfacer that overruns its timeout is killed and reports
# nothing, which reads identically to a clean fleet. Branch state moves on the
# order of hours, so re-deriving it per session buys nothing — the existing
# fetch in this same hook is capped at 4h on the same reasoning.
CLAIM_CACHE_PATH = Path.home() / ".claude" / ".claim-branch-cache.json"
CLAIM_CACHE_TTL_S = 30 * 60


def _claim_cached() -> tuple[str | None, int] | None:
    """(section, n_unpushed_claims) from a fresh cache, or None when stale."""
    try:
        raw = json.loads(CLAIM_CACHE_PATH.read_text())
        if time.time() - float(raw["ts"]) < CLAIM_CACHE_TTL_S:
            return raw.get("section") or None, int(raw.get("n_unpushed", 0))
    except Exception:
        pass
    return None


def _claim_cache_write(section: str | None, n_unpushed: int) -> None:
    try:
        CLAIM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CLAIM_CACHE_PATH.write_text(
            json.dumps({"ts": time.time(), "section": section, "n_unpushed": n_unpushed})
        )
    except Exception:
        pass  # cache is an optimization, never a correctness dependency


def _emit(message: str | None = None) -> None:
    if message:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": message,
            }
        }))
    else:
        print(json.dumps({}))
    sys.exit(0)


def _delta_24h(genuine_now: int) -> int | None:
    """Append the current genuine count; return growth vs the newest sample
    older than 24h (None if no such baseline yet). Fail-open."""
    now = time.time()
    samples = []
    try:
        if STATE_PATH.exists():
            samples = json.loads(STATE_PATH.read_text())
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


def main() -> None:
    try:
        json.load(sys.stdin)
    except Exception:
        pass

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

    # MYC-3290: claim-bearing branches are promoted OUT of the suppressed count
    # and headlined individually. Computed before the early exits below, because
    # a fleet with zero lost-work risk can still be sitting on finished work no
    # other session can see — the MYC-86 shape (pushed, no PR) trips no
    # loss-risk detector at all, so it would exit silently here.
    cached = _claim_cached()
    if cached is not None:
        claim_section, n_unpushed_claims = cached
    else:
        claims = collect_claim_bearing(repos)
        claim_section = format_claim_section(claims)
        n_unpushed_claims = sum(1 for c in claims if c.state == ClaimState.UNPUSHED)
        _claim_cache_write(claim_section, n_unpushed_claims)

    # Don't bill the reader twice for the same branch: an UNPUSHED claim branch
    # is exactly what _count_topic_branches_with_unpushed already counted. The
    # pushed-no-PR ones were never in that count (their commits are on a remote).
    n_topic = max(0, n_topic - n_unpushed_claims)

    if not genuine and not n_topic and not claim_section:
        _emit()

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
        _emit(f"{claim_section}\n\n{base}" if claim_section else base)

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
    _emit(f"{claim_section}\n\n{body}" if claim_section else body)


if __name__ == "__main__":
    main()
