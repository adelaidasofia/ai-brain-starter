#!/usr/bin/env python3
"""Recover orphan commits on claude/* branches by fast-forwarding them into master.

Background
==========
ai-brain-starter sessions run in git worktrees rooted at
`<vault>/.claude/worktrees/<slug>/`. Before the fix in `session-end-hook.sh`
that lands with issue #65, the hook resolved its `VAULT` variable via
`$SCRIPT_DIR/../..`, which — when invoked from the worktree's own checkout of
the script — resolved to the worktree path. Every write (session file,
decisions, captures) and every `git add` then targeted the worktree branch
`claude/<slug>` instead of the main `master` branch.

Symptom: when the worktree was archived (Claude Code's harness removes the
directory) and `worktree-prune.sh` ran later, the orphan `claude/*` branch
was deleted. The commits became unreachable. They survived in the reflog
until `git gc`, then were gone.

What this script does
=====================
1. Enumerate every local `claude/*` branch.
2. For each branch, count commits not reachable from `master`.
3. Branches with zero unmerged commits: skip (nothing to recover).
4. Branches with unmerged commits: fast-forward-merge into master if the
   branch's history is a linear superset of master. Otherwise, list the
   non-FF branches so the user can decide (manual merge or cherry-pick).
5. Print a summary: scanned, recovered, non-FF (needs human), skipped.

Invocation
==========
    python3 scripts/recover-orphan-claude-branches.py [--dry-run] [--vault PATH]

Defaults:
- VAULT: current git toplevel (works from main vault root or any worktree).
- Mode: live by default. Pass `--dry-run` to preview without merging.

Safe to re-run. Already-merged branches are skipped. Non-FF branches are
never auto-merged — they require human review.

Exit codes:
- 0: completed (may have left non-FF branches behind; they're listed).
- 1: setup error (not a git repo, master missing, etc.).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], cwd: Path, check: bool = True) -> str:
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(
            f"command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


def list_claude_branches(vault: Path) -> list[str]:
    out = run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/claude/"], vault)
    return [line.strip() for line in out.splitlines() if line.strip()]


def unmerged_count(vault: Path, branch: str, base: str) -> int:
    out = run(["git", "rev-list", "--count", f"{base}..{branch}"], vault, check=False)
    try:
        return int(out)
    except ValueError:
        return 0


def is_fast_forward(vault: Path, branch: str, base: str) -> bool:
    """Branch is FF onto base iff base is an ancestor of branch."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, branch],
        cwd=str(vault),
        capture_output=True,
    )
    return result.returncode == 0


def base_branch(vault: Path) -> str:
    """Return the name of the integration branch. Prefer master; fall back to main."""
    for candidate in ("master", "main"):
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"],
            cwd=str(vault),
        )
        if result.returncode == 0:
            return candidate
    raise SystemExit("no master or main branch found in this repo")


def in_worktree(vault: Path) -> bool:
    """Detect if we're operating inside a worktree (not the main checkout)."""
    out = run(["git", "rev-parse", "--git-common-dir"], vault, check=False)
    git_dir = run(["git", "rev-parse", "--git-dir"], vault, check=False)
    return bool(out) and bool(git_dir) and Path(out).resolve() != Path(git_dir).resolve()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Preview without merging.")
    ap.add_argument("--vault", default=None, help="Vault root (default: git toplevel).")
    args = ap.parse_args()

    if args.vault:
        vault = Path(args.vault).expanduser().resolve()
    else:
        toplevel = run(["git", "rev-parse", "--show-toplevel"], Path.cwd())
        vault = Path(toplevel).resolve()

    if not (vault / ".git").exists() and not (vault / ".git").is_file():
        print(f"ERROR: {vault} is not a git repository", file=sys.stderr)
        return 1

    if in_worktree(vault):
        common = run(["git", "rev-parse", "--git-common-dir"], vault)
        main_vault = Path(common).resolve().parent
        print(f"NOTE: detected worktree. Operating on main vault: {main_vault}")
        vault = main_vault

    base = base_branch(vault)
    current_head = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], vault)
    if current_head != base:
        print(f"ERROR: must be on '{base}' to recover (currently on '{current_head}').")
        print(f"  Run: git -C \"{vault}\" checkout {base}")
        return 1

    branches = list_claude_branches(vault)
    if not branches:
        print(f"No claude/* branches found in {vault}. Nothing to do.")
        return 0

    scanned = 0
    clean = 0
    recovered = 0
    nonff: list[tuple[str, int]] = []

    for branch in branches:
        scanned += 1
        count = unmerged_count(vault, branch, base)
        if count == 0:
            clean += 1
            continue

        if is_fast_forward(vault, branch, base):
            if args.dry_run:
                print(f"DRY-RUN: would fast-forward {branch} ({count} commit(s)) into {base}")
                recovered += 1
            else:
                # Fast-forward merge: move base ref to branch tip.
                msg = f"recover: fast-forward {branch} into {base} (orphan commits from worktree)"
                result = subprocess.run(
                    ["git", "merge", "--ff-only", "--no-edit", branch, "-m", msg],
                    cwd=str(vault),
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    print(f"OK: fast-forwarded {branch} ({count} commit(s)) into {base}")
                    recovered += 1
                else:
                    print(f"FAIL: ff-merge {branch} → {base}: {result.stderr.strip()}")
                    nonff.append((branch, count))
        else:
            nonff.append((branch, count))

    print()
    print("=" * 60)
    print(f"Scanned:    {scanned} branch(es)")
    print(f"Clean:      {clean} (already on {base})")
    print(f"Recovered:  {recovered}{' (dry-run)' if args.dry_run else ''}")
    print(f"Non-FF:     {len(nonff)} (need human review)")
    if nonff:
        print()
        print("Non-FF branches (history diverged from base — manual merge required):")
        for branch, count in nonff:
            print(f"  {branch}  ({count} unique commit(s))")
        print()
        print("To inspect a single branch:")
        print(f"  git log {base}..<branch> --oneline")
        print("To merge manually (non-FF):")
        print(f"  git merge --no-ff <branch>")
        print("Or to cherry-pick specific commits:")
        print("  git cherry-pick <sha>")

    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
