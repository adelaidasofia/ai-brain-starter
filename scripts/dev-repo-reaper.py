#!/usr/bin/env python3
"""dev-repo-reaper — the durable close-loop for ~/dev branch/worktree/stash drift (MYC-683).

The automations that CREATE drift (per-session `claude/<slug>` branches,
dev-repo-worktrees, dev-repo-checkpoints) shipped no reaper, so the pile grew
unbounded (50->125 unpushed commits in 5 days, unnoticed). This walks every
~/dev git repo and reaps ONLY what is provably safe — content already on origin:

  REAP   : TRUE_MERGED + SQUASH_MERGED_STALE branches (not checked out),
           CLEAN worktrees on those branches (`git worktree remove`, NEVER --force),
           surplus/old `claude-checkpoint` stashes (keep newest-K + TTL).
  SURFACE: RELIC + GENUINE_UNIQUE branches — real un-backed-up state, printed for
           a human, NEVER auto-deleted.
  SKIP    : dirty worktrees, named (parked) stashes, the default branch, any branch
           checked out anywhere, and any repo with a live .session-lock.

PRE-SCAN FETCH (freshness belt): before planning, `git fetch --quiet origin` on
each primary repo so (a) merged-branch classification sees CURRENT origin refs
(else a branch merged on a fresher origin mis-surfaces as unpushed) and (b) the
~/dev fleet's origin/main never rots far behind — the read-time
STALE-BARE-CHECKOUT-READ guard then starts from a fresh ref instead of a 300-
commit-stale one. Read-only, fail-open per repo, bounded. `--no-fetch` skips it.

MERGED-PR CHECK (authoritative, MYC-2347): also before planning, `gh pr list
--state merged` per repo yields the set of head-branches whose PRs merged. A
branch (or its worktree) in that set is reapable even after origin/main has
advanced past its merge — the squash-merge diff FALSE-NEGATIVE that otherwise
left 47 merged worktrees (~170G) unreaped in one repo. Fail-open per repo
(gh missing / unauthed / offline → diff heuristic → preserve). `--no-fetch` skips
it too (an offline run has no auth for gh either).

DRY-RUN BY DEFAULT. `--apply` performs the reaps and writes a recovery manifest
(branch->sha, worktree->branch->sha, stash->message) to ~/.claude/logs/dev-repo-reaper/
so every deletion is reversible from reflog + the manifest.

The reap/preserve decision lives in _lib/dev_repo_scan.py and is proven RED-then-green
by hooks/test_dev_repo_reaper.py (neg-control: merged reaped, unmerged+dirty+named preserved).

Usage:
  dev-repo-reaper.py                 # dry-run, the whole dev-root fleet
  dev-repo-reaper.py --apply         # execute the safe tier + write manifest
  dev-repo-reaper.py --repo ~/dev/x  # one repo
  dev-repo-reaper.py --json          # machine-readable plan
  dev-repo-reaper.py --keep-k 10 --ttl-days 14
  dev-repo-reaper.py --no-fetch      # skip the pre-scan origin fetch (CI/offline)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path



def _add_lib_to_path() -> bool:
    """Locate the deployed/in-repo _lib package and put it on sys.path."""
    candidates = [
        os.environ.get("DEV_REPO_SCAN_DIR"),
        # THIS script's own sibling first. A ~/.claude/hooks/ copy can be an
        # older deploy, and a reaper running against a stale _lib silently loses
        # whatever the shipped version knows (the NO_REMOTE class, the merged-PR
        # override) — it looks like a clean fleet. The lib that ships WITH this
        # script is the one it was written against.
        str(Path(__file__).resolve().parent.parent / "hooks"),  # in-repo (scripts/../hooks)
        str(Path.home() / ".claude" / "skills" / "ai-brain-starter" / "hooks"),  # installed skill
        str(Path.home() / ".claude" / "hooks"),          # deployed (copied hooks)
    ]
    for c in candidates:
        if c and (Path(c) / "_lib" / "dev_repo_scan.py").exists():
            sys.path.insert(0, c)
            return True
    return False


if not _add_lib_to_path():
    print("dev-repo-reaper: could not locate _lib/dev_repo_scan.py "
          "(set DEV_REPO_SCAN_DIR).", file=sys.stderr)
    sys.exit(2)

try:
    from _lib.dev_repo_scan import (  # noqa: E402
        discover_primary_repos,
        execute_reap,
        fetch_merged_pr_branches,
        plan_repo_reap,
        resolve_dev_root,
        vault_shaped_repos,
    )
except ImportError as exc:  # a lib copy older than this script — fail LOUD
    print(f"dev-repo-reaper: the _lib/dev_repo_scan.py on sys.path is older than "
          f"this script ({exc}). Re-run the installer "
          f"(scripts/install-hooks-user-level.py) or set DEV_REPO_SCAN_DIR to a "
          f"current hooks dir. Refusing to run against a stale classifier: it "
          f"would report a clean fleet it cannot actually see.", file=sys.stderr)
    sys.exit(2)
# discover_primary_repos now lives in _lib/dev_repo_scan.py — ONE shared, full-
# fleet, worktree-deduped discovery for BOTH this reaper and the session-start
# unpushed surface, so a coverage change happens in one place (MYC-1894).


def _git(cwd, *args, timeout=15):
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 99, ""


def _fmt_plan(plan) -> list[str]:
    lines = []
    n = (len(plan.reap_branches) + len(plan.reap_worktrees)
         + len(plan.reap_stashes) + len(plan.surface_branches))
    if n == 0 and not plan.skipped:
        return lines
    lines.append(f"\n## {plan.repo.name}")
    for t in plan.reap_branches:
        lines.append(f"  REAP branch    {t.branch}  [{t.cls}] {t.sha}")
    for w in plan.reap_worktrees:
        lines.append(f"  REAP worktree  {w.path}  (branch {w.branch})")
    for s in plan.reap_stashes:
        lines.append(f"  REAP stash     {s.ref}  ({s.reason})  {s.message[:60]}")
    for t in plan.surface_branches:
        if t.cls == "relic":
            tag = "RELIC (obsolete?)"
        elif t.cls == "no_remote":
            tag = "NO REMOTE (backed up nowhere)"
        else:
            tag = "UNPUSHED WORK"
        lines.append(f"  SURFACE        {t.branch}  [{tag}] {t.sha}  -> push or drop")
    for name, reason in plan.skipped:
        lines.append(f"  skip           {name}: {reason}")
    return lines


# Vault-shaped dirs that must NEVER exist inside a ~/dev product repo. Tooling that
# hand-joins a vault-relative path onto a variable repo root creates these silently;
# they then get swept up by a broad `git add` and committed into product (sometimes
# public) repos. Seen three times now in independent tools:
#   2026-07-14  the journal indexer  ("stop stray-Meta creation")
#   2026-07-23  the reaper's own log writer (6 repos contaminated)
#   2026-07-23  a snapshot-dir vault-sniff cascade
# Point-fixing each writer does not converge; this detects the SYMPTOM wherever it
# lands, whichever tool caused it. Bug class VAULT-ARTIFACT-INSIDE-PRODUCT-REPO.
STRAY_VAULT_DIRS = ("⚙️ Meta",)
# Repos that legitimately ARE vaults (or ship vault scaffolding) — never flagged.
# Names vary per install, so the set is config-driven (`vault_shaped_repos` in
# ~/.claude/dev-hygiene.json) over a substrate default rather than hardcoded to
# one operator's repo names (MYC-2070).


def _stray_vault_artifacts(repos) -> list[str]:
    """Report vault-shaped dirs sitting inside product repos. Read-only, fail-open."""
    hits: list[str] = []
    for r in repos:
        try:
            vault_shaped = vault_shaped_repos()
            if r.name in vault_shaped or r.name.split("-")[0] in vault_shaped:
                continue
            for d in STRAY_VAULT_DIRS:
                stray = r / d
                if not stray.is_dir():
                    continue
                n = sum(1 for f in stray.rglob("*") if f.is_file())
                rc, tracked = _git(r, "ls-files", "--", d)
                staged = len([x for x in tracked.splitlines() if x.strip()]) if rc == 0 else 0
                flag = "  *** TRACKED/STAGED IN GIT ***" if staged else ""
                hits.append(f"  STRAY-VAULT-DIR {r.name}/{d}  ({n} file(s)){flag}")
        except OSError:
            continue
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="Reap merged/stale ~/dev drift safely.")
    ap.add_argument("--apply", action="store_true",
                    help="execute the safe-tier reaps (default: dry-run)")
    ap.add_argument("--repo", help="limit to one repo path (default: the whole dev-root fleet)")
    ap.add_argument("--keep-k", type=int, default=10,
                    help="keep newest K claude-checkpoint stashes per repo (default 10)")
    ap.add_argument("--ttl-days", type=float, default=14,
                    help="reap checkpoint stashes older than this (default 14)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip the network freshness ops: the pre-scan `git fetch "
                         "origin` AND the gh merged-PR check (CI/offline)")
    args = ap.parse_args()

    if args.repo:
        repos = [Path(args.repo).expanduser()]
    else:
        repos = discover_primary_repos(resolve_dev_root())

    # Pre-scan freshness pass: refresh origin refs so (a) merged-branch
    # classification below is computed against CURRENT origin/main (a branch
    # merged on a fresher origin must not mis-surface as unpushed) and (b) the
    # ~/dev fleet's origin refs never rot far behind — the read-time
    # stale-checkout guard then starts from a fresh ref. Read-only (no
    # working-tree mutation), fail-open per repo, bounded. --no-fetch skips.
    merged_sets: dict = {}
    if not args.no_fetch:
        fetched = 0
        for r in repos:
            rc, _ = _git(r, "fetch", "--quiet", "origin", timeout=15)
            if rc == 0:
                fetched += 1
        if not args.json:
            print(f"pre-scan fetch: refreshed origin on {fetched}/{len(repos)} repo(s)")

        # Authoritative merged-PR set per repo (MYC-2347): `gh` knows a branch
        # merged even after origin/main advances on files it touched — the
        # squash-merge diff FALSE-NEGATIVE that left 47 merged worktrees unreaped.
        # Fail-open per repo (None on any gh failure → diff heuristic → preserve).
        authoritative = 0
        for r in repos:
            m = fetch_merged_pr_branches(r)
            merged_sets[r] = m
            if m is not None:
                authoritative += 1
        if not args.json:
            print(f"merged-PR check: {authoritative}/{len(repos)} repo(s) returned "
                  f"an authoritative gh-merged set")

    plans = [
        plan_repo_reap(r, keep_k=args.keep_k, ttl_days=args.ttl_days,
                       merged_branches=merged_sets.get(r))
        for r in repos
    ]

    tot_b = sum(len(p.reap_branches) for p in plans)
    tot_w = sum(len(p.reap_worktrees) for p in plans)
    tot_s = sum(len(p.reap_stashes) for p in plans)
    tot_surf = sum(len(p.surface_branches) for p in plans)

    manifests = [execute_reap(p, apply=args.apply) for p in plans]
    strays = _stray_vault_artifacts(repos)

    if args.json:
        print(json.dumps({
            "applied": args.apply,
            "totals": {"reap_branches": tot_b, "reap_worktrees": tot_w,
                       "reap_stashes": tot_s, "surface_branches": tot_surf,
                       "stray_vault_dirs": len(strays)},
            "stray_vault_artifacts": strays,
            "manifests": manifests,
        }, indent=2))
    else:
        out = [
            f"dev-repo-reaper {'APPLY' if args.apply else 'DRY-RUN'} — "
            f"{len(repos)} repos scanned",
            f"REAP: {tot_b} branches, {tot_w} worktrees, {tot_s} checkpoint stashes  |  "
            f"SURFACE (human decision): {tot_surf} branches",
        ]
        for p in plans:
            out.extend(_fmt_plan(p))
        if strays:
            out.append("\nSTRAY VAULT ARTIFACTS (never auto-deleted — migrate, don't drop:")
            out.append("a snapshot dir holds UNSAVED WORK):")
            out.extend(strays)
        if not args.apply and (tot_b or tot_w or tot_s):
            out.append("\nRe-run with --apply to execute the REAP tier "
                       "(SURFACE items are never auto-deleted).")
        print("\n".join(out))

    if args.apply and (tot_b or tot_w or tot_s):
        log_dir = Path.home() / ".claude" / "logs" / "dev-repo-reaper"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest_path = log_dir / f"reap-{stamp}.json"
        manifest_path.write_text(json.dumps({
            "timestamp": stamp,
            "totals": {"reap_branches": tot_b, "reap_worktrees": tot_w,
                       "reap_stashes": tot_s},
            "manifests": manifests,
        }, indent=2))
        if not args.json:
            print(f"\nRecovery manifest -> {manifest_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
