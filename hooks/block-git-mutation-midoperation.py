#!/usr/bin/env python3
"""block-git-mutation-midoperation.py

PreToolUse(Bash) hook. Refuse any git-MUTATING command when the target
repository is mid-operation — a rebase, merge, cherry-pick, revert, am, or
bisect that was started and never finished.

Why (bug class GIT-MUTATION-INTO-STALLED-OPERATION):

A `git rebase -i` stalls between picks. HEAD is left detached. Nothing in the
routine pre-commit path notices: `git status` says "rebase in progress" but no
one reads it, `git rev-parse --abbrev-ref HEAD` says `HEAD`, the ahead/behind
count looks survivable, and a liveness lock keyed on session heartbeats sees
nothing at all because the session that started the rebase is idle or gone.
Every subsequent session commits onto the detached HEAD. Those commits belong
to no branch. When someone eventually branches off an older commit and moves
on, everything committed in between silently leaves history.

Observed 2026-07-27/28: a rebase stalled after 1 of 2 picks and sat for 22+
hours while ~22 commits from many concurrent sessions landed on the detached
HEAD. Nine commits from five sessions ended up on no remote branch; one was
one `git rebase --abort` away from being orphaned entirely, because `--abort`
resets the rebase's `head-name` back to `orig-head` and a later session had
committed directly onto that branch in the meantime.

The pre-existing guard blocked the VERB (you may not START a rebase while a
sibling is live) but never the STATE (you may freely commit into someone
else's). This is the state layer.

Design notes that matter:

  - **The exits are always allowed.** `--abort`, `--quit`, `--continue`,
    `--skip` on rebase/merge/cherry-pick/revert/am are how you LEAVE the
    stalled state. A guard that blocks them traps the operator inside the
    condition it is complaining about. Same for `git status` and every other
    read.
  - **Per-worktree, not per-repo.** Rebase state for the main worktree lives
    in `$GIT_COMMON_DIR`; for a linked worktree it lives in
    `$GIT_DIR/worktrees/<name>`. `git rev-parse --git-dir` returns the correct
    per-worktree directory, so a worktree session is not blocked by an
    unrelated stall in the main checkout (worktrees are HEAD-isolated).
  - **`git -C <path>` is resolved.** That form is the documented way a
    worktree session reaches the main checkout, so the guard must follow it to
    the real target rather than checking the session cwd.

Bypass: `GIT_MIDOP_BYPASS=1` (document why — e.g. deliberately committing a
conflict resolution as part of finishing the operation).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

# Commands that write to refs, the index, or the working tree. A stalled
# operation makes every one of these land somewhere other than where the
# caller believes it will.
MUTATING = {
    "commit", "merge", "rebase", "reset", "checkout", "switch", "cherry-pick",
    "revert", "am", "apply", "restore", "pull", "push", "stash", "clean",
    "branch", "tag", "gc", "prune", "filter-branch",
}

# Reads are always safe, even mid-operation. Listed explicitly so a new verb
# defaults to "not mutating" only if it is genuinely absent from MUTATING.
READ_ONLY = {
    "status", "log", "diff", "show", "rev-parse", "rev-list", "cat-file",
    "for-each-ref", "merge-base", "cherry", "ls-remote", "ls-files", "blame",
    "describe", "shortlog", "reflog", "grep", "config", "remote", "fetch",
    "worktree", "version", "help", "bisect",
}

# Subcommand flags that RESOLVE a stalled operation. Never block these.
RESOLVER_FLAGS = {"--abort", "--quit", "--continue", "--skip"}
RESOLVABLE_VERBS = {"rebase", "merge", "cherry-pick", "revert", "am"}

# Read-only forms of otherwise-mutating verbs.
READ_ONLY_SUBCOMMANDS = {
    ("stash", "list"), ("stash", "show"), ("branch", "--list"),
    ("branch", "-l"), ("branch", "--contains"), ("tag", "-l"),
    ("tag", "--list"), ("remote", "-v"),
}

# Marker -> human description. Order matters only for message clarity.
MARKERS = [
    ("rebase-merge", "an interactive/merge rebase"),
    ("rebase-apply", "a rebase (am/apply backend)"),
    ("MERGE_HEAD", "an unfinished merge"),
    ("CHERRY_PICK_HEAD", "an unfinished cherry-pick"),
    ("REVERT_HEAD", "an unfinished revert"),
    ("sequencer", "a multi-commit cherry-pick/revert sequence"),
    ("BISECT_LOG", "a bisect session"),
]

SEGMENT_SPLIT_RE = re.compile(r"(?:\|\||&&|\||;|\n)")
ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=[^\s]*\s+")


def _inline_bypass(command: str, var: str) -> bool:
    """Detect `VAR=1 git ...` — the session env cannot see a command-local prefix."""
    return re.search(rf"(^|\s){re.escape(var)}=1(\s|$)", command) is not None


def _strip_env(seg: str) -> str:
    while True:
        new = ENV_PREFIX_RE.sub("", seg)
        if new == seg:
            return seg
        seg = new


def _git_dir(cwd: str) -> str | None:
    """Resolve the PER-WORKTREE git dir for cwd, or None if not a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    path = out.stdout.strip()
    return path or None


