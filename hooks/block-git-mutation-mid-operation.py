#!/usr/bin/env python3
"""block-git-mutation-mid-operation.py

PreToolUse(Bash) hook. Refuse any git command that mutates history, refs, the
index or the working tree while the TARGET repository has an operation already
in flight -- a paused rebase, an unresolved merge, a stopped cherry-pick /
revert, or an active bisect.

WHY (incident 2026-07-28). A `git rebase -i` on `main` stalled
at 23:07 the night before and sat paused for 22+ hours. HEAD was detached the
whole time. Every session that day committed onto it -- ~22 commits between
12:05 and 21:09 -- and not one noticed. One session then branched from an
earlier point, and six commits from four different sessions fell off `main`.

The gap, stated precisely: `session-lock.py` already knows the word "rebase" --
it is in that hook's gated-verb list, so it stops you LAUNCHING a rebase while
a sibling session is live. It has no check for a rebase already in flight.
**The existing guard blocks the verb; nothing blocked the state.** A session was
prevented from starting a rebase, and then allowed to commit into someone
else's.

Liveness is the wrong signal and cannot be repaired. A session that starts a
rebase and then goes idle -- usage limit, closed terminal, crash -- leaves the
repo mid-operation while looking exactly like "no session" to a heartbeat
check. This hook therefore reads GIT STATE ONLY. It never consults session
liveness, and it fires the same whether zero or ten siblings are alive.

Routine pre-commit checks did not surface it either: `git status --porcelain`
and `git rev-parse --abbrev-ref HEAD` both returned survivable-looking answers
while `.git` was mid-rebase. You had to already suspect the problem to ask the
question that revealed it. That is what this hook asks, every time, for free.

WHAT IT CHECKS
  Resolves the git-dir the command would actually act on (honoring `-C <path>`
  and `--git-dir`, and handling a `.git` FILE that points elsewhere -- the
  incident repo keeps its working tree in one place and its git-dir in a
  separate mirror directory, so a naive `<cwd>/.git` guess finds nothing).
  Then looks for:
      rebase-merge/     paused interactive / merge-backend rebase
      rebase-apply/     paused am-backend rebase, or a stopped `git am`
      MERGE_HEAD        merge stopped at conflicts
      CHERRY_PICK_HEAD  cherry-pick stopped at conflicts
      REVERT_HEAD       revert stopped at conflicts
      BISECT_LOG        bisect in progress
  Any present + a mutating verb -> BLOCK (exit 2).

  In a linked worktree this deliberately uses `--git-dir`, not
  `--git-common-dir`: in-flight operation state is per-worktree, so the
  per-worktree git-dir is the correct place to look.

DETACHED HEAD gets its own, softer path (a warning, not a block) with its own
message. Committing to a plain detached HEAD is survivable -- you can recover a
commit from the reflog. Committing into a STALLED REBASE's detached HEAD is how
work leaves history, because the next `--abort` moves the branch out from under
it. Same symptom on the surface, different severity underneath; identical
messages would flatten exactly the distinction that cost six commits.

WHAT IS DELIBERATELY NOT BLOCKED
  - The RESOLUTION forms: `git rebase|merge|cherry-pick|revert|am
    --abort|--continue|--skip|--quit` and `git bisect reset`. Blocking these
    would trap the repo in the stuck state with the bypass env var as the only
    exit, which trains everyone to disable the guard. Whoever owns the
    operation must be able to end it.
  - `git tag` and `git branch`: both were the moves that actually PRESERVED
    work during the incident (a tag anchored an orphan commit; an unrelated
    session branching off it is the only reason six stranded commits survived).
    Neither touches operation state.
  - `git fetch`, `git status`, `git log` and every other read: recovery starts
    with reading.

DELIBERATELY NOT A JUDGMENT CALL THIS HOOK MAKES: whether to `--abort` or
`--continue`. In the incident, `--continue` would have force-moved `main` to a
22-hour-stale base; `--abort` was correct. That determination needs the
operation's intent, which lives with whoever started it. The hook reports the
state and stops; it never suggests a resolution verb.

KNOWN GAPS (documented, low-incidence -- stated so nobody reads this hook as
total coverage). A `git` buried inside another program's argument is not parsed:
`bash -c "git commit"`, `ssh host git commit`, a git call inside a heredoc or a
script this hook never sees. A Windows ABSOLUTE path written with backslashes
(`C:\\Program Files\\Git\\cmd\\git.exe commit`) is also missed, because the command
arrives as a Bash string and `shlex` correctly treats a backslash as an escape;
parsing it would mean guessing the quoting dialect per command, which would
break ordinary POSIX quoting for everyone to catch a form that a Bash tool
call essentially never emits. Bare `git`, `git.exe` (any case), and
forward-slash paths ARE covered, which is what Git Bash / WSL / PATH
invocations actually look like.

The parser reads leading env assignments, transparent wrappers (`env`/`sudo`/
...), `-C`, `--git-dir`, `GIT_DIR` and `GIT_WORK_TREE`, which is the shape real
sessions and agents emit. The incidents this exists for were plain `git commit`
calls.

Fails OPEN on any error (missing git, unreadable dir, timeout). A correctness
guard that hard-fails would block every git command in the repo on a transient
hiccup; the failure mode it prevents is rare, and the cost of a false block on
every commit is not worth trading for it.

Bypass: `GIT_INFLIGHT_OP_BYPASS=1` (env or inline prefix). Document why.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

BYPASS_VAR = "GIT_INFLIGHT_OP_BYPASS"

# Split a command into sequential segments on shell separators so each piece is
# evaluated independently (mirrors session-lock.py / check-cd-outside-worktree.py).
SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;\n|]")
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Transparent wrappers to skip before identifying `git`
# (`env FOO=x git commit`, `command git commit`, `sudo git commit`).
WRAPPER_PREFIXES = {"env", "command", "exec", "builtin", "nohup", "sudo", "time"}

# git subcommands that mutate history / refs / index / working tree. Kept tight
# on purpose: every entry is something that, run into a paused rebase, either
# lands work on a doomed detached HEAD or destroys the operation's state.
MUTATING_SUBCOMMANDS = {
    "commit", "push", "merge", "rebase", "reset", "checkout", "switch",
    "cherry-pick", "revert", "pull", "am", "apply", "stash", "restore",
    "clean", "rm", "mv", "gc", "prune", "filter-branch", "update-ref",
}

# Subcommands that OWN an in-flight operation and can therefore end it.
RESOLVABLE_SUBCOMMANDS = {"rebase", "merge", "cherry-pick", "revert", "am"}
RESOLUTION_FLAGS = {"--abort", "--continue", "--skip", "--quit"}

# git global options that consume the FOLLOWING token as their value.
GIT_GLOBAL_VALUE_OPTS = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
}

# Marker -> (description, the verb that OWNS this operation). The owning verb is
# tracked separately from whatever verb got blocked: the resolution hint must
# name the in-flight operation (`git rebase --abort`), never the command the
# session happened to run (`git commit --abort` is not a thing, and printing it
# would send someone chasing a nonexistent command mid-incident).
IN_FLIGHT_MARKERS = [
    ("rebase-merge", "interactive/merge-backend rebase, paused", "rebase"),
    ("rebase-apply", "rebase (am backend) or `git am`, paused", "rebase"),
    ("MERGE_HEAD", "merge stopped at conflicts", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick stopped at conflicts", "cherry-pick"),
    ("REVERT_HEAD", "revert stopped at conflicts", "revert"),
    ("BISECT_LOG", "bisect in progress", "bisect"),
    # LAST on purpose. `.git/sequencer` is the multi-commit cherry-pick/revert
    # queue, and it OUTLIVES the per-stop markers above: when a sequence stops
    # on a conflict you get CHERRY_PICK_HEAD *and* sequencer, and the entry
    # above wins with the more specific message. But once the conflict is
    # resolved and COMMITTED, git clears CHERRY_PICK_HEAD while the remaining
    # picks stay queued, leaving `sequencer` as the only evidence that the
    # operation is still in flight. Verified empirically: after
    # `git cherry-pick A B` stops on A and the resolution is committed,
    # sequencer is present and all six markers above are absent, so without
    # this entry the guard goes quiet mid-sequence and a commit lands inside
    # someone else's unfinished cherry-pick.
    ("sequencer", "multi-commit cherry-pick/revert sequence, mid-run", "cherry-pick"),
]

GIT_TIMEOUT_SEC = 5


def _inline_bypass(command: str, var: str) -> bool:
    """True iff `<var>=1` leads any shell segment of `command`. A quoted token
    is not a leading assignment (`echo 'X=1'` -> no)."""
    for seg in SEGMENT_SPLIT_RE.split(command):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        for tok in tokens:
            if not (ENV_ASSIGN_RE.match(tok) or tok in WRAPPER_PREFIXES):
                break
            if ENV_ASSIGN_RE.match(tok):
                k, _, v = tok.partition("=")
                if k == var and v == "1":
                    return True
    return False


def _is_git_exe(token: str) -> bool:
    """True iff `token` invokes git, on every platform this ships to.

    A bare `basename(token) != "git"` test is inert on Windows, where the
    command is `git.exe` (and `C:\\Program Files\\Git\\cmd\\git.exe` for an
    absolute invocation) -- the guard would silently never fire for an entire
    platform, which is worse than not shipping it there. Case-folded because
    Windows paths are case-insensitive.
    """
    base = os.path.basename(token.replace("\\", "/")).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base == "git"


def _git_invocations(command: str):
    """Yield (subcommand, args, workdir_override, gitdir_override) for each git
    call in `command`. Skips leading env assignments and transparent wrappers so
    `FOO=1 sudo git commit` is still seen as a commit."""
    for seg in SEGMENT_SPLIT_RE.split(command):
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()

        # Leading env assignments are skipped to find `git`, but GIT_DIR /
        # GIT_WORK_TREE are READ on the way past: `GIT_DIR=/other/repo git
        # commit` retargets git entirely, and a guard that resolved the repo
        # from cwd alone would clear the WRONG repository and allow the commit.
        # A false ALLOW is the failure mode that loses work, so this is worth
        # the few lines. (A GIT_DIR exported into the session env needs no
        # handling here -- the `rev-parse` subprocess inherits it already.)
        i = 0
        env_git_dir = None
        env_work_tree = None
        while i < len(tokens) and (
            ENV_ASSIGN_RE.match(tokens[i]) or tokens[i] in WRAPPER_PREFIXES
        ):
            if ENV_ASSIGN_RE.match(tokens[i]):
                k, _, v = tokens[i].partition("=")
                if k == "GIT_DIR":
                    env_git_dir = v
                elif k == "GIT_WORK_TREE":
                    env_work_tree = v
            i += 1
        if i >= len(tokens):
            continue
        if not _is_git_exe(tokens[i]):
            continue
        i += 1

        # An explicit flag outranks the env assignment, matching git itself.
        workdir_override = env_work_tree
        gitdir_override = env_git_dir
        # Walk git's global options to find the real subcommand.
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("--") and "=" in tok:
                key, _, val = tok.partition("=")
                if key == "--git-dir":
                    gitdir_override = val
                elif key == "-C":
                    workdir_override = val
                i += 1
                continue
            if tok in GIT_GLOBAL_VALUE_OPTS:
                val = tokens[i + 1] if i + 1 < len(tokens) else None
                if tok == "-C" and val:
                    workdir_override = val
                elif tok == "--git-dir" and val:
                    gitdir_override = val
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            break

        if i >= len(tokens):
            continue
        yield tokens[i], tokens[i + 1:], workdir_override, gitdir_override


def _is_resolution(subcmd: str, args) -> bool:
    """`git rebase --abort`, `git merge --continue`, `git bisect reset`, ... --
    the acts that END an in-flight operation. Never blocked."""
    if subcmd in RESOLVABLE_SUBCOMMANDS:
        return any(a in RESOLUTION_FLAGS for a in args)
    if subcmd == "bisect":
        return bool(args) and args[0] in {"reset", "bad", "good", "skip", "log", "view"}
    return False


def _resolve_git_dir(workdir: str, gitdir_override):
    """Absolute git-dir the command would act on, or None. Uses `git rev-parse
    --git-dir` (not a `.git` path guess) so a `.git` FILE pointing at a mirror,
    a linked worktree, and a bare repo all resolve correctly."""
    if gitdir_override:
        return os.path.normpath(os.path.join(workdir, os.path.expanduser(gitdir_override)))
    try:
        out = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "--git-dir"],
            capture_output=True, text=True,
            # A vault path carries non-ASCII; text=True alone decodes with the
            # console code page, so on a cp1252 Windows console reading it
            # raises UnicodeDecodeError INSIDE subprocess.run and this guard
            # dies on the very repos it protects.
            encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    path = out.stdout.strip()
    if not path:
        return None
    return os.path.normpath(os.path.join(workdir, path))


def _in_flight(git_dir: str):
    """(marker, description, owning_verb) for the first in-flight operation
    found, else None."""
    for marker, desc, owner in IN_FLIGHT_MARKERS:
        if os.path.exists(os.path.join(git_dir, marker)):
            return marker, desc, owner
    return None


def _is_detached(git_dir: str) -> bool:
    """Read git-dir/HEAD directly -- no second subprocess. Attached HEAD is
    `ref: refs/heads/<branch>`; anything else is a raw SHA, i.e. detached."""
    try:
        with open(os.path.join(git_dir, "HEAD"), "r", encoding="utf-8") as f:
            return not f.read().strip().startswith("ref:")
    except OSError:
        return False


def _block_message(subcmd: str, marker: str, desc: str, owner: str,
                   git_dir: str, workdir: str) -> str:
    resolution = (
        "  git bisect reset\n" if owner == "bisect"
        else "  git %s --abort      # or --continue -- see above, it is a real choice\n" % owner
    )
    return (
        f"BLOCKED: `git {subcmd}` would mutate a repository that has an "
        f"operation already IN FLIGHT.\n\n"
        f"  working dir : {workdir}\n"
        f"  git-dir     : {git_dir}\n"
        f"  in flight   : {marker}  ({desc})\n\n"
        "HEAD is almost certainly detached onto that operation's temporary base. "
        "A commit here does NOT land on the branch you think it does -- it lands "
        "on the in-flight state, and the next `--abort` moves the branch out from "
        "under it. That is not hypothetical: on 2026-07-28 a rebase stalled for "
        "22+ hours, ~22 commits landed on its detached HEAD, and six commits from "
        "four different sessions fell off `main` when one session branched from an "
        "earlier point.\n\n"
        "This is a GIT-STATE check, not a session check. No sibling session needs "
        "to be alive for this to be dangerous -- an abandoned rebase is exactly "
        "the case a liveness lock cannot see.\n\n"
        "DO NOT `rm -rf` the operation directory (e.g. .git/rebase-merge). It "
        "holds an AUTOSTASH -- uncommitted work parked when the operation "
        "started. Deleting the directory drops that pointer silently and takes "
        "those changes with it. `--abort` and `--continue` both restore it; a "
        "hand-delete does not.\n\n"
        "`--abort` vs `--continue` is a judgment call, and it belongs to whoever "
        "owns this operation -- not to a passing session and not to this hook. "
        "Continuing a stale rebase can force a branch back to a days-old base. "
        "Find that person, or confirm the operation is abandoned, before ending "
        "it.\n\n"
        "Safe right now, without touching the operation:\n"
        f"  git -C {workdir} status\n"
        f"  git -C {workdir} branch --contains HEAD    # says '(no branch, rebasing <x>)'\n"
        f"  git -C {workdir} tag anchor/<what-this-is> # anchors a commit you already made\n"
        "  git branch <name>                          # a pushed branch is what actually preserves work\n\n"
        "Ending the operation is NOT blocked by this hook -- these run fine, "
        "once you know which one is right:\n"
        + resolution +
        f"\nBypass (document why): {BYPASS_VAR}=1\n"
    )


def _detached_warning(subcmd: str, git_dir: str, workdir: str) -> str:
    return (
        f"WARNING: `git {subcmd}` targets a repo whose HEAD is DETACHED (no "
        f"in-flight rebase/merge/cherry-pick found, so this is the survivable "
        f"case).\n"
        f"  working dir : {workdir}\n"
        f"  git-dir     : {git_dir}\n\n"
        "A commit here belongs to no branch. It is recoverable from the reflog, "
        "but it will not appear on any branch and it is a garbage-collection "
        "candidate once the reflog entry ages out.\n\n"
        "If this is deliberate (inspecting an old commit, a bisect run), carry "
        "on. If it is not, attach to a branch BEFORE mutating:\n"
        f"  git -C {workdir} switch -c <branch>   # keep this work on a real branch\n"
        f"  git -C {workdir} switch -              # or go back where you were\n\n"
        "Not blocking -- detached HEAD alone is a normal state. The blocking "
        "case is a detached HEAD that belongs to a PAUSED operation, which is "
        "reported separately."
    )


def main() -> int:
    if os.environ.get(BYPASS_VAR) == "1":
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    if not command.strip():
        return 0

    # Cheapest possible reject: the overwhelming majority of Bash calls are not
    # git at all, and must cost nothing beyond this substring test. Case-folded
    # -- a case-sensitive test here silently skipped `GIT.EXE commit` on
    # Windows, defeating the whole hook before the parser ever ran.
    if "git" not in command.lower():
        return 0

    if _inline_bypass(command, BYPASS_VAR):
        return 0

    session_cwd = payload.get("cwd") or os.getcwd()
    warnings = []

    for subcmd, args, workdir_override, gitdir_override in _git_invocations(command):
        if subcmd not in MUTATING_SUBCOMMANDS:
            continue
        if _is_resolution(subcmd, args):
            continue  # ending the operation is exactly what should stay possible

        workdir = session_cwd
        if workdir_override:
            workdir = os.path.normpath(
                os.path.join(session_cwd, os.path.expanduser(workdir_override))
            )
        if not os.path.isdir(workdir):
            continue

        git_dir = _resolve_git_dir(workdir, gitdir_override)
        if not git_dir or not os.path.isdir(git_dir):
            continue  # not a git repo (or unreadable) -> fail open

        found = _in_flight(git_dir)
        if found:
            marker, desc, owner = found
            sys.stderr.write(
                _block_message(subcmd, marker, desc, owner, git_dir, workdir)
            )
            return 2

        if _is_detached(git_dir):
            warnings.append(_detached_warning(subcmd, git_dir, workdir))

    if warnings:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "\n\n".join(warnings),
            }
        }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail open, always. Blocking every git command in a repo because this
        # guard hit an unexpected input is a worse outcome than the rare miss.
        sys.exit(0)
