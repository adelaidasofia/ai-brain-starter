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
  Resolves the git-dir the command would actually act on (honoring `-C <path>`,
  `--git-dir`, an inline `GIT_DIR=`, AND a `cd` earlier in the same command --
  see WHICH REPOSITORY below -- and handling a `.git` FILE that points
  elsewhere: the incident repo keeps its working tree in one place and its
  git-dir in a separate mirror directory, so a naive `<cwd>/.git` guess finds
  nothing). Then looks for:
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

WHICH REPOSITORY (added 2026-08-22, after this hook blocked the wrong repo)
  The hook read `-C`, `--git-dir` and `GIT_DIR`, but not the shell's own `cd`.
  So for the single most common shape an agent emits --

      cd /other/repo && git commit -m ...

  -- the `cd` segment was parsed, found not to be a git call, and discarded;
  the `git commit` segment then resolved against the SESSION cwd. That is the
  wrong repository, and it failed in BOTH directions:

    FALSE BLOCK  session cwd mid-rebase, `cd`-target clean -> refused a commit
                 to a repo that had nothing in flight, citing an unrelated
                 repo's rebase. Measured 2026-08-22. This is the direction that
                 does the lasting damage, because the only way past it is the
                 bypass var, so it teaches everyone to set
                 GIT_INFLIGHT_OP_BYPASS=1 reflexively -- and then the guard is
                 gone for the case it exists to catch.
    FALSE ALLOW  session cwd clean, `cd`-target mid-rebase -> the commit was
                 PERMITTED straight into a paused rebase. Measured the same
                 day. This is the 2026-07-28 incident walking through the front
                 door of the guard written to stop it.

  So the cwd is now tracked across shell segments. Rules, and why each one:
    `cd X && git ...`   -> X. `&&` runs the right side only if `cd` SUCCEEDED,
                           so X is not a guess.
    `cd X ; git ...`    -> X *and* the previous cwd. `;` runs the right side
                           whether or not `cd` worked, so the repo is genuinely
                           ambiguous; both are checked and either one being
                           mid-operation blocks. Conservative on purpose -- the
                           ambiguous case must not be the one that loses work.
    `cd X || git ...`   -> previous cwd. The right side runs only if `cd`
                           FAILED, so the cwd did not move.
    `cd X | git ...`    -> previous cwd. A pipeline component is a subshell;
                           its `cd` does not escape.
  Bare `cd` resolves to $HOME. `cd -` does NOT: tracking $OLDPWD across the
  ambiguous separators is more machinery than that shape is worth, and a wrong
  OLDPWD is a wrong repository, so it takes the fallback below.

  TRACKING IS ABANDONED (falling back to the session cwd -- i.e. exactly what
  this hook did before any of this existed, so the fallback cannot regress it)
  the moment the shell stops being readable:
    - an unquoted `( ... )` subshell anywhere in the command. A subshell scopes
      its `cd` to the group, so in `(cd /a && git commit) && git push` the push
      is back at the ORIGINAL cwd. A tracker that missed the `)` would judge
      the push against /a and could ALLOW a mutation into a stalled repo. The
      detection is quote-aware: `git commit -m "fix (bug)"` must not trip it,
      or the false block comes straight back for parenthesised messages.
    - a `cd` whose destination is not a literal path -- `cd $VAR`,
      `cd "$(...)"`, a glob, `cd -`, `popd`, a Windows drive-relative path, or
      more than one operand.
  Command substitution is NOT a subshell for this purpose: `$(cd /x)` cannot
  move the caller's cwd, and it tokenizes as one word, not a bare paren.

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
...), `-C`, `--git-dir`, `GIT_DIR`, `GIT_WORK_TREE` and a preceding `cd`, which
is the shape real sessions and agents emit. The incidents this exists for were
plain `git commit` calls, reached either directly or through a `cd`.

Fails OPEN on any error (missing git, unreadable dir, timeout). A correctness
guard that hard-fails would block every git command in the repo on a transient
hiccup; the failure mode it prevents is rare, and the cost of a false block on
every commit is not worth trading for it.

