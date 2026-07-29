#!/usr/bin/env python3
"""check-cd-outside-worktree.py — the PreToolUse(Bash) git-safety gate.

Two checks, one process. Both answer the same question ("will this Bash command
put git work at risk?"), and PreToolUse:Bash is the hottest matcher in the
fleet, so they share one cold start rather than two (footprint SLA, MYC-2358).
The filename predates CHECK A and is kept deliberately: it is the identity the
installer fingerprints and the wiring regression test drives from hooks.json.

  CHECK A — git mutation into a repo that is MID-OPERATION (rebase/merge/
    cherry-pick/revert/am/bisect left unfinished). Applies to EVERY session.
    See the block above _check_midoperation. Bypass: GIT_MIDOP_BYPASS=1.
  CHECK B — `cd` out of a worktree into the shared main checkout. Applies only
    to worktree-rooted sessions. Documented below. Bypass: WORKTREE_CD_BYPASS=1.

CHECK B, in detail. When the session is rooted in a git worktree
(`<repo>/.claude/worktrees/<slug>/...`), block any `cd` / `pushd` whose
resolved target lands in the MAIN checkout tree but OUTSIDE the worktrees
subtree.

Why: CONCURRENT-SESSION-HEAD-DRIFT. A worktree has its own HEAD; the main
checkout has a different HEAD shared with every other main-checkout session.
`cd` into the main checkout from a worktree-spawned session, then a commit /
branch op lands on whatever branch the MAIN checkout (or a parallel session)
currently points at — not the worktree's `claude/<slug>` branch. Observed
repeatedly in long multi-session workflows (a ~30-minute reflog rescue in one
real incident) despite a codified worktree-isolation rule. The rule existed; the
enforcement layer did not. This is that layer.

Detection:
  - cwd (from payload) matches `<main>/.claude/worktrees/<slug>` → worktree
    session; `<main>` is the shared main checkout.
  - Each `cd`/`pushd` target in the command is resolved against cwd
    (absolute targets ignore cwd; relative `..` climbs resolve too).
  - Block if the resolved target is `<main>` itself OR under `<main>/` but NOT
    under `<main>/.claude/worktrees/` (staying anywhere inside the worktrees
    subtree is fine; that's still HEAD-isolated).

Recommended instead: stay in the worktree, or use `git -C <path> ...` for
read-only queries against the main checkout (no cwd change, no drift).

Known gaps (documented, low-incidence): `cd` nested inside `bash -c "..."` or
other quoting layers is not parsed; env-var targets beyond `~`/`$HOME` are not
expanded. The real-world incident that motivated this was a bare absolute
`cd`, which this catches.

Bypass: `WORKTREE_CD_BYPASS=1` (document why — e.g. a deliberate one-off
read-only op in the main checkout).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

WORKTREE_RE = re.compile(r"^(?P<main>.+?)/\.claude/worktrees/(?P<slug>[^/]+)(?:/|$)")

# Split a command into sequential segments on shell separators so each `cd`
# is evaluated in order (mirrors how the shell would run them).
SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;\n|]")
CD_RE = re.compile(r"^(?:cd|pushd)\s+(?P<arg>.+)$")

# --- inline-bypass + leading-env handling (self-contained) -------------------
# A PreToolUse(Bash) gate runs in the hook process, whose env is the SESSION
# env. An inline `WORKTREE_CD_BYPASS=1 cd ...` prefix lives only in the command
# STRING, so the bypass this hook advertises must be read from the command, not
# os.environ alone (bug class HOOK-READS-SESSION-ENV-NOT-COMMAND-ENV). These
# helpers are LOCAL on purpose: a shared ~/.claude/hooks/_lib/ module would be
# co-owned by multiple installers (last-writer-wins) and could be clobbered by a
# version missing one of these functions, silently making the fix inert.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPER_PREFIXES = {"env", "command", "exec", "builtin", "nohup", "sudo", "time"}
# Leading `VAR=value` (quote-aware value) + wrapper tokens at a segment start.
_LEADING_ENV_PREFIX_RE = re.compile(
    r'^\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|\'[^\']*\'|\S+)'
    r'|env|command|exec|builtin|nohup|sudo|time)\s+)*'
)


def _inline_bypass(command, var):
    """True iff a `<var>=1` assignment leads any shell segment of `command`
    (`WORKTREE_CD_BYPASS=1 cd x`, `ls && WORKTREE_CD_BYPASS=1 cd x`). A token
    inside quotes is not a leading assignment (`echo 'X=1'` -> no)."""
    for seg in SEGMENT_SPLIT_RE.split(command):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        i = 0
        while i < len(tokens) and (_ENV_ASSIGN_RE.match(tokens[i])
                                   or tokens[i] in _WRAPPER_PREFIXES):
            if _ENV_ASSIGN_RE.match(tokens[i]):
                k, _, v = tokens[i].partition("=")
                if k == var and v == "1":
                    return True
            i += 1
    return False


def _strip_leading_env(segment):
    """`segment` with any leading `VAR=value` / wrapper prefix removed, remainder
    verbatim. Lets `^cd` see an env-prefixed cd — `FOO=1 cd /main` otherwise
    slips past `^cd` undetected (a HEAD-drift false-negative)."""
    return _LEADING_ENV_PREFIX_RE.sub("", segment, count=1)


def _first_token(arg: str) -> str:
    """First shell word of a cd argument, respecting simple quoting."""
    arg = arg.strip()
    if not arg:
        return ""
    if arg[0] in ("'", '"'):
        q = arg[0]
        end = arg.find(q, 1)
        return arg[1:end] if end != -1 else arg[1:]
    # unquoted: up to first whitespace
    return arg.split()[0]


def _expand(path: str) -> str:
    if path == "~" or path.startswith("~/"):
        path = os.path.expanduser(path)
    elif path.startswith("$HOME"):
        path = os.environ.get("HOME", os.path.expanduser("~")) + path[len("$HOME"):]
    return path


# --- CHECK A: git-mutating command into a repo that is MID-OPERATION --------
# Merged into this hook rather than shipped as its own PreToolUse(Bash) entry:
# both checks answer one question ("will this Bash command put git work at
# risk?"), and one process is one cold start instead of two on the hottest
# matcher in the fleet (the footprint SLA gate, MYC-2358, counts per-tool
# fan-out and this event is at its ceiling). Reuses this file's quote-aware
# _inline_bypass/_strip_leading_env rather than re-deriving weaker copies.
#
# Bug class GIT-MUTATION-INTO-STALLED-OPERATION. A `git rebase -i` that stalls
# between picks leaves HEAD detached, and nothing in the routine pre-commit path
# notices: `git status` says so but no one reads it, `rev-parse --abbrev-ref
# HEAD` says `HEAD`, and a liveness lock keyed on session heartbeats sees
# nothing because the session that started the rebase is idle or gone.
# Concurrent sessions then commit onto the detached HEAD, and those commits
# belong to no branch — they leave history the moment anyone branches off an
# earlier commit. Observed 2026-07-27/28: a rebase stalled 22+ hours while ~22
# commits landed on the detached HEAD; nine commits from five sessions ended up
# on no remote branch.
#
# The pre-existing session lock gated the VERB (no starting a rebase while a
# sibling is live) and never the STATE, so a session was stopped from starting a
# rebase and then allowed to commit into someone else's.
MUTATING = {
    "commit", "merge", "rebase", "reset", "checkout", "switch", "cherry-pick",
    "revert", "am", "apply", "restore", "pull", "push", "stash", "clean",
    "branch", "tag", "gc", "prune", "filter-branch",
}
READ_ONLY = {
    "status", "log", "diff", "show", "rev-parse", "rev-list", "cat-file",
    "for-each-ref", "merge-base", "cherry", "ls-remote", "ls-files", "blame",
    "describe", "shortlog", "reflog", "grep", "config", "remote", "fetch",
    "worktree", "version", "help", "bisect",
}
# Flags that RESOLVE a stalled operation. NEVER blocked: a guard that blocks the
# exits traps the operator inside the state it is flagging.
RESOLVER_FLAGS = {"--abort", "--quit", "--continue", "--skip"}
RESOLVABLE_VERBS = {"rebase", "merge", "cherry-pick", "revert", "am"}
READ_ONLY_SUBCOMMANDS = {
    ("stash", "list"), ("stash", "show"), ("branch", "--list"),
    ("branch", "-l"), ("branch", "--contains"), ("tag", "-l"),
    ("tag", "--list"), ("remote", "-v"),
}
MIDOP_MARKERS = [
    ("rebase-merge", "an interactive/merge rebase"),
    ("rebase-apply", "a rebase (am/apply backend)"),
    ("MERGE_HEAD", "an unfinished merge"),
    ("CHERRY_PICK_HEAD", "an unfinished cherry-pick"),
    ("REVERT_HEAD", "an unfinished revert"),
    ("sequencer", "a multi-commit cherry-pick/revert sequence"),
    ("BISECT_LOG", "a bisect session"),
]


def _git_dir(cwd: str):
    """PER-WORKTREE git dir for cwd, or None. Per-worktree matters: rebase state
    for the main worktree lives in $GIT_COMMON_DIR, for a linked worktree in
    $GIT_DIR/worktrees/<name>, so a worktree session is not blocked by an
    unrelated stall in the main checkout."""
    try:
        out = subprocess.run(["git", "rev-parse", "--absolute-git-dir"],
                             cwd=cwd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _analyze_git(seg: str, cwd: str):
    """(verb, target_cwd) if seg is a MUTATING git command, else None."""
    seg = _strip_leading_env(seg.strip()).strip()
    if not seg:
        return None
    try:
        parts = shlex.split(seg)
    except ValueError:
        parts = seg.split()
    if not parts or os.path.basename(parts[0]) != "git":
        return None

    target, i, verb = cwd, 1, None
    while i < len(parts):
        tok = parts[i]
        if tok == "-C" and i + 1 < len(parts):  # follow to the real target repo
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


def _check_midoperation(cwd: str, command: str) -> int:
    if "git" not in command:
        return 0
    for seg in SEGMENT_SPLIT_RE.split(command):
        found = _analyze_git(seg, cwd)
        if not found:
            continue
        verb, target = found
        if not os.path.isdir(target):
            continue
        git_dir = _git_dir(target)
        if not git_dir:
            continue
        states = [d for n, d in MIDOP_MARKERS if os.path.exists(os.path.join(git_dir, n))]
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
        return 0
    return 0


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    cwd = payload.get("cwd") or os.getcwd()
    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    if not command.strip():
        return 0

    # --- CHECK A: repo mid-operation ----------------------------------------
    # Runs for EVERY session, worktree-rooted or not — the incident this guards
    # happened in the shared MAIN checkout, so gating it behind the worktree
    # test below would miss the exact case it exists for.
    if (os.environ.get("GIT_MIDOP_BYPASS") != "1"
            and not _inline_bypass(command, "GIT_MIDOP_BYPASS")):
        rc = _check_midoperation(cwd, command)
        if rc:
            return rc

    # --- CHECK B: `cd` out of a worktree into the shared main checkout -------
    if os.environ.get("WORKTREE_CD_BYPASS") == "1":
        return 0

    m = WORKTREE_RE.match(cwd)
    if not m:
        return 0  # not a worktree-rooted session

    main_root = os.path.normpath(m.group("main"))
    slug = m.group("slug")
    worktrees_prefix = main_root + "/.claude/worktrees/"

    # Inline `WORKTREE_CD_BYPASS=1 cd ...` prefix: the session-env check at the
    # top can't see a prefix that lives only in the command string.
    if _inline_bypass(command, "WORKTREE_CD_BYPASS"):
        return 0

    for seg in SEGMENT_SPLIT_RE.split(command):
        # Strip any leading `VAR=val` / wrapper prefix so an env-prefixed cd is
        # still detected — `^cd` alone misses `FOO=1 cd /main` (a real
        # HEAD-drift false-negative). The WORKTREE_CD_BYPASS prefix is already
        # honored above; any OTHER env-prefixed cd into main must still block.
        seg = _strip_leading_env(seg.strip()).strip()
        cm = CD_RE.match(seg)
        if not cm:
            continue
        target = _first_token(cm.group("arg"))
        if not target:
            continue
        target = _expand(target)
        # resolve relative targets against the session cwd; absolute targets
        # make os.path.join ignore cwd.
        resolved = os.path.normpath(os.path.join(cwd, target))

        in_main_tree = resolved == main_root or resolved.startswith(main_root + "/")
        in_worktrees = resolved.startswith(worktrees_prefix)
        if in_main_tree and not in_worktrees:
            sys.stderr.write(
                "BLOCKED: this session is rooted in a git worktree\n"
                f"  {cwd}\n"
                f"but the command `cd`s into the MAIN checkout:\n"
                f"  {resolved}\n\n"
                "The main checkout shares one .git/HEAD with every other "
                "main-checkout session. A commit or branch op after this cd "
                "lands on the wrong branch (CONCURRENT-SESSION-HEAD-DRIFT — "
                "a ~30-minute reflog rescue in a real multi-session "
                "incident).\n\n"
                "Do instead:\n"
                f"  - stay in the worktree ({slug}); or\n"
                "  - use `git -C <path> <cmd>` for read-only queries against "
                "the main checkout (no cwd change, no drift).\n\n"
                "Bypass (document why): WORKTREE_CD_BYPASS=1\n"
            )
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
