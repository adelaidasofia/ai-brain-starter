#!/usr/bin/env python3
"""dev-hub-refresh — keep bare ~/dev/<repo> hubs fresh (MYC-1893).

Bare hubs ROT: parked on stale feature branches, dirtied by edits, written into
by background jobs (STALE-BARE-CHECKOUT-READ, MYC-670 / MYC-1127). The read-time
guard warn-stale-dev-checkout.py DETECTS the rot; this is the PREVENTION half.

ONE auto-action: fast-forward a hub that is clean, on its default branch, behind,
and carrying no local commits (a GUARANTEED ff, reversible via reflog). Everything
else — dirty / on a feature branch / detached / diverged — is SURFACED, never
auto-switched, auto-cleaned, or auto-recovered. Worktrees and any repo with a live
.session-lock are skipped. The reap/surface decision lives in _lib/dev_repo_scan.py
and is proven RED-then-green (incl. neg controls) by hooks/test_dev_hub_refresh.py.

FETCH-FIRST: a stale local ref misleads in BOTH directions (MYC-1893 correction),
so the scan `git fetch --quiet origin` each hub before judging. --no-fetch skips it.

DRY-RUN BY DEFAULT. --apply performs the fast-forwards and writes the state file
(~/.claude/.dev-hub-refresh-state.json) the SessionStart surfacer reads, plus a
per-run log to ~/.claude/logs/dev-hub-refresh/. A fast-forward only advances the
branch pointer, so every action is reversible from the reflog.

Usage:
  dev-hub-refresh.py                 # dry-run, fetch-first, whole ~/dev fleet
  dev-hub-refresh.py --apply         # ff clean mains, surface the rest, write state
  dev-hub-refresh.py --repo ~/dev/x  # one hub
  dev-hub-refresh.py --no-fetch      # skip the pre-scan `git fetch origin`
  dev-hub-refresh.py --json          # machine-readable plan
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # Python 3.7+
    except (AttributeError, ValueError):
        pass
DEV_ROOT = Path(os.environ.get("DEV_HUB_REFRESH_ROOT") or (Path.home() / "dev"))
STATE_PATH = Path(
    os.environ.get("DEV_HUB_REFRESH_STATE")
    or (Path.home() / ".claude" / ".dev-hub-refresh-state.json")
)
LOG_DIR = Path.home() / ".claude" / "logs" / "dev-hub-refresh"
FETCH_TIMEOUT = 20


def _add_lib_to_path() -> bool:
    """Locate the deployed/in-repo _lib package and put it on sys.path."""
    for c in (
        os.environ.get("DEV_REPO_SCAN_DIR"),
        str(Path.home() / ".claude" / "hooks"),            # deployed
        str(Path(__file__).resolve().parent.parent / "hooks"),  # in-repo (bin/../hooks)
    ):
        if c and (Path(c) / "_lib" / "dev_repo_scan.py").exists():
            sys.path.insert(0, c)
            return True
    return False


if not _add_lib_to_path():
    print(
        "dev-hub-refresh: could not locate _lib/dev_repo_scan.py "
        "(set DEV_REPO_SCAN_DIR).",
        file=sys.stderr,
    )
    sys.exit(2)

from _lib.dev_repo_scan import (  # noqa: E402
    HUB_SURFACE_ACTIONS,
    classify_hub_action,
    discover_hubs,
    execute_hub_refresh,
    summarize_hub_states,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch(repo: Path) -> None:
    """Read-only origin refresh, fail-open + bounded."""
    try:
        subprocess.run(
            ["git", "-C", str(repo), "fetch", "--quiet", "origin"],
            capture_output=True,
            timeout=FETCH_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def refresh_fleet(repos, *, apply: bool, fetch: bool):
    """Fetch-first (optional), then classify + execute each hub.

    Returns (states, results). States are the POST-action classification so an
    ff'd hub reads as current in the state file (not perpetually "behind").
    """
    if fetch:
        for r in repos:
            _fetch(r)
    states, results = [], []
    for r in repos:
        st = classify_hub_action(r)
        res = execute_hub_refresh(st, apply=apply)
        if res.get("applied"):
            st = classify_hub_action(r)  # reflect the fast-forward
        states.append(st)
        results.append(res)
    return states, results


def _state_payload(states, results, applied: bool) -> dict:
    return {
        "generated_at": _utcnow(),
        "applied": applied,
        "hubs": [
            {
                "repo": s.repo.name,
                "path": str(s.repo),
                "action": s.action,
                "behind": s.behind,
                "ahead": s.ahead,
                "dirty": s.dirty,
                "current_branch": s.current_branch,
                "default_branch": s.default_branch,
            }
            for s in states
        ],
        "summary": summarize_hub_states(states),
        "ff_applied": [Path(r["repo"]).name for r in results if r.get("applied")],
    }


def _write_json(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Keep bare ~/dev hubs fresh: ff clean mains, surface the rest."
    )
    ap.add_argument("--apply", action="store_true",
                    help="perform the fast-forwards (default: dry-run)")
    ap.add_argument("--repo", help="limit to one hub path (default: whole ~/dev fleet)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip the pre-scan `git fetch origin` freshness pass")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    repos = [Path(args.repo).expanduser()] if args.repo else discover_hubs(DEV_ROOT)
    states, results = refresh_fleet(repos, apply=args.apply, fetch=not args.no_fetch)
    payload = _state_payload(states, results, args.apply)

    # The state file feeds the cheap SessionStart surfacer; refresh it every run
    # (dry-run included) so the surface stays current between --apply cycles.
    _write_json(STATE_PATH, payload)

    ff_done = payload["ff_applied"]
    summary = payload["summary"]

    if args.json:
        print(json.dumps(
            {"applied": args.apply, "ff_applied": ff_done,
             "results": results, "summary": summary}, indent=2))
    elif not args.quiet:
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"dev-hub-refresh {mode} — {len(repos)} hub(s) scanned")
        print(f"  ff: {summary['ff']}  surfaced (need human): {summary['surfaced']}  "
              f"max-behind: {summary['max_behind']}")
        if ff_done:
            print(f"  fast-forwarded: {', '.join(ff_done)}")
        for s in states:
            if s.action in HUB_SURFACE_ACTIONS:
                br = s.current_branch or "DETACHED"
                print(f"  SURFACE  {s.repo.name}  [{s.action}]  on {br}, "
                      f"{s.behind} behind, {s.ahead} ahead")
        if not args.apply and summary["ff"]:
            print("\nRe-run with --apply to fast-forward the clean mains "
                  "(SURFACE items are never auto-touched).")

    if args.apply:
        _write_json(
            LOG_DIR / f"refresh-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json",
            payload,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