Bypass: `GIT_INFLIGHT_OP_BYPASS=1` (env or inline prefix). Document why.

BYPASS POLICY (decided 2026-08-23, MYC-3779 predicate 4). The bypass stays
self-serviceable, and every use of it is now LOUD.

  Why self-serviceable stays. The guard refuses to judge `--abort` vs
  `--continue` on purpose; that call belongs to whoever owns the operation. A
  bypass only a second party can grant would, in the one case that matters --
  an abandoned operation whose owner is gone -- leave the repo with no exit at
  all. The hook already declines to trap a repo (it never blocks the resolution
  verbs) and this is the same principle one level up.

  Why that was not enough before. MYC-3779 measured three subagents in one
  session independently setting this var. Each had verified its own worktree was
  clean and each was factually right; the guard was evaluating a DIFFERENT repo
  and was unfalsifiable from where they stood. The honest reading is that the
  scoping bug manufactured those bypasses -- which is why the fix is the
  cwd tracking above, not a harder-to-reach escape hatch. Making a correct
  guard harder to bypass would have punished the agents for the guard's error.

  What changed anyway. A bypass that leaves no trace is a comment, not a
  control. The bypass is now evaluated AFTER the repository check rather than
  before it, so the log records what it actually SUPPRESSED -- repo, marker,
  subcommand -- instead of merely that someone had the variable exported. A
  bypass that suppresses nothing writes nothing. Blocks are recorded too, so a
  guard that has gone quiet is distinguishable from one that never fires
  (a dead guard and a healthy one otherwise emit the same signal).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

BYPASS_VAR = "GIT_INFLIGHT_OP_BYPASS"
HOOK_NAME = "block-git-mutation-mid-operation"

# Fire telemetry (MYC-285). Fail-open: a missing _lib must never break a
# fail-open guard, and a telemetry error must never turn an allow into a block.
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_lib"))
    from guard_telemetry import log_fire
except Exception:  # pragma: no cover - telemetry is never load-bearing
    def log_fire(*_a, **_k):
        return

# Split a command into sequential segments on shell separators so each piece is
# evaluated independently (mirrors session-lock.py / check-cd-outside-worktree.py).
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

# Shell builtins that move the working directory. `pushd` moves it too; `popd`
# moves it back to a stack this hook never saw, so it is unresolvable by
# construction and is listed to force the fallback rather than be mistaken for
# a non-cd command.
CD_BUILTINS = {"cd", "pushd"}
UNRESOLVABLE_CD_BUILTINS = {"popd"}
CD_FLAGS = {"-L", "-P", "-e", "-@"}

# A destination containing any of these is not a literal path: the shell would
# expand it and this hook would resolve the pre-expansion text to the wrong
# directory. `$`/backtick are substitution, the rest are globs.
UNRESOLVABLE_PATH_CHARS = ("$", "`", "*", "?", "[")

# A Windows drive-relative destination. Found by the pre-push doubt-pass, not by
# a failing test. `shlex` eats the backslashes in `cd C:\repo` (it is a POSIX
# tokenizer and a backslash is an escape), leaving `C:repo` -- which `isabs`
# rejects on BOTH platforms, so it got joined onto the base, produced a
# directory that does not exist, failed the isdir check and made the hook SKIP
# the git call entirely. Skipping is the false-allow direction. Treat any
# `X:`-prefixed destination that is not already absolute as unresolvable so it
# takes the documented session-cwd fallback instead.
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# Sentinel: the segment is not a `cd` at all (distinct from a `cd` we could not
# resolve, which must abandon tracking -- collapsing the two would silently
# turn every unreadable `cd` into "cwd unchanged", the false-allow direction).
NOT_A_CD = object()

GIT_TIMEOUT_SEC = 5


def _inline_bypass(command: str, var: str) -> bool:
    """True iff `<var>=1` leads any shell segment of `command`. A quoted token
    is not a leading assignment (`echo 'X=1'` -> no)."""
    for seg in _segments(command)[0]:
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


