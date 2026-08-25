#!/usr/bin/env python3
"""gh-safe.py - session coordination + attribution for `gh pr create` / `gh pr merge`.

THE GAP THIS CLOSES (MYC-3527). On a machine running many concurrent agent
sessions, two unattributed shared-state mutations were observed: a PR appeared
that no session's transcript created, and a tracker ticket changed state with
zero work behind it. The existing coordination primitives are local-git-only:
hooks/session-lock.py gates git verbs against a live sibling, and
hooks/block-git-mutation-mid-operation.py gates in-flight operation state.
Both stop at the repo boundary. NOTHING covered mutations of EXTERNAL shared
state - `gh pr create`, `gh pr merge` - so two sessions could silently race
the same branch, and nothing on the resulting artifact said which session
made it.

THREE GUARANTEES, in order:

  1. ATTRIBUTION, fail loud. Every wrapped mutation carries a session trailer
     (`session: <id>`) sourced from --session or $CLAUDE_SESSION_ID. No id, no
     mutation: exit 2 before any gh call. Creates embed the trailer in the PR
     body; merges post a claim comment carrying it (uniform across merge
     methods, including --rebase which has no commit body to carry one).

  2. READ-BEFORE-WRITE, inside the lock. Create lists open PRs for the head
     branch first and refuses (exit 4) if one exists, printing it. Merge reads
     the PR state first and refuses (exit 4) unless OPEN. The read happens
     INSIDE the critical section, so the second of two racing sessions sees
     the first one's PR instead of racing it (no check-then-act window). A
     failed read fails CLOSED: no mutation on a blind read.

  3. ADVISORY LOCK, one yields. A mkdir-based lock keyed by repo+branch under
     the user temp dir. mkdir is atomic on POSIX and Windows and needs no
     flock(1) (absent on stock macOS) and no fcntl (absent on Windows) - the
     same reasoning as scripts/_session_close_guard.sh. The loser does NOT
     wait: it yields immediately (exit 3) naming the holder session so the
     calling agent can decide. Stale locks (dead same-host owner pid on POSIX,
     or older than GH_SAFE_LOCK_STALE_SEC) are reclaimed. There is
     deliberately NO bypass env: the escape hatch for a wedged lock is the
     stale reclaim, and a "just this once" bypass is how two sessions mint
     competing PRs again.

EXIT CODES
    0  wrapped gh command succeeded (its own output is passed through)
    2  usage / missing or malformed session id / body cannot carry a trailer
    3  yielded: another live session holds the repo+branch lock
    4  refused by read-before-write (PR already exists / PR not OPEN)
    1  a pre-write read failed (refusing to mutate blind); or internal error
    else: the wrapped gh command's own nonzero exit code, passed through

ENV
    CLAUDE_SESSION_ID       session id used when --session is not given
    GH_SAFE_LOCK_DIR        lock root (default: the system temp dir)
    GH_SAFE_LOCK_STALE_SEC  stale-lock reclaim age in seconds (default 600)

Convention + policy doc: docs/SESSION_COORDINATION.md. Companion warn rule for
bare `gh pr create|merge` outside this wrapper:
templates/hookify-rules/hookify.warn-bare-gh-pr-mutation.local.md.

Stdlib only. Python 3.9 compatible. Works on macOS, Linux, and Windows
git-bash (invoke as `python3 scripts/gh-safe.py ...` or via an alias).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

PROG = "gh-safe"
DEFAULT_STALE_SEC = 600.0
GH_READ_TIMEOUT_SEC = 120
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_YIELD = 3
EXIT_REFUSED = 4

USAGE = """usage: gh-safe pr create [--session <id>] <gh pr create args>
       gh-safe pr merge  [--session <id>] [<pr>] <gh pr merge args>

Wraps exactly two external mutations with session attribution, a
read-before-write check, and a repo+branch advisory lock. Reads
(`gh pr list/view/checks/...`) need no wrapper - use plain gh.

