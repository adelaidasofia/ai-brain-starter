#!/usr/bin/env python3
"""session-lock.py

Sibling-session coordination lock for git repos. Three event modes, one
shared lock file: `<main_root>/.claude/.session-lock.json` (shared across
every worktree of the repo via `git rev-parse --git-common-dir`).

SessionStart
    Resolve the MAIN repo root for the session's cwd, read the lock, and if a
    DIFFERENT session was active within the last 5 minutes, warn (informational
    additionalContext). Then upsert THIS session's own entry.

PreToolUse(Bash)  [the enforcement layer]
    Read the lock via this session's cached lock-path pointer (no git on the
    hot path). If a different session is live (<5 min):
      * git-mutating command (commit/push/checkout/switch/branch-create/
        merge/rebase/reset/cherry-pick/revert/pull/am/apply) targeting THIS
        repo -> warn-block EVERY time (exit 2). These are the ops that caused
        the real collision.
      * any other command -> warn-block ONCE per session (exit 2), then stay
        quiet so intentional parallel read-only work isn't nagged.
    Either way, refresh this session's last_activity_at (keep the slot warm).

Stop / any other invocation
    Activity heartbeat: bump last_activity_at on this session's entry.

SCOPE LIMIT -- THIS HOOK GATES THE VERB, NOT THE REPO'S STATE
    `rebase` appears in the gated-verb list above, and that has been misread as
    "rebases are covered." It is not. This hook stops you STARTING a rebase
    while a sibling is live. It has NO idea whether one is ALREADY IN FLIGHT.

    Worse, its signal is LIVENESS, which structurally cannot see the dangerous
    case: a session that starts a rebase and then dies (usage limit, closed
    terminal, crash) leaves the repo mid-operation while looking exactly like
    "no session" to a heartbeat check. Incident 2026-07-28: a rebase stalled
    22+ hours, ~22 commits from several sessions landed on its detached HEAD,
    and six commits from four sessions fell off `main`. This hook was working
    correctly the entire time -- a session was stopped from STARTING a rebase,
    then allowed to commit into someone else's.

    In-flight operation state (rebase-merge/, rebase-apply/, MERGE_HEAD,
    CHERRY_PICK_HEAD, REVERT_HEAD, BISECT_LOG) is covered by a separate,
    liveness-independent gate: hooks/block-git-mutation-mid-operation.py.
    Do not add state checks here -- the two signals have different lifetimes
    and merging them would make this hook's liveness cache authoritative over
    facts that outlive any session.

WHY
---
Worktree isolation (per-session git worktrees) prevents the shared-HEAD
collision structurally, but two sessions can still pick the SAME repo and
clobber each other's in-flight work (SIBLING-SESSION-PARALLEL-COMMIT-COLLISION:
a sibling commits broken state + reverts, wiping in-flight work). The
SessionStart alert is INFORMATIONAL only; it doesn't stop the collision once
both sessions are running. The PreToolUse layer is the gate that actually
blocks the dangerous op, in time, with an explicit bypass.

The lock is a multi-session MAP (every live session keeps its own entry) so
BOTH siblings detect each other — a single-entry lock only lets the
earlier-started session see the later one. Concurrent writers serialize through
a best-effort `flock` sidecar (`.session-lock.lock`) so the read-modify-write
doesn't lose entries when many sessions hammer one repo; the flock is
non-blocking + bounded + fail-open, so it never hangs a tool call.

LIVENESS is `last_activity_at` ONLY. The `pid` field is INFORMATIONAL — it is
the ephemeral hook-process pid (each hook invocation is a fresh, instantly-
exiting process), NOT the long-lived session pid, so it is dead on arrival and
MUST NOT anchor any liveness check. A crashed session stops being "live" after
5 min (no more heartbeats) and is pruned after 30 min.

Never HARD-blocks: every block is bypassable, and only same-repo siblings are
ever gated. Different repos worked in parallel have different lock files and
never interfere. This is a coordination nudge, not a hard control.

Lock + sidecar + tmp files should be gitignored globally (e.g.
`~/.config/git/ignore`) so the per-Bash-call writes never dirty a repo or get
committed. See docs/HOOKS_INSTALL.md for the opt-in install + gitignore step.

Bypass: SIBLING_SESSION_LOCK_BYPASS=1 (intentional parallel / collaborative
work — set it in the session's environment, not as a per-command prefix; like
every other hook bypass it is read from the hook process's env).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

try:
    import fcntl  # POSIX only; absent on Windows.
except ImportError:  # pragma: no cover
    fcntl = None

BYPASS_ENV = "SIBLING_SESSION_LOCK_BYPASS"
WARN_WINDOW_SEC = 300       # a sibling is "live" if active within 5 min
IDLE_EXPIRE_SEC = 1800      # prune entries idle > 30 min (stale-lock safety valve)
CACHE_TTL_SEC = 604_800     # prune per-session cache pointers older than 7 days
GIT_TIMEOUT_SEC = 6
SCHEMA_VERSION = 2
FLOCK_RETRIES = 20          # 20 * 10ms = 200ms max wait, then fail-open (unlocked)
FLOCK_SLEEP_SEC = 0.01
# Test-only determinism knob. When set, acquire the sidecar lock with a BLOCKING
# flock (LOCK_EX, no LOCK_NB) instead of the bounded non-blocking retry below, so
# the read-modify-write serializes with ZERO fail-open. Production NEVER sets this:
# the default bounded + fail-open path is the production contract (a stuck holder
# must never hang a real tool call). A blocking acquire is safe here because flock
# auto-releases on process exit, so it can only ever wait for siblings' tiny
# critical sections — the concurrency test's subprocess wait-timeout is the backstop
# against any pathology. Lets the concurrency test assert FULL serialization (every
# entry survives) without the 200ms liveness budget — an anti-hang valve, NOT a
# serialization property — flaking the assertion under CI load.
FLOCK_BLOCKING_ENV = "SESSION_LOCK_BLOCKING"
CACHE_DIR = os.path.expanduser("~/.claude/.cache/session-lock")
CONTINUE = '{"continue":true,"suppressOutput":true}'

# Split a command into sequential segments on shell separators so each piece is
# evaluated independently (mirrors check-cd-outside-worktree.py).
SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;\n|]")
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Working-tree root extracted from a cwd path: `<main>/.claude/worktrees/<slug>`.
WORKTREE_KEY_RE = re.compile(r"^(.+?/\.claude/worktrees/[^/]+)")

# Transparent command wrappers to skip before identifying `git` (flagless forms;
# e.g. `env FOO=x git commit`, `command git commit`, `sudo git commit`).
WRAPPER_PREFIXES = {"env", "command", "exec", "builtin", "nohup", "sudo"}

# git subcommands that mutate HEAD / index / refs / working tree.
ALWAYS_MUTATING = {
    "commit", "push", "checkout", "switch", "merge", "rebase", "reset",
    "cherry-pick", "revert", "pull", "am", "apply",
}
# git global options that consume the FOLLOWING token as their value.
GIT_GLOBAL_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

# ---- `git commit` option tables (for the scoped-commit exemption) -----------
# The parse has ONE catastrophic failure mode: mistaking an option's VALUE for a
# pathspec. `git commit -o -m msg` would then look like "-o plus the pathspec
# 'msg'" and be ALLOWED while naming no paths at all -- a false-ALLOW, strictly
# worse than the false-block this exemption exists to remove. So every option is
# classified, and anything UNCLASSIFIED forfeits the exemption rather than being
# guessed at.

# Consume the FOLLOWING token as their value when written without `=`.
COMMIT_LONG_VALUE_OPTS = {
    "--message", "--file", "--author", "--date", "--template", "--cleanup",
    "--reedit-message", "--reuse-message", "--fixup", "--squash", "--trailer",
    "--pathspec-from-file",
}
# Consume nothing. (Options with an OPTIONAL value -- `--gpg-sign`,
# `--untracked-files` -- only accept it attached with `=`, so bare they are
# value-less and the `=` form is split off before lookup.)
COMMIT_LONG_BOOL_OPTS = {
    "--only", "--all", "--include", "--amend", "--interactive", "--patch",
    "--signoff", "--no-signoff", "--verify", "--no-verify", "--edit", "--no-edit",
    "--allow-empty", "--allow-empty-message", "--no-post-rewrite", "--reset-author",
    "--short", "--branch", "--no-branch", "--porcelain", "--long", "--null",
    "--dry-run", "--status", "--no-status", "--verbose", "--quiet",
    "--pathspec-file-nul", "--gpg-sign", "--no-gpg-sign", "--untracked-files",
    "--ahead-behind", "--no-ahead-behind",
}
COMMIT_SHORT_VALUE = set("mFcCt")   # -m <msg>, -F <file>, -c/-C <commit>, -t <file>
COMMIT_SHORT_BOOL = set("ensvqz")   # -e -n -s -v -q -z
COMMIT_SHORT_OPTARG = set("uS")     # -u[<mode>], -S[<keyid>]: value only ever attached

# Flags that make a commit take content BEYOND the named pathspecs, so they defeat
# the whole point of the exemption. `--amend` is here on measurement, not theory:
# `git commit -o --amend` with no paths took the staged index (a file staged by
# another process was swept into the amended commit), and it rewrites a HEAD a
# sibling session may already have built on.
COMMIT_UNSAFE_LONG = {"--all", "--include", "--amend", "--interactive", "--patch"}
COMMIT_UNSAFE_SHORT = set("aip")    # -a (all), -i (include), -p (patch)


def _emit(obj):
    sys.stdout.write(json.dumps(obj))


def _emit_continue():
    sys.stdout.write(CONTINUE)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return None


def _atomic_write_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _git_common_dir(cwd):
    """Absolute path to the shared git dir, or '' if cwd is not a git repo."""
    for args in (
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],  # git >= 2.31
        ["rev-parse", "--git-common-dir"],
    ):
        try:
            r = subprocess.run(
                ["git", "-C", cwd] + args,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_SEC,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""
        if r.returncode == 0 and r.stdout.strip():
            out = r.stdout.strip()
            if not os.path.isabs(out):
                out = os.path.normpath(os.path.join(cwd, out))
            return out
    return ""


def _main_root(cwd):
    """Main checkout root shared by all worktrees of the repo, or '' if not a repo."""
    common = _git_common_dir(cwd)
    if not common:
        return ""
    common = common.rstrip("/")
    # common is typically <main_root>/.git ; for a bare repo it is the repo dir.
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return common


def _lock_path_for(main_root):
    return os.path.join(main_root, ".claude", ".session-lock.json")


def _main_root_from_lock_path(lock_path):
    """Inverse of _lock_path_for: <main_root>/.claude/.session-lock.json -> <main_root>."""
    return os.path.dirname(os.path.dirname(lock_path))


def _safe_id(session_id):
    return "".join(c for c in session_id if c.isalnum() or c in "-_") or "unknown"


def _cache_path(session_id):
    return os.path.join(CACHE_DIR, f"{_safe_id(session_id)}.path")


def _warned_marker_path(session_id):
    return os.path.join(CACHE_DIR, f"{_safe_id(session_id)}.warned")


def _prune_cache():
    """Delete per-session cache pointers / markers older than CACHE_TTL_SEC."""
    try:
        now = time.time()
        for name in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, name)
            try:
                if os.path.isfile(p) and (now - os.path.getmtime(p)) > CACHE_TTL_SEC:
                    os.remove(p)
            except OSError:
                continue
    except (FileNotFoundError, OSError):
        return


# ---- multi-session lock map ------------------------------------------------

def _load_sessions(lock):
    """Normalize any lock-file shape into {session_id: entry}.

    Handles v2 (``{"version":2,"sessions":{...}}``), legacy v1
    (``{"session_id":...,"last_activity_at":...}``), and missing/garbage.
    """
    if not isinstance(lock, dict):
        return {}
    sessions = lock.get("sessions")
    if isinstance(sessions, dict):
        return {k: v for k, v in sessions.items() if isinstance(v, dict)}
    # legacy single-entry (v1)
    sid = lock.get("session_id")
    if sid:
        return {sid: {
            "started_at": lock.get("started_at"),
            "last_activity_at": lock.get("last_activity_at"),
            "cwd": lock.get("cwd"),
            "pid": lock.get("pid"),
        }}
    return {}


def _prune_sessions(sessions, now):
    """Drop entries with no/old last_activity_at (idle > IDLE_EXPIRE_SEC)."""
    out = {}
    for sid, e in sessions.items():
        la = e.get("last_activity_at")
        if isinstance(la, (int, float)) and (now - la) <= IDLE_EXPIRE_SEC:
            out[sid] = e
    return out


def _worktree_key(cwd, main_root):
    """Working-tree identity, derived from the cwd PATH (no git on the hot path).

    Two sessions collide on HEAD/index/working-tree ONLY if they share a working
    tree. A git worktree (`<main>/.claude/worktrees/<slug>`) is HEAD-isolated from
    every other worktree AND from the main checkout, so each gets its own key; a
    main-checkout cwd keys to main_root. Sessions in DIFFERENT worktrees are
    isolated and must NOT warn-block each other — this is what makes the
    always-many-concurrent-sessions workflow usable.
    """
    if cwd:
        m = WORKTREE_KEY_RE.match(cwd)
        if m:
            return os.path.normpath(m.group(1))
    if main_root:
        return os.path.normpath(main_root)
    return os.path.normpath(cwd) if cwd else ""


# A command that will touch a shared REF, not just the working tree. Branch
# collisions only matter for these, so everything else stays off this path and
# the extra `git rev-parse` never lands on the hot path for an ordinary command.
_REF_TOUCHING_RE = re.compile(
    r"(?:^|[\n;|&]\s*)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*git\b[^|;&\n]*"
    r"\b(push|commit|merge|rebase|cherry-pick)\b"
)


def _current_branch(cwd):
    """Short branch name for `cwd`, or '' when detached / not a repo / git slow.

    Fail-open by construction: every failure path returns '', and an empty
    branch never matches another session's branch, so a broken git can only
    make this guard quieter, never louder or wrong.
    """
    if not cwd:
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, timeout=2, check=False,
            # NOT `text=True`. That decodes the child's bytes with the LOCALE
            # encoding, which raises UnicodeDecodeError on a non-UTF-8 Windows
            # console the moment a path holds a non-ASCII character -- and this
            # brain ships cross-platform with emoji-named vault folders. Caught
            # by scripts/check-utf8-subprocess.py, not by review.
            encoding="utf-8", errors="replace",
        )
    except Exception:
        return ""
    if out.returncode != 0:
        return ""
    b = (out.stdout or "").strip()
    return "" if b in ("", "HEAD") else b


def _same_dir(a, b):
    """True when two cwds are the same directory.

    Deliberately NOT `_worktree_key`. That key only recognises the
    `<main>/.claude/worktrees/<slug>` layout; a checkout laid out as a sibling
    directory (`<repo>-<slug>`, which several worktree helpers produce) falls
    through to main_root instead. Reusing it here made every sibling collapse
    onto the caller's own key, so the guard matched nothing at all — caught by
    its own positive control before it shipped, not in review.
    """
    if not a or not b:
        return False
    return os.path.normpath(a) == os.path.normpath(b)


def _live_branch_siblings(sessions, my_id, now, main_root, my_cwd, my_branch):
    """Live sessions on the SAME BRANCH but a DIFFERENT working tree.

    This is the gap `_live_siblings` documents and deliberately leaves open:
    "two sessions pushing the same branch ... OUT OF SCOPE here: per-session
    branch isolation makes them rare." That premise assumes ONE BRANCH PER
    SESSION. It stops holding the moment branches are named per TICKET or per
    FEATURE, because then two sessions working the same ticket legitimately
    share one branch and are invisible to a worktree-keyed check.

    Measured: two sessions on one shared branch, in separate worktrees,
    independently produced the same upstream merge with the same conflict
    resolution. Nothing warned either of them. Git's non-fast-forward rejection
    is what finally surfaced it, at push time, after both had done the work.

    Deliberately a WARNING and never a block: a branch is a shared REF, not a
    shared working tree, so the catastrophic HEAD/index stomp is not available
    here and git already refuses the unsafe push. The recoverable cost of this
    collision is DUPLICATED EFFORT, and the only way to recover that is to say
    so BEFORE the work rather than at push time.
    """
    if not my_branch:
        return []
    out = []
    for sid, e in sessions.items():
        if sid == my_id:
            continue
        la = e.get("last_activity_at")
        if not (isinstance(la, (int, float)) and (now - la) < WARN_WINDOW_SEC):
            continue
        if e.get("branch") != my_branch:
            continue
        if _same_dir(e.get("cwd") or "", my_cwd):
            continue  # same directory -> already covered by _live_siblings
        out.append((sid, e))
    out.sort(key=lambda kv: kv[1].get("last_activity_at") or 0, reverse=True)
    return out


def _live_siblings(sessions, my_id, now, main_root, my_cwd):
    """Entries that are NOT me, were active within WARN_WINDOW_SEC, AND share my
    working tree (same worktree-key), newest first.

    Liveness is recency of last_activity_at ONLY — never the `pid` field (that is
    the ephemeral hook pid, dead on arrival; see module docstring). Cross-worktree
    siblings are HEAD/index-isolated and are intentionally NOT counted — the
    catastrophic collisions (HEAD-drift, index/working-tree stomp) require a
    SHARED working tree. Residual cross-worktree risks (shared refs, the object
    store, two sessions pushing the same branch) are OUT OF SCOPE here: per-session
    `claude/<slug>` branch isolation makes them rare, and the `cd`-into-main guard
    (check-cd-outside-worktree.py) covers the worktree-escape path.
    """
    my_key = _worktree_key(my_cwd, main_root)
    live = []
    for sid, e in sessions.items():
        if sid == my_id:
            continue
        la = e.get("last_activity_at")
        if not (isinstance(la, (int, float)) and (now - la) < WARN_WINDOW_SEC):
            continue
        if _worktree_key(e.get("cwd", ""), main_root) != my_key:
            continue  # different working tree => HEAD-isolated => not a collision
        live.append((sid, e))
    live.sort(key=lambda kv: kv[1].get("last_activity_at", 0), reverse=True)
    return live


def _upsert_self(sessions, my_id, cwd, now, branch=""):
    """Insert/refresh my own entry, preserving started_at + a known cwd.

    `pid` is recorded for human debugging only (it is the hook pid, not the
    session pid) and is never read for liveness.
    """
    e = dict(sessions.get(my_id) or {})
    e["last_activity_at"] = now
    if not e.get("started_at"):
        e["started_at"] = now
    if cwd:
        e["cwd"] = cwd
    if branch:
        e["branch"] = branch
    e["pid"] = os.getpid()  # informational only
    sessions[my_id] = e
    return sessions


def _write_sessions(lock_path, sessions):
    _atomic_write_json(lock_path, {"version": SCHEMA_VERSION, "sessions": sessions})


@contextlib.contextmanager
def _flock(lock_path):
    """Best-effort exclusive lock via a sidecar file, serializing the
    read-modify-write across concurrent sessions. Non-blocking with a bounded
    retry, then FAIL-OPEN (yield without the lock) so a stuck holder can never
    hang a tool call. flock auto-releases on process exit.

    If FLOCK_BLOCKING_ENV is set (tests only), acquire with a BLOCKING flock so
    serialization is guaranteed with zero fail-open — never enabled in production."""
    if fcntl is None:
        yield
        return
    sidecar = os.path.join(os.path.dirname(lock_path), ".session-lock.lock")
    f = None
    held = False
    try:
        try:
            os.makedirs(os.path.dirname(sidecar), exist_ok=True)
            f = open(sidecar, "ab+")
        except OSError:
            yield
            return
        if os.environ.get(FLOCK_BLOCKING_ENV) == "1":
            # Deterministic serialization (tests only): wait for the exclusive lock
            # instead of failing open. Never enabled in production.
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                held = True
            except OSError:
                pass
            yield
            return
        for _ in range(FLOCK_RETRIES):
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                time.sleep(FLOCK_SLEEP_SEC)
        yield
    finally:
        if f is not None:
            if held:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                f.close()
            except OSError:
                pass


def _update_sessions(lock_path, mutate, now):
    """flock-protected: read -> prune -> mutate(sessions) -> write.

    `mutate` receives the pruned sessions dict, may inspect it (e.g. capture
    live siblings), and returns the dict to persist. Returns the written dict.
    """
    with _flock(lock_path):
        sessions = _prune_sessions(_load_sessions(_read_json(lock_path)), now)
        sessions = mutate(sessions)
        _write_sessions(lock_path, sessions)
    return sessions


def _git_gate_active(cwd, main_root):
    """Can the PreToolUse git-mutation gate actually FIRE in this repo?

    _is_home_repo_git_mutation attributes a command to the home repo by asking
    whether its target sits INSIDE main_root. main_root comes from
    `git rev-parse --git-common-dir`, which is right for LOCATING the shared lock
    but wrong for that containment test whenever the git dir lives OUTSIDE the
    checkout (a mirror or `--separate-git-dir` layout). There, main_root IS that
    outside path, no working-tree command is ever inside it, and the gate silently
    never fires -- while the once-per-session heads-up keeps firing, so it still
    LOOKS alive.

    Measured in such a repo with several live siblings and no bypass set: a live
    `git commit --dry-run` ran unblocked and the detector returned False for a
    bare commit, `reset --hard` and `push`. Passing main_root == cwd instead
    returned True, isolating the cause to the path mismatch. Repos with an in-tree
    `.git` gate normally.

    Reported, not repaired: making the gate live in those repos changes WHICH
    repos are gated at all, which is a behavior change with its own blast radius.
    Surfacing it is what stops a dead guard and a quiet one emitting the same
    signal.
    """
    if not cwd or not main_root:
        return True                      # unknown -> never claim it is broken
    cwd = os.path.normpath(cwd)
    main_root = os.path.normpath(main_root)
    return cwd == main_root or cwd.startswith(main_root + os.sep)


def _sibling_summary(live, now, main_root):
    sid, e = live[0]
    la = e.get("last_activity_at")
    active_min = int((now - la) // 60) if isinstance(la, (int, float)) else 0
    started_at = e.get("started_at")
    started_str = ""
    if isinstance(started_at, (int, float)):
        started_str = f" (started {int((now - started_at) // 60)} min ago)"
    extra = f" (+{len(live) - 1} more)" if len(live) > 1 else ""
    return (
        f"  repo:          {main_root}\n"
        f"  other session: {sid}{extra}\n"
        f"  last active:   {active_min} min ago{started_str}\n"
    )


# ---- git-mutation detection ------------------------------------------------

def _expand(path):
    if path == "~" or path.startswith("~/"):
        return os.path.expanduser(path)
    if path.startswith("$HOME"):
        return os.environ.get("HOME", os.path.expanduser("~")) + path[len("$HOME"):]
    return path


def _branch_is_mutating(rest):
    create_flags = {"-m", "-M", "-d", "-D", "-c", "-C", "--move", "--copy",
                    "--delete", "--force", "--edit-description"}
    for tok in rest:
        if tok in create_flags:
            return True
        if not tok.startswith("-"):
            return True  # positional arg => creating/renaming a branch
    return False


def _stash_is_mutating(rest):
    if not rest:
        return True  # bare `git stash` == push
    return rest[0] not in {"list", "show"}


def _tag_is_mutating(rest):
    read_flags = {"-l", "--list", "-n", "--contains", "--points-at", "--merged",
                  "--no-merged", "--sort", "--format", "--column"}
    for tok in rest:
        if tok in {"-d", "--delete", "-f", "--force"}:
            return True
    for tok in rest:
        if tok in read_flags or tok.startswith("--sort=") or tok.startswith("--format="):
            return False
    for tok in rest:
        if not tok.startswith("-"):
            return True  # positional tag name => creating
    return False


def _is_literal_pathspec(tok):
    """True for a plainly-named path; False for anything that sweeps unnamed files.

    "Explicitly named" is the load-bearing word. `git commit -o .` is scoped in
    the -o sense (it still ignores the index) but it commits EVERY modified file
    under cwd -- including a sibling session's unstaged edits in the shared
    working tree. That is the same harm through a different door, so whole-tree
    and glob pathspecs forfeit the exemption. A named subdirectory is still a
    deliberate scope and stays allowed.
    """
    if not tok or tok == "-":
        return False
    if tok.startswith(":"):                     # pathspec magic: :/ , :(glob), :!x
        return False
    if any(c in tok for c in "*?["):            # glob -> matches files you did not name
        return False
    if tok.rstrip("/") in {"", ".", ".."}:      # . ./ .. ../ -> whole tree
        return False
    return True


def _commit_is_scoped_to_pathspecs(rest):
    """PURE. True only if these `git commit` args PROVE the commit takes nothing
    but explicitly named paths -- i.e. `-o`/`--only` plus >=1 literal pathspec,
    with no index-widening flag.

    `rest` is the token list AFTER the `commit` subcommand. Returns False on the
    slightest doubt (unknown option, no pathspec, no -o): the caller then blocks,
    which is the pre-existing behavior, so every ambiguity fails safe.

    Why -o is genuinely safe here (measured, not read off the man page): it
    commits the working-tree contents of the named paths and disregards anything
    staged for other paths. With a file B staged, `git commit -o A` produced a
    commit containing only A and left B still staged; the same commit run bare
    swept B in. That bare sweep IS
    SIBLING-SESSION-PARALLEL-COMMIT-COLLISION.

    Short options cluster (`-om "msg"` really does parse as `-o -m "msg"`), so the
    cluster walk is required, not defensive padding -- without it "msg" reads as a
    pathspec and a commit naming no paths would be allowed.
    """
    only = False
    pathspecs = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            pathspecs.extend(rest[i + 1:])      # everything after -- is a pathspec
            break
        if tok.startswith("--"):
            name, has_eq, _ = tok.partition("=")
            if name in COMMIT_UNSAFE_LONG:
                return False
            if name == "--only":
                only = True
                i += 1
                continue
            if name in COMMIT_LONG_VALUE_OPTS:
                i += 1 if has_eq else 2         # `--opt=v` self-contained; `--opt v` eats next
                continue
            if name in COMMIT_LONG_BOOL_OPTS:
                i += 1
                continue
            return False                        # unclassified option -> cannot prove safety
        if tok.startswith("-") and tok != "-":
            j = 1
            eats_next = False
            while j < len(tok):
                ch = tok[j]
                if ch in COMMIT_UNSAFE_SHORT:
                    return False
                if ch == "o":
                    only = True
                    j += 1
                    continue
                if ch in COMMIT_SHORT_VALUE:
                    # remainder of the cluster is the value (`-mmsg`); if the
                    # cluster ends here (`-m msg`), the NEXT token is the value.
                    eats_next = (j + 1 == len(tok))
                    break
                if ch in COMMIT_SHORT_OPTARG:
                    break                       # optional value is attached; rest is it
                if ch in COMMIT_SHORT_BOOL:
                    j += 1
                    continue
                return False                    # unclassified flag -> cannot prove safety
            i += 2 if eats_next else 1
            continue
        pathspecs.append(tok)
        i += 1

    if not only or not pathspecs:
        return False
    return all(_is_literal_pathspec(p) for p in pathspecs)


def _index_is_empty(dirpath):
    """True only if `dirpath`'s repo has NOTHING staged (index == HEAD).

    `git diff --cached --quiet`: rc 0 = clean index, rc 1 = staged changes, other
    = error. Anything but a clean 0 returns False so the caller keeps blocking --
    an unreadable index is not an empty one.

    Works on an UNBORN HEAD (verified: rc 0 with an empty index, rc 1 with a
    staged file), so a first-commit repo needs no special case. Compares index to
    HEAD without walking the working tree, so it stays cheap even on a very large
    repository, and it only ever runs after the syntactic check has already passed
    -- never on the hot path for ordinary commands.

    NOTE this is belt-and-braces, not the primary safety property: `-o` ignores
    the index whatever it holds, so the TOCTOU gap between this probe and the real
    commit cannot reintroduce the collision.
    """
    try:
        r = subprocess.run(
            ["git", "-C", dirpath, "diff", "--cached", "--quiet"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_SEC,
            env=_GIT_CLEAN_ENV,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return r.returncode == 0


def _git_mutation_target(tokens):
    """Given shlex tokens of one segment, return False if it is not a git
    mutation, else (True, cdir_or_None, gdir_or_None): `cdir` is the `-C` value
    and `gdir` is the explicit git-dir (`--git-dir` flag or `GIT_DIR=` env) if
    present. The git-dir is the real collision surface (HEAD/index/refs live
    there); `--work-tree`/`GIT_WORK_TREE` are deliberately NOT captured because
    they relocate only the working tree, not the git-dir — so they do not move
    the surface this lock guards, and attributing a mutation to a `--work-tree`
    while its git-dir is still the home repo would be a false-ALLOW.
    """
    i = 0
    cdir = gdir = None
    # skip leading VAR=val env assignments + transparent wrappers (env/command/
    # sudo/...). git honors GIT_DIR like `--git-dir`, so capture it here; an
    # explicit `--git-dir` flag below overrides it (matching git's precedence).
    while i < len(tokens):
        t = tokens[i]
        if ENV_ASSIGN_RE.match(t) or t in WRAPPER_PREFIXES:
            if t.startswith("GIT_DIR="):
                gdir = t.split("=", 1)[1]
            i += 1
            continue
        break
    if i >= len(tokens):
        return False
    exe = tokens[i]
    base = os.path.basename(exe)
    if base != "git" and not exe.endswith("/git"):
        return False
    i += 1

    while i < len(tokens) and tokens[i].startswith("-"):
        tok = tokens[i]
        if tok == "-C":
            if i + 1 < len(tokens):
                cdir = tokens[i + 1]
            i += 2
            continue
        if tok == "--git-dir":
            if i + 1 < len(tokens):
                gdir = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--git-dir="):
            gdir = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in GIT_GLOBAL_VALUE_OPTS:
            i += 2
            continue
        if "=" in tok and tok.split("=", 1)[0] in GIT_GLOBAL_VALUE_OPTS:
            i += 1
            continue
        i += 1  # boolean global flag (--no-pager, --paginate, --bare, -p, ...)

    if i >= len(tokens):
        return False
    sub = tokens[i]
    rest = tokens[i + 1:]

    if sub in ALWAYS_MUTATING:
        mutating = True
    elif sub == "branch":
        mutating = _branch_is_mutating(rest)
    elif sub == "stash":
        mutating = _stash_is_mutating(rest)
    elif sub == "tag":
        mutating = _tag_is_mutating(rest)
    else:
        mutating = False

    if not mutating:
        return False
    return (True, cdir, gdir, sub, rest)


def _apply_cd(tokens, effective_cwd, cwd_known):
    """Apply a `cd` segment to the running effective cwd. Returns (new_cwd, known).

    `known=False` means we lost track of the dir (e.g. `cd "$VAR"`, `cd -`, a
    glob) — a subsequent BARE `git commit` must then NOT be attributed to the
    home repo, because we cannot prove it targets it (same posture as the
    unresolvable redirect precedent below: prefer letting a cross-repo op through
    over a false-block, which trains reflexive bypass)."""
    rest = tokens[1:]
    # `cd -P -- /x`: drop option flags + the `--` separator, keep the first path.
    args = [t for t in rest if t != "--" and not t.startswith("-")]
    if not args:
        # bare `cd` => HOME
        return os.path.normpath(os.environ.get("HOME", os.path.expanduser("~"))), True
    dest = _expand(args[0])
    if "$" in dest or "*" in dest or "?" in dest or dest == "-":
        return effective_cwd, False           # unresolvable target → lose track
    if os.path.isabs(dest):
        return os.path.normpath(dest), True
    if not cwd_known:
        return effective_cwd, False           # relative cd on an already-unknown base
    return os.path.normpath(os.path.join(effective_cwd, dest)), True


def _is_home_repo_git_mutation(command, cwd, main_root, index_probe=None):
    """True if `command` runs a git-mutating op against the session's home repo.

    Tracks an in-command `cd` so a compound `cd /other-repo && git commit` is
    attributed to /other-repo, NOT the session cwd. Without this, a bare commit
    after a `cd` into a DIFFERENT repo (e.g. committing to one repo from a session
    whose cwd is a different repo) false-blocked: the segment splitter evaluated
    the bare `git commit` against the session cwd because it carries no redirect
    (SIBLING-SESSION-FALSE-BLOCK-ON-CD-TO-OTHER-REPO — false-blocked repeatedly in
    one session, training reflexive SIBLING_SESSION_LOCK_BYPASS=1). An explicit
    `git -C <path>` still wins over the tracked cwd."""
    if not command or "git" not in command:
        return False
    probe = _index_is_empty if index_probe is None else index_probe
    main_root = os.path.normpath(main_root)
    effective_cwd = os.path.normpath(cwd) if cwd else main_root
    cwd_known = True

    def _in_home(target):
        return target == main_root or target.startswith(main_root + os.sep)

    for seg in SEGMENT_SPLIT_RE.split(command):
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = None

        # `cd` re-bases every FOLLOWING segment's bare-git target. Strip a leading
        # subshell `(` so `( cd x && git commit )` is tracked too.
        if tokens:
            head = tokens[1:] if (tokens and tokens[0] == "(") else tokens
            if head and head[0] == "cd":
                effective_cwd, cwd_known = _apply_cd(head, effective_cwd, cwd_known)
                continue

        if "git" not in seg:
            continue

        if tokens is None:
            # unbalanced quotes: conservative coarse check, but only attribute to
            # the home repo when the effective cwd actually IS the home repo (so a
            # quoting-weird commit in another repo isn't false-blocked).
            # An explicit absolute `git -C <path>` outside the home repo wins even
            # here: a multi-line `-m "..."` message makes the newline-splitter
            # produce an unbalanced-quote segment, and without this check the
            # coarse branch attributed `git -C /other-repo commit -m "<multi-
            # line>"` to the home cwd and false-blocked (SIBLING-SESSION-FALSE-BLOCK
            # class).
            mc = re.search(r"\bgit\s+(?:--?[\w-]+(?:=\S+)?\s+)*-C\s+(\S+)", seg)
            if mc:
                cpath = _expand(mc.group(1).strip("\"'"))
                if "$" in cpath:
                    # -C is an UNRESOLVED shell variable (e.g. `git -C "$d"`): the
                    # parsed-token path already lets this through, but a multi-line
                    # `-m` drops us into this coarse branch instead, where the
                    # absolute-path escape below can't fire ($VAR isn't absolute).
                    # Mirror the parsed-path posture here: an explicit -C almost
                    # always targets a DIFFERENT repo and the real collision (a bare
                    # `git commit`) carries no -C, so let it through rather than
                    # false-block.
                    continue
                if os.path.isabs(cpath) and not _in_home(os.path.normpath(cpath)):
                    continue  # explicit -C at a different repo → not this lock's business
            # Same posture for an explicit git-dir (`--git-dir`), which a multi-line
            # `-m` likewise drops into this coarse branch. The git-dir is the
            # collision surface; an absolute one outside home → let through, an
            # unresolvable $VAR → fail open. (--work-tree is intentionally NOT matched
            # here: it does not move the git-dir, so it must not relax the gate.)
            mg = re.search(r"\bgit\s+(?:--?[\w-]+(?:=\S+)?\s+)*--git-dir(?:=|\s+)(\S+)", seg)
            if mg:
                gpath = _expand(mg.group(1).strip("\"'"))
                if "$" in gpath:
                    continue
                if os.path.isabs(gpath) and not _in_home(os.path.normpath(gpath)):
                    continue  # explicit git-dir at a different repo → not this lock's business
            # GIT_DIR= env form lands here too under a multi-line -m. ANCHOR it to the
            # leading env-assignment block BEFORE `git` (env vars must precede the
            # command) so a GIT_DIR= appearing only INSIDE the message can't
            # false-ALLOW a real home commit — the worst failure for this gate.
            mge = re.search(
                r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*GIT_DIR=(\S+)"
                r"(?:\s+[A-Za-z_][A-Za-z0-9_]*=\S+)*\s+"
                r"(?:env\s+|command\s+|sudo\s+|exec\s+|nohup\s+|builtin\s+)*git\b",
                seg,
            )
            if mge:
                gpath = _expand(mge.group(1).strip("\"'"))
                if "$" in gpath:
                    continue  # unresolvable GIT_DIR ($VAR) → fail open
                if os.path.isabs(gpath) and not _in_home(os.path.normpath(gpath)):
                    continue  # GIT_DIR at a different repo → not this lock's business
            if cwd_known and _in_home(effective_cwd) and re.search(r"\bgit\b", seg) and re.search(
                r"\b(commit|push|checkout|switch|merge|rebase|reset|cherry-pick|"
                r"revert|pull|am|apply)\b", seg
            ):
                return True
            continue

        res = _git_mutation_target(tokens)
        if not res:
            continue
        _, cdir, gdir, sub, rest = res
        # `-C` changes git's working dir BEFORE the rest of the command resolves,
        # so apply it first to get the cwd this segment's git actually runs in.
        seg_cwd, seg_cwd_known = effective_cwd, cwd_known
        if cdir:
            cexp = _expand(cdir)
            if "$" in cexp:
                # -C points at an UNRESOLVED shell variable (e.g. `git -C "$MV"`):
                # shlex doesn't expand shell vars and the hook can't see the
                # command's env. An explicit -C almost always targets a DIFFERENT
                # dir, and the real collision pattern (a bare `git commit`) carries
                # no -C — so treat an unresolvable -C as "not this repo" rather than
                # false-blocking legitimate cross-repo ops. Bare-commit detection
                # is unaffected.
                continue
            if os.path.isabs(cexp):
                seg_cwd, seg_cwd_known = os.path.normpath(cexp), True
            elif cwd_known:
                seg_cwd, seg_cwd_known = os.path.normpath(os.path.join(effective_cwd, cexp)), True
            else:
                continue  # relative -C on an unknown effective cwd → don't false-block
        if gdir:
            # An explicit git-dir (`--git-dir`/`GIT_DIR=`) IS the collision surface
            # (HEAD/index/refs live there), so it wins over -C-derived discovery.
            # Unresolvable ($VAR) → fail open, same posture as -C above.
            gexp = _expand(gdir)
            if "$" in gexp:
                continue
            if os.path.isabs(gexp):
                target = os.path.normpath(gexp)
            elif seg_cwd_known:
                target = os.path.normpath(os.path.join(seg_cwd, gexp))
            else:
                continue  # relative git-dir on an unknown cwd → don't false-block
        else:
            if not seg_cwd_known:
                continue  # bare git in an unknown dir (post unresolvable cd) → let through
            target = seg_cwd
        if _in_home(target):
            # The one exemption: a commit that provably takes ONLY the paths it
            # names and ignores the index cannot sweep a sibling's staged work, so
            # it is not the collision this gate exists to stop. Every condition is
            # required:
            #   sub == "commit"        -- no other verb is exempt, ever
            #   not gdir               -- an explicit git-dir means the index we
            #                             probe may not be the one git commits
            #                             against; do not reason about it, block
            #   seg_cwd_known          -- must know WHERE to probe
            #   _commit_is_scoped_...  -- -o + >=1 literal pathspec, no -a/-i/
            #                             --amend/--interactive/--patch
            #   probe(seg_cwd)         -- and nothing is staged right now
            # Order matters for cost: the pure syntactic check runs first, so the
            # git subprocess only ever fires for a command already shaped like a
            # scoped commit.
            if (sub == "commit" and not gdir and seg_cwd_known
                    and _commit_is_scoped_to_pathspecs(rest)
                    and probe(seg_cwd)):
                continue
            return True
        # a git mutation pointed at a DIFFERENT repo is not a collision on this
        # repo's HEAD/index — let it through.
    return False


# ---- event handlers --------------------------------------------------------

def _session_start(payload):
    session_id = payload.get("session_id") or "unknown"
    cwd = (payload.get("cwd")
           or os.environ.get("CLAUDE_PROJECT_DIR")
           or os.environ.get("CLAUDE_CWD")
           or os.getcwd())
    main_root = _main_root(cwd)
    if not main_root:
        _emit_continue()
        return

    lock_path = _lock_path_for(main_root)
    now = time.time()
    captured = {}

    def mutate(sessions):
        captured["live"] = _live_siblings(sessions, session_id, now, main_root, cwd)
        return _upsert_self(sessions, session_id, cwd, now)

    _update_sessions(lock_path, mutate, now)
    live = captured.get("live", [])

    warn = None
    if live:
        warn = (
            "[session-lock] Another Claude session appears active in this repo:\n"
            + _sibling_summary(live, now, main_root)
            + "Two sessions on one repo risk CONCURRENT-SESSION-HEAD-DRIFT + "
            "sibling-session parallel-commit collisions. Coordinate, use a "
            "separate repo, or wait for the other session to finish.\n"
            f"Bypass: {BYPASS_ENV}=1 (intentional parallel / collaborative work)."
        )
        # Say plainly when the enforcement arm CANNOT fire here, so this warning
        # is never mistaken for active protection. Without this line the notice
        # reads identically in a gated repo and an ungated one.
        if not _git_gate_active(cwd, main_root):
            warn += (
                "\n\nHEADS-UP: the git-mutation gate is DORMANT in this repo. Its git dir\n"
                f"({main_root}) sits OUTSIDE the checkout ({cwd}), so no command\n"
                "resolves as 'this repo' and commits / resets / pushes here are NOT blocked.\n"
                "The warning above is informational only -- coordinate by hand."
            )

    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(session_id), "w", encoding="utf-8") as f:
            f.write(lock_path)
    except OSError:
        pass
    # fresh session => clear any stale "already warned" marker
    try:
        os.remove(_warned_marker_path(session_id))
    except OSError:
        pass
    _prune_cache()

    if warn:
        _emit({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": warn,
        }})
    else:
        _emit_continue()


def _lock_path_from_cache(session_id):
    if not session_id:
        return ""
    try:
        with open(_cache_path(session_id), "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _heartbeat(session_id):
    """Bump my last_activity_at. Cheap: cache pointer + file I/O, no git."""
    lock_path = _lock_path_from_cache(session_id)
    if not lock_path:
        return
    now = time.time()
    _update_sessions(lock_path,
                     lambda s: _upsert_self(s, session_id, None, now), now)


def _session_end(session_id):
    """Graceful-exit cleanup: drop my own lock entry + cache pointer + warned
    marker so the live-session count stays honest for siblings (many concurrent
    sessions otherwise leave stale entries inflating the count for 30 min).
    Best-effort — ungraceful kills are covered by the 5-min live window + the
    30-min prune."""
    lock_path = _lock_path_from_cache(session_id)
    if lock_path:
        now = time.time()

        def mutate(sessions):
            sessions.pop(session_id, None)
            return sessions

        _update_sessions(lock_path, mutate, now)
    for p in (_cache_path(session_id), _warned_marker_path(session_id)):
        try:
            os.remove(p)
        except OSError:
            pass


def _pretooluse(payload):
    """Return an exit code: 0 = allow, 2 = warn-block (message already on stderr)."""
    session_id = payload.get("session_id") or ""
    lock_path = _lock_path_from_cache(session_id)
    if not lock_path:
        return 0  # no pointer => can't cheaply resolve the repo; fail open
    main_root = _main_root_from_lock_path(lock_path)
    cwd = payload.get("cwd") or main_root

    now = time.time()
    captured = {}
    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""

    # Resolve the branch ONLY for commands that touch a shared ref, so an
    # ordinary `ls` never pays for a git subprocess.
    my_branch = _current_branch(cwd) if _REF_TOUCHING_RE.search(command) else ""

    # refresh my heartbeat on every Bash call (keep the slot warm), capturing live
    # siblings from the same consistent, flock-protected snapshot.
    def mutate(sessions):
        captured["live"] = _live_siblings(sessions, session_id, now, main_root, cwd)
        captured["branch_live"] = _live_branch_siblings(
            sessions, session_id, now, main_root, cwd, my_branch
        )
        return _upsert_self(sessions, session_id, payload.get("cwd"), now, my_branch)

    _update_sessions(lock_path, mutate, now)
    live = captured.get("live", [])
    branch_live = captured.get("branch_live", [])

    # Cross-worktree, SAME-branch collision. Warn, never block: git's own
    # non-fast-forward rejection already prevents the data loss, so the only
    # thing left to save is the duplicated work — and that is saved by saying so
    # BEFORE the work, not at push time.
    if branch_live and not live:
        _emit({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                f"[session-lock] ANOTHER LIVE SESSION IS ON THIS BRANCH ({my_branch}).\n"
                + _sibling_summary(branch_live, now, main_root)
                + "\nDifferent worktrees, so HEAD and the index are safe and this is NOT "
                "blocked. The risk here is duplicated work: two sessions sharing one "
                "branch have independently produced the same upstream merge, with the "
                "same conflict resolution, and only git's non-fast-forward rejection "
                "surfaced it — after both had done it.\n"
                "Before you commit or push: `git fetch origin && git log --oneline "
                "HEAD..origin/<branch>` to see whether they already did this.\n"
                f"Bypass: {BYPASS_ENV}=1"
            ),
        }})
        return -1  # emitted our own continue-with-context; main must not double-emit

    if not live:
        return 0

    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    summary = _sibling_summary(live, now, main_root)

    if _is_home_repo_git_mutation(command, cwd, main_root):
        sys.stderr.write(
            "BLOCKED: a sibling Claude session is live in this repo and this is a "
            "git-mutating command.\n"
            + summary
            + "\nTwo sessions committing / checking out / resetting the same repo is "
            "exactly the SIBLING-SESSION-PARALLEL-COMMIT-COLLISION that can wipe "
            "in-flight work. Coordinate with the other session before mutating "
            "shared git state.\n\n"
            "If this is a COMMIT there is a way through that keeps the gate on.\n"
            "Stage nothing, and name every path you are committing:\n"
            "    git commit -o <path> [<path>...] -F <message-file>\n"
            "`-o`/`--only` commits the working-tree contents of the named paths and\n"
            "disregards the index, so it cannot sweep a sibling's staged work. That\n"
            "form is ALLOWED here while the index is empty. It needs at least one\n"
            "literal path (not `.`, not a glob) and no -a / -i / --amend / --patch;\n"
            "use -F <file> or a single-line -m (a multi-line -m cannot be parsed\n"
            "safely, so it stays blocked).\n\n"
            f"Blanket bypass, disables this gate session-wide: {BYPASS_ENV}=1\n"
        )
        return 2

    marker = _warned_marker_path(session_id)
    if not os.path.exists(marker):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            open(marker, "w").close()
        except OSError:
            pass
        sys.stderr.write(
            "HEADS-UP: another Claude session is live in this repo.\n"
            + summary
            + "\nRead-only work is fine, but avoid git commit / push / checkout / "
            "reset here until the other session finishes — concurrent mutations "
            "collide (CONCURRENT-SESSION-HEAD-DRIFT). This heads-up fires once; "
            "git-mutating commands stay gated while the sibling is live.\n\n"
            f"Bypass (intentional parallel / collaborative work): {BYPASS_ENV}=1\n"
        )
        return 2
    return 0


def main() -> int:
    if os.environ.get(BYPASS_ENV) == "1":
        _emit_continue()
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        _emit_continue()
        return 0

    event = payload.get("hook_event_name") or ""
    try:
        if event == "SessionStart":
            _session_start(payload)
            return 0
        if event == "PreToolUse":
            if (payload.get("tool_name") or "") != "Bash":
                _emit_continue()
                return 0
            rc = _pretooluse(payload)
            if rc == 0:
                _emit_continue()
            if rc < 0:
                return 0  # _pretooluse already emitted its own continue-with-context
            return rc
        if event == "SessionEnd":
            _session_end(payload.get("session_id") or "")
            _emit_continue()
            return 0
        # Stop / any other invocation = activity heartbeat.
        _heartbeat(payload.get("session_id") or "")
        _emit_continue()
        return 0
    except Exception:
        # Never break a session or a tool call on an internal error (fail open).
        _emit_continue()
        return 0


if __name__ == "__main__":
    # This hook prints repo paths, and a vault path carries non-ASCII. On a
    # cp1252 Windows console that print() raises UnicodeEncodeError, the caller
    # reads the empty output as failure, and a coordination lock that cannot
    # speak is a lock that silently stops coordinating.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