def _in_progress(git_dir: str) -> list[str]:
    return [desc for name, desc in MARKERS if os.path.exists(os.path.join(git_dir, name))]


def _detached(cwd: str) -> bool:
    try:
        out = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode != 0


def _analyze(seg: str, cwd: str) -> tuple[str, str] | None:
    """Return (verb, target_cwd) if seg is a mutating git command, else None."""
    seg = _strip_env(seg.strip())
    if not seg:
        return None
    try:
        parts = shlex.split(seg)
    except ValueError:
        parts = seg.split()
    if not parts or os.path.basename(parts[0]) != "git":
        return None

    target = cwd
    i = 1
    verb = None
    while i < len(parts):
        tok = parts[i]
        # follow `git -C <path>` to the real target repo
        if tok == "-C" and i + 1 < len(parts):
            cand = os.path.expanduser(parts[i + 1])
            target = cand if os.path.isabs(cand) else os.path.normpath(os.path.join(cwd, cand))
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        verb = tok
        break
    if verb is None:
        return None

    rest = parts[i + 1:]

    # The exits out of a stalled operation are never blocked.
    if verb in RESOLVABLE_VERBS and any(f in rest for f in RESOLVER_FLAGS):
        return None
    if verb in READ_ONLY:
        return None
    for v, sub in READ_ONLY_SUBCOMMANDS:
        if verb == v and sub in rest:
            return None
    if verb not in MUTATING:
        return None
    return verb, target


def main() -> int:
    if os.environ.get("GIT_MIDOP_BYPASS") == "1":
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0

    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    if not command.strip() or "git" not in command:
        return 0
    if _inline_bypass(command, "GIT_MIDOP_BYPASS"):
        return 0

    cwd = payload.get("cwd") or os.getcwd()

    for seg in SEGMENT_SPLIT_RE.split(command):
        found = _analyze(seg, cwd)
        if not found:
            continue
        verb, target = found
        if not os.path.isdir(target):
            continue
        git_dir = _git_dir(target)
        if not git_dir:
            continue

        states = _in_progress(git_dir)
        if states:
            sys.stderr.write(
                f"BLOCKED: `git {verb}` into a repository that is MID-OPERATION.\n"
                f"  repo git-dir: {git_dir}\n"
                f"  in progress:  {', '.join(states)}\n\n"
                "A stalled operation leaves HEAD detached. A commit made now "
                "belongs to no branch, and vanishes from history the moment "
                "anyone branches off an earlier commit and moves on. This is "
                "how nine commits from five sessions left `main` on "
                "2026-07-27/28.\n\n"
                "Finish or clear the operation first, in that repo:\n"
                "  git -C <repo> status              # see what stalled\n"
                "  git -C <repo> rebase --continue   # finish it, or\n"
                "  git -C <repo> rebase --quit       # clear state, keep HEAD/worktree, or\n"
                "  git -C <repo> rebase --abort      # rewind to orig-head\n\n"
                "Before choosing --abort, check what it rewinds: it resets the "
                "rebase's head-name branch to orig-head, which DROPS anything "
                "committed onto that branch since the rebase began. Anchor "
                "first: `git -C <repo> tag recover/<name> <sha>`.\n"
                "NEVER `rm -rf .git/rebase-merge` — it silently discards the "
                "autostash pointer and the uncommitted work it holds.\n\n"
                "Bypass (document why): GIT_MIDOP_BYPASS=1\n"
            )
            return 2

        # Detached HEAD without a stalled operation: survivable, but a commit
        # here still belongs to no branch. Warn without blocking.
        if verb == "commit" and _detached(target):
            sys.stderr.write(
                "NOTE: HEAD is DETACHED in "
                f"{target} — this commit will belong to no branch.\n"
                "If that is not deliberate, `git -C <repo> switch <branch>` "
                "first, or anchor it afterwards with a tag/branch so it "
                "cannot be garbage-collected.\n"
            )
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