def _segments(command: str):
    """Split `command` into sequential shell segments plus the separator between
    each pair, ignoring operators that sit INSIDE quotes.

    Returns (segments, separators); separators[i] is the one FOLLOWING
    segments[i], because `cd X && git ...` and `cd X || git ...` put the git
    call in different directories and only the separator says which.

    QUOTE AWARENESS IS LOAD-BEARING HERE, not tidiness. This started as a plain
    `re.split` on the operators, and with cwd tracking on top of it,

        echo "a ; cd /elsewhere" && git commit

    split into a phantom `cd /elsewhere` segment cut out of text the shell never
    executes. The tracker followed the phantom, the git call was judged against
    a directory that does not exist, the isdir check skipped it, and a commit
    into a PAUSED REBASE was ALLOWED. Measured 2026-08-23 during the pre-merge
    doubt-pass: on that exact command the older, cwd-blind hook returned 2 and
    the cwd-tracking one returned 0. The tracker did not merely fail to help --
    it opened a hole the naive version did not have.

    A quote-aware walk is also what `_lib/gh_merge.py` settled on for the same
    class (MYC-357: `echo '... && gh pr merge 1'` minted a real marker off a
    quoted string). Same bug, same shape, same fix.

    Segments keep their quotes, so each one is independently balanced and
    `shlex.split` succeeds on it. `&` is deliberately NOT a split point: leaving
    `cd /a & git commit` as one segment makes the `cd` unparseable, which takes
    the safe session-cwd fallback rather than a guess about backgrounding.
    """
    segs, seps, cur = [], [], []
    i, n, quote = 0, len(command), None
    while i < n:
        c = command[i]
        if quote:                        # inside quotes: copy through to the close
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < n and command[i + 1] in '"\\$`':
                cur.append(command[i + 1]); i += 2; continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c; cur.append(c); i += 1; continue
        if c == "\\" and i + 1 < n:      # escaped operator outside quotes -> literal
            cur.append(c); cur.append(command[i + 1]); i += 2; continue
        two = command[i:i + 2]
        if two in ("&&", "||"):
            segs.append("".join(cur)); seps.append(two); cur = []; i += 2; continue
        if c in (";", "\n", "|"):
            segs.append("".join(cur)); seps.append(c); cur = []; i += 1; continue
        cur.append(c); i += 1
    segs.append("".join(cur))
    return segs, seps


def _has_subshell(command: str) -> bool:
    """True iff `command` contains an UNQUOTED `(` or `)` grouping token.

    Quote-aware by construction: tokenized with `shlex` in punctuation mode, a
    paren inside a quoted word stays inside that word (`git commit -m
    "fix (bug)"` -> [..., 'fix (bug)']) while a real subshell becomes its own
    token (`(cd /a && git commit)` -> ['(', ...,  ')']). A naive substring test
    would confuse the two and re-break every parenthesised commit message.

    Command substitution deliberately does NOT count: `$(...)` tokenizes as a
    single word, and a substitution cannot move the caller's cwd anyway.

    Fails toward True (abandon tracking, resolve from the session cwd) on any
    tokenizer error -- an unbalanced quote means the shape is unreadable, and
    an unreadable shape must not drive a repository decision.
    """
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return any(tok in ("(", ")") for tok in lex)
    except (ValueError, AttributeError):
        return True