The session id comes from --session or $CLAUDE_SESSION_ID; without one the
command refuses to run (exit 2). `pr create` requires --body or --body-file
so the `session: <id>` trailer can be embedded. See
docs/SESSION_COORDINATION.md for the trailer convention and exit codes.
"""


def _err(msg):
    sys.stderr.write("[%s] %s\n" % (PROG, msg))


def _run_git(args, cwd=None):
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _extract_value_flag(args, names):
    """First value of any flag in `names` (supports `--flag v` and `--flag=v`)."""
    for i, tok in enumerate(args):
        if tok in names:
            if i + 1 < len(args):
                return args[i + 1]
            return None
        for name in names:
            if tok.startswith(name + "="):
                return tok.split("=", 1)[1]
    return None


def _strip_value_flag(args, names):
    """Remove every occurrence of the given value-taking flags. Returns
    (values, remaining_args)."""
    out, values, i = [], [], 0
    while i < len(args):
        tok = args[i]
        matched = False
        if tok in names:
            if i + 1 < len(args):
                values.append(args[i + 1])
            i += 2
            matched = True
        else:
            for name in names:
                if tok.startswith(name + "="):
                    values.append(tok.split("=", 1)[1])
                    i += 1
                    matched = True
                    break
        if not matched:
            out.append(tok)
            i += 1
    return values, out


def _resolve_session(args):
    """(session_id, args_without_session_flag) - fail loud when absent."""
    values, rest = _strip_value_flag(args, ["--session"])
    sid = (values[-1] if values else "") or os.environ.get("CLAUDE_SESSION_ID", "")
    sid = sid.strip()
    if not sid:
        _err(
            "no session id. Every external mutation must be attributable to "
            "the session that made it. Pass --session <id> or export "
            "CLAUDE_SESSION_ID. Refusing to run."
        )
        return None, rest
    if not SESSION_ID_RE.match(sid):
        _err("session id %r has characters outside [A-Za-z0-9._:-]; refusing." % sid)
        return None, rest
    return sid, rest


def _normalize_repo(spec):
    """Normalize a repo spec/URL to `host/owner/name` (lowercased, no .git).

    Handles https/ssh URLs (with or without an explicit :port), scp-style
    git@host:owner/repo, bare OWNER/REPO specs, and GitHub's ssh-over-443
    alias host (ssh.github.com), so every spelling of one repo yields ONE
    lock key. A numeric scp "owner" (git@host:443/x) is indistinguishable
    from a port and collapses as one; that spelling is not worth defending.
    """
    s = spec.strip().lower()
    s = re.sub(r"^(https?://|ssh://|git\+ssh://)", "", s)
    s = re.sub(r"^git@", "", s)
    s = re.sub(r"^([^/:]+):(\d+)/", r"\1/", s)  # URL host:port -> drop the port
    s = s.replace(":", "/")                     # scp-style colon -> path sep
    s = re.sub(r"^ssh\.github\.com/", "github.com/", s)
    s = re.sub(r"\.git$", "", s).strip("/")
    parts = [p for p in s.split("/") if p]
    if len(parts) == 2:
        return "github.com/" + "/".join(parts)
    if len(parts) >= 3:
        return "/".join(parts[-3:])
    return s


def _resolve_repo(args):
    """Repo lock key + the passthrough tokens for internal gh reads."""
    spec = _extract_value_flag(args, ["-R", "--repo"])
    if spec:
        return _normalize_repo(spec), ["--repo", spec]
    origin = _run_git(["remote", "get-url", "origin"])
    if origin:
        return _normalize_repo(origin), []
    return "", []


def _current_branch():
    return _run_git(["branch", "--show-current"])


# ---- advisory lock ----------------------------------------------------------

def _lock_root():
    return os.environ.get("GH_SAFE_LOCK_DIR") or tempfile.gettempdir()


def _lock_dir(repo, branch):
    key = hashlib.sha256((repo + "\n" + branch).encode("utf-8")).hexdigest()[:16]
    return os.path.join(_lock_root(), "gh-safe-%s.lock" % key)


def _stale_sec():
    try:
        return float(os.environ.get("GH_SAFE_LOCK_STALE_SEC", str(DEFAULT_STALE_SEC)))
    except (TypeError, ValueError):
        return DEFAULT_STALE_SEC


def _read_owner(lockdir):
    try:
        with open(os.path.join(lockdir, "owner.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _owner_is_stale(lockdir, owner):
    """Stale = dead same-host owner pid (POSIX only), or older than the window.

    The pid probe is gated to POSIX on purpose: on Windows os.kill(pid, 0) is
    not a probe - it TERMINATES the target via TerminateProcess. Windows relies
    on the age test alone.
    """
    now = time.time()
    created = owner.get("created_at")
    if not isinstance(created, (int, float)):
        try:
            created = os.stat(lockdir).st_mtime
        except OSError:
            created = now
    if (now - created) > _stale_sec():
        return True
    pid = owner.get("pid")
    host = owner.get("host")
    if (
        os.name == "posix"
        and isinstance(pid, int)
        and host == socket.gethostname()
    ):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True  # provably dead on this host -> the lock is abandoned
        except OSError:
            pass  # EPERM etc.: assume alive; never reclaim on uncertainty
    return False


def _test_reclaim_hold():
    """Test-only determinism knob (production never sets it): parks a
    reclaimer between its staleness verdict and its atomic rename so the
    suite can stage the reclaim race deterministically. Same pattern as the
    FLOCK_BLOCKING_ENV knob in hooks/session-lock.py. Bounded: resumes after
    10s even if the release never comes, so a broken test cannot hang gh."""
    hold = os.environ.get("GH_SAFE_TEST_RECLAIM_HOLD")
    if not hold:
        return
    try:
        with open(hold + ".ready", "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        return
    deadline = time.time() + 10
    while os.path.exists(hold) and time.time() < deadline:
        time.sleep(0.05)


def _acquire_lock(repo, branch, session, op):
    """Returns (lockdir, None) when acquired, or (None, owner) when held.

    STALE RECLAIM IS ATOMIC (rename-first), because the obvious shape is
    racy: with check-stale -> rmtree -> mkdir, a second contender can fully
    reclaim AND re-acquire between the first one's check and its rmtree, and
    the first's rmtree then deletes the second's FRESH lock (dual-hold). So:

      1. os.rename() the stale dir aside - exactly one contender can win the
         rename of a given dir; a loser gets FileNotFoundError and falls
         through to the mkdir attempt.
      2. RE-VERIFY staleness on the renamed dir: if a faster contender
         already reclaimed and re-acquired at the same path, what we just
         renamed is their fresh lock - rename it back and yield instead of
         stealing it. (Directory mtimes survive a rename, so the age test
         still reads the original clock.)
      3. Only a dir that is stale AFTER the rename gets deleted, and it is
         deleted at a private graveyard path no other contender touches.

    Residual 3-party window: between a loser's rename-away and its
    rename-back, a third contender can mkdir the freed path; the restore
    then fails and both later contenders yield loudly. Even then no
    duplicate PR is minted - read-before-write inside the winner's critical
    section is the backstop.
    """
    lockdir = _lock_dir(repo, branch)
    try:
        os.makedirs(_lock_root(), exist_ok=True)
    except OSError:
        pass
    for attempt in (1, 2):
        try:
            os.mkdir(lockdir)
        except FileExistsError:
            owner = _read_owner(lockdir)
            if attempt == 1 and _owner_is_stale(lockdir, owner):
                _test_reclaim_hold()  # no-op outside the test suite
                grave = "%s.reclaim.%d.%d" % (lockdir, os.getpid(), time.time_ns())
                try:
                    os.rename(lockdir, grave)
                except FileNotFoundError:
                    continue  # lost the reclaim race; the slot may be free now
                except OSError as e:
                    _err("cannot reclaim stale lock %s: %s" % (lockdir, e))
                    return None, owner
                current = _read_owner(grave)
                if not _owner_is_stale(grave, current):
                    # We renamed a contender's FRESH lock. Put it back, yield.
                    try:
                        os.rename(grave, lockdir)
                    except OSError:
                        _err(
                            "could not restore a live lock after a reclaim "
                            "race (%s); yielding" % lockdir
                        )
                    return None, current
                _err(
                    "reclaiming stale lock %s (owner session=%s pid=%s)"
                    % (lockdir, current.get("session", "?"), current.get("pid", "?"))
                )
                shutil.rmtree(grave, ignore_errors=True)
                continue
            return None, owner
        except OSError as e:
            _err("cannot take lock %s: %s" % (lockdir, e))
            return None, {}
        meta = {
            "session": session,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "op": op,
            "repo": repo,
            "branch": branch,
            "created_at": time.time(),
        }
        try:
            with open(os.path.join(lockdir, "owner.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f)
        except OSError:
            pass  # lock still held; metadata is best-effort
        return lockdir, None
    return None, _read_owner(lockdir)


def _release_lock(lockdir):
    if lockdir:
        shutil.rmtree(lockdir, ignore_errors=True)


def _yield_to(owner, repo, branch, op):
    age = ""
    created = owner.get("created_at")
    if isinstance(created, (int, float)):
        age = " for %ds" % int(time.time() - created)
    _err(
        "YIELD: another session holds the %s lock for %s#%s%s.\n"
        "  holder session: %s (pid %s on %s)\n"
        "  This session yields rather than minting a competing mutation. "
        "Wait for the holder to finish, or coordinate with it. A crashed "
        "holder's lock is reclaimed automatically after %ds."
        % (
            op, repo, branch, age,
            owner.get("session", "unknown"), owner.get("pid", "?"),
            owner.get("host", "?"), int(_stale_sec()),
        )
    )
    return EXIT_YIELD


# ---- gh calls ----------------------------------------------------------------

def _gh_read_json(args):
    """Run a gh READ returning (ok, parsed). Any failure -> (False, None):
    the callers fail CLOSED (never mutate on a blind read)."""
    try:
        r = subprocess.run(
            ["gh"] + args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GH_READ_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _err("gh read failed (%s %s): %s" % ("gh", " ".join(args[:3]), e))
        return False, None
    if r.returncode != 0:
        _err(
            "gh read failed rc=%d (%s): %s"
            % (r.returncode, " ".join(["gh"] + args[:3]), r.stderr.strip()[:300])
        )
        return False, None
    try:
        return True, json.loads(r.stdout or "null")
    except ValueError:
        _err("gh read returned non-JSON output; refusing to mutate blind.")
        return False, None


def _gh_passthrough(args):
    """Run the wrapped MUTATION with inherited stdio; return its exit code."""
    try:
        return subprocess.run(["gh"] + args).returncode
    except OSError as e:
        _err("cannot run gh: %s" % e)
        return 1


def _append_trailer(body, session):
    trailer = "session: %s" % session
    if re.search(r"(?m)^" + re.escape(trailer) + r"\s*$", body):
        return body  # already attributed to this session; keep idempotent
    return body.rstrip("\n") + "\n\n" + trailer + "\n"


# ---- subcommands ---------------------------------------------------------------

def cmd_create(args):
    session, args = _resolve_session(args)
    if session is None:
        return EXIT_USAGE
    if "--web" in args:
        _err("--web is interactive and cannot carry the session trailer; "
             "use a non-interactive create.")
        return EXIT_USAGE

    repo, repo_tokens = _resolve_repo(args)
    if not repo:
        _err("cannot resolve the repo (no --repo flag and no `origin` remote); "
             "the coordination lock needs a repo key. Refusing.")
        return EXIT_USAGE
    branch = _extract_value_flag(args, ["--head", "-H"]) or _current_branch()
    if not branch:
        _err("cannot resolve the head branch (pass --head or run on a branch).")
        return EXIT_USAGE

    # Build the attributed body BEFORE taking the lock (pure validation).
    tmp_body_path = None
    body = _extract_value_flag(args, ["--body", "-b"])
    body_file = _extract_value_flag(args, ["--body-file", "-F"])
    if body is not None:
        _, rest = _strip_value_flag(args, ["--body", "-b"])
        args = rest + ["--body", _append_trailer(body, session)]
    elif body_file is not None:
        if body_file == "-":
            _err("--body-file - (stdin) is not supported; pass a real file.")
            return EXIT_USAGE
        try:
            with open(body_file, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            _err("cannot read --body-file %s: %s" % (body_file, e))
            return EXIT_USAGE
        fd, tmp_body_path = tempfile.mkstemp(prefix="gh-safe-body-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_append_trailer(content, session))
        _, rest = _strip_value_flag(args, ["--body-file", "-F"])
        args = rest + ["--body-file", tmp_body_path]
    else:
        _err(
            "pr create needs --body or --body-file so the `session: <id>` "
            "trailer can be embedded (--fill alone cannot carry it)."
        )
        return EXIT_USAGE

    lockdir, owner = _acquire_lock(repo, branch, session, "pr-create")
    if lockdir is None:
        return _yield_to(owner or {}, repo, branch, "pr-create")
    try:
        # READ-BEFORE-WRITE, inside the critical section (no check-then-act gap).
        ok, prs = _gh_read_json(
            ["pr", "list", "--head", branch, "--state", "open",
             "--json", "number,url"] + repo_tokens
        )
        if not ok:
            _err("read-before-write failed; refusing to create blind.")
            return 1
        if isinstance(prs, list) and prs:
            first = prs[0] if isinstance(prs[0], dict) else {}
            _err(
                "REFUSED: an open PR already exists for %s#%s: %s\n"
                "  Creating another would mint a competing mutation. Update "
                "the existing PR instead."
                % (repo, branch, first.get("url", "(url unavailable)"))
            )
            return EXIT_REFUSED
        return _gh_passthrough(["pr", "create"] + args)
    finally:
        _release_lock(lockdir)
        if tmp_body_path:
            try:
                os.remove(tmp_body_path)
            except OSError:
                pass


MERGE_VALUE_FLAGS = [
    "--body", "-b", "--body-file", "-F", "--subject", "-t",
    "--match-head-commit", "--author-email", "-A", "--repo", "-R",
]


def _merge_selector(args):
    """First positional arg that is not the value of a known value flag."""
    skip_next = False
    for tok in args:
        if skip_next:
            skip_next = False
            continue
        if tok in MERGE_VALUE_FLAGS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        return tok
    return ""


def cmd_merge(args):
    session, args = _resolve_session(args)
    if session is None:
        return EXIT_USAGE

    repo, repo_tokens = _resolve_repo(args)
    if not repo:
        _err("cannot resolve the repo (no --repo flag and no `origin` remote); "
             "the coordination lock needs a repo key. Refusing.")
        return EXIT_USAGE

    selector = _merge_selector(args)
    view_args = ["pr", "view"] + ([selector] if selector else []) + \
        ["--json", "state,url,headRefName,number"] + repo_tokens
    ok, pr = _gh_read_json(view_args)
    if not ok or not isinstance(pr, dict):
        _err("cannot read the PR to merge; refusing to merge blind.")
        return 1
    branch = pr.get("headRefName") or "unknown-head"

    lockdir, owner = _acquire_lock(repo, branch, session, "pr-merge")
    if lockdir is None:
        return _yield_to(owner or {}, repo, branch, "pr-merge")
    try:
        # Re-read INSIDE the lock: the state may have moved while we waited.
        ok, pr = _gh_read_json(view_args)
        if not ok or not isinstance(pr, dict):
            _err("cannot re-read the PR inside the lock; refusing to merge blind.")
            return 1
        state = (pr.get("state") or "").upper()
        if state != "OPEN":
            _err(
                "REFUSED: PR %s is %s, not OPEN: %s"
                % (pr.get("number", "?"), state or "unknown",
                   pr.get("url", "(url unavailable)"))
            )
            return EXIT_REFUSED

        # The claim comment IS the merge attribution (uniform across merge
        # methods; --rebase has no commit body to carry a trailer). No comment,
        # no merge.
        number = str(pr.get("number", "") or selector)
        comment_rc = _gh_passthrough(
            ["pr", "comment", number,
             "--body", "Merging via gh-safe.\n\nsession: %s" % session]
            + repo_tokens
        )
        if comment_rc != 0:
            _err("claim comment failed (rc=%d); refusing an unattributed merge."
                 % comment_rc)
            return comment_rc or 1

        body = _extract_value_flag(args, ["--body", "-b"])
        if body is not None:
            _, rest = _strip_value_flag(args, ["--body", "-b"])
            args = rest + ["--body", _append_trailer(body, session)]
        return _gh_passthrough(["pr", "merge"] + args)
    finally:
        _release_lock(lockdir)


def main(argv):
    args = list(argv)
    if not args or args[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return EXIT_OK if args else EXIT_USAGE
    if len(args) >= 2 and args[0] == "pr" and args[1] == "create":
        return cmd_create(args[2:])
    if len(args) >= 2 and args[0] == "pr" and args[1] == "merge":
        return cmd_merge(args[2:])
    _err(
        "gh-safe wraps exactly: `pr create`, `pr merge` (the external "
        "mutations). Reads need no wrapper - use plain gh.\n\n" + USAGE
    )
    return EXIT_USAGE


if __name__ == "__main__":
    # House rule: a CLI that may print repo paths/URLs must not crash a cp1252
    # Windows console (see scripts/check-utf8-stdout.py).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