def _cd_targets(segment: str, bases):
    """Where a `cd` segment would land, relative to each of `bases`.

    Returns NOT_A_CD when the segment is not a cd, None when it is a cd whose
    destination cannot be resolved literally, else a list of absolute paths
    (one per base, since a RELATIVE `cd` means something different from each).
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()

    i = 0
    while i < len(tokens) and (
        ENV_ASSIGN_RE.match(tokens[i]) or tokens[i] in WRAPPER_PREFIXES
    ):
        i += 1
    if i >= len(tokens):
        return NOT_A_CD
    builtin = tokens[i]
    if builtin in UNRESOLVABLE_CD_BUILTINS:
        return None
    if builtin not in CD_BUILTINS:
        return NOT_A_CD

    rest = []
    seen_ddash = False
    for tok in tokens[i + 1:]:
        if not seen_ddash and tok == "--":
            seen_ddash = True
            continue
        if not seen_ddash and not rest and tok in CD_FLAGS:
            continue
        rest.append(tok)

    if not rest:
        # Bare `cd` is $HOME; bare `pushd` swaps the top of a stack we cannot see.
        if builtin == "pushd":
            return None
        home = os.path.expanduser("~")
        return [os.path.normpath(home)] if home and home != "~" else None
    if len(rest) > 1:
        return None

    dest = rest[0]
    if dest == "-":
        # $OLDPWD. Deliberately NOT resolved: tracking it correctly across the
        # ambiguous separators is more machinery than the shape is worth, and a
        # wrong OLDPWD is a wrong repository. Falls back to the session cwd.
        return None
    if any(c in dest for c in UNRESOLVABLE_PATH_CHARS):
        return None

    dest = os.path.expanduser(dest)
    if os.path.isabs(dest):
        return [os.path.normpath(dest)]
    if WINDOWS_DRIVE_RE.match(dest) or "\\" in dest:
        return None  # drive-relative / backslashed: not resolvable here
    return [os.path.normpath(os.path.join(b, dest)) for b in bases]


def _cwd_candidates_by_segment(command: str, session_cwd: str):
    """Per shell segment, the working directories a command there could run in.

    Aligned index-for-index with `_segments(command)[0]`, which is what
    `_git_invocations` walks -- the same function, so the indices cannot drift.
    Usually a single directory; more than one only for the genuinely ambiguous
    `cd X ; git ...`, where the caller checks every candidate and blocks if ANY
    is mid-operation.
    """
    segments, separators = _segments(command)

    if _has_subshell(command):
        return [[session_cwd] for _ in segments]

    out = []
    current = [session_cwd]
    tracking = True
    for idx, seg in enumerate(segments):
        out.append(list(current))
        if not tracking:
            continue

        targets = _cd_targets(seg, current)
        if targets is NOT_A_CD:
            continue
        if targets is None:
            # An unreadable `cd`: stop guessing and fall back to the session cwd
            # for the rest of the command -- byte-for-byte the behavior this
            # hook had before cwd tracking existed, so it cannot regress it.
            tracking = False
            current = [session_cwd]
            continue

        sep = separators[idx] if idx < len(separators) else None
        prev = current
        if sep == "&&":
            current = targets                      # cd must have succeeded
        elif sep in (";", "\n"):
            current = targets + [p for p in prev if p not in targets]
        else:
            current = prev                         # `||` / `|`: cd did not apply
    return out


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
    """Yield (segment_index, subcommand, args, workdir_override, gitdir_override)
    for each git call in `command`. Skips leading env assignments and transparent
    wrappers so `FOO=1 sudo git commit` is still seen as a commit.

    The segment index is what lets the caller look up the working directory a
    preceding `cd` put this call in; it indexes the same split, so it stays
    correct even when earlier segments are skipped."""
    for seg_index, seg in enumerate(_segments(command)[0]):
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
        yield seg_index, tokens[i], tokens[i + 1:], workdir_override, gitdir_override


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
        with open(os.path.join(git_dir, "HEAD"), "r", encoding="utf-8", errors="replace") as f:
            return not f.read().strip().startswith("ref:")
    except OSError:
        return False


def _block_message(subcmd: str, marker: str, desc: str, owner: str,
                   git_dir: str, workdir: str, moved: bool = False,
                   ambiguous: bool = False, session_cwd: str = "") -> str:
    resolution = (
        "  git bisect reset\n" if owner == "bisect"
        else "  git %s --abort      # or --continue -- see above, it is a real choice\n" % owner
    )
    # Say HOW this directory was arrived at whenever it is not the session cwd.
    # Without it the block reads as though it were about the session's own repo,
    # which is exactly the confusion that made the old wrong-repo block look
    # like a bug in the guard rather than a real finding about another repo.
    provenance = ""
    if moved:
        provenance += (
            f"  reached via : a `cd` earlier in this same command\n"
            f"  session cwd : {session_cwd or '(unknown)'}  <- NOT the repo above\n"
        )
    if ambiguous:
        provenance += (
            "  ambiguity   : `;` runs the next command whether or not the `cd` "
            "succeeded, so every candidate directory was checked and this one "
            "is mid-operation. Use `&&` to pin it.\n"
        )
    return (
        f"BLOCKED: `git {subcmd}` would mutate a repository that has an "
        f"operation already IN FLIGHT.\n\n"
        f"  working dir : {workdir}\n"
        f"  git-dir     : {git_dir}\n"
        f"  in flight   : {marker}  ({desc})\n"
        + provenance +
        "\n"
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
    # NOTE: the bypass is resolved here but APPLIED at the block site, so the
    # audit record can name what it suppressed. Returning early here would make
    # every bypass indistinguishable from every other -- including the ones that
    # suppressed nothing at all.
    bypass_env = os.environ.get(BYPASS_VAR) == "1"

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

    bypassed = bypass_env or _inline_bypass(command, BYPASS_VAR)

    session_cwd = payload.get("cwd") or os.getcwd()
    seg_cwds = _cwd_candidates_by_segment(command, session_cwd)
    warnings = []
    warned_git_dirs = set()

    for seg_index, subcmd, args, workdir_override, gitdir_override in _git_invocations(command):
        if subcmd not in MUTATING_SUBCOMMANDS:
            continue
        if _is_resolution(subcmd, args):
            continue  # ending the operation is exactly what should stay possible

        bases = seg_cwds[seg_index] if seg_index < len(seg_cwds) else [session_cwd]
        moved = bases != [session_cwd]
        ambiguous = len(bases) > 1
        seen_git_dirs = set()

        for base in bases:
            workdir = base
            if workdir_override:
                # An ABSOLUTE `-C` outranks the base outright -- os.path.join
                # already discards the left side for an absolute right side, so
                # every base collapses to the same target and the dedupe below
                # keeps this to one resolution.
                workdir = os.path.normpath(
                    os.path.join(base, os.path.expanduser(workdir_override))
                )
            if not os.path.isdir(workdir):
                continue

            git_dir = _resolve_git_dir(workdir, gitdir_override)
            if not git_dir or not os.path.isdir(git_dir):
                continue  # not a git repo (or unreadable) -> fail open
            if git_dir in seen_git_dirs:
                continue  # several candidate dirs, one underlying repository
            seen_git_dirs.add(git_dir)

            found = _in_flight(git_dir)
            if found:
                marker, desc, owner = found
                if bypassed:
                    # Honored -- and now on the record, with the thing it let
                    # through. This is the ONLY place a bypass is logged: one
                    # that suppressed nothing is not worth a line.
                    log_fire(HOOK_NAME, status="bypassed", subcmd=subcmd,
                             marker=marker, workdir=workdir, git_dir=git_dir,
                             via="env" if bypass_env else "inline")
                    return 0
                log_fire(HOOK_NAME, status="blocked", subcmd=subcmd,
                         marker=marker, workdir=workdir, git_dir=git_dir)
                sys.stderr.write(_block_message(
                    subcmd, marker, desc, owner, git_dir, workdir,
                    moved=moved, ambiguous=ambiguous, session_cwd=session_cwd,
                ))
                return 2

            # One warning per repository, not per candidate path that reaches it.
            if _is_detached(git_dir) and git_dir not in warned_git_dirs:
                warned_git_dirs.add(git_dir)
                warnings.append(_detached_warning(subcmd, git_dir, workdir))

    if warnings and not bypassed:
        log_fire(HOOK_NAME, status="warned", detail="detached-head")
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
