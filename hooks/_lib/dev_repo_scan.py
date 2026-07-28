"""Shared scanner for in-flight builds across ~/dev/* repos.

Used by:
- block-branch-switch-with-untracked-build.py (PreToolUse)
- nudge-checkpoint-after-pytest-pass.py (PostToolUse)
- warn-uncommitted-builds-on-stop.py (Stop)
- list-wip-stashes-on-session-start.py (SessionStart)

Heuristic for "in-flight build":
- A directory under <repo> contains 3+ untracked source files matching
  one of: .py .ts .tsx .js .jsx .go .rs .java .rb .php .c .cpp .h
- OR a new test file exists alongside a new module (tests/X/* + src/X/*)

Heuristic for "active repo": modified within the last 60 minutes.
Faster than scanning every ~/dev/* repo every session-start.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Optional


SOURCE_EXT_RE = re.compile(
    r"\.(py|ts|tsx|js|jsx|go|rs|java|rb|php|c|cpp|h|hpp|swift|kt|scala)$"
)
# Abandoned-git-lock reclaim (MYC-3175), shared with the install-clone updater.
# Fail-open: a missing sibling must never break the scan.
try:
    from .git_locks import reclaim_stale_git_locks
except ImportError:  # loaded as a plain module (not a package), the common case
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from git_locks import reclaim_stale_git_locks
    except Exception:  # pragma: no cover - heal is best-effort
        def reclaim_stale_git_locks(_repo):
            return []


# ── client-safe configuration (MYC-2070) ─────────────────────────────────────
# This module shipped assuming ONE operator's layout: a hardcoded ~/dev holding
# a ~50-repo fleet. A client install is neither — their code may live anywhere,
# and a 3-repo client shown a fleet banner learns to ignore banners, which is
# the cry-wolf failure the surface exists to prevent. So the two things that
# vary per install are configuration, not constants.
CONFIG_PATH = Path.home() / ".claude" / "dev-hygiene.json"
# A repo that is DELIBERATELY local-only (a scratch pad, a private journal, an
# air-gapped client tree) is not drift. Without an opt-out it would surface as
# NO_REMOTE every session forever — cry-wolf by construction.
NO_REMOTE_OK_MARKER = ".no-remote-ok"


def _config() -> dict:
    """`~/.claude/dev-hygiene.json`, or {} when absent/unreadable. Fail-open:
    dev hygiene must never break because its optional config is malformed."""
    try:
        doc = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def resolve_dev_root() -> Path:
    """Where this install's code repos live: $ABS_DEV_ROOT, else the config's
    `dev_root`, else ~/dev. Resolved per call so a test (and a client who edits
    the config mid-session) sees the change without a reimport."""
    env = os.environ.get("ABS_DEV_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    cfg = _config().get("dev_root")
    if isinstance(cfg, str) and cfg.strip():
        return Path(cfg).expanduser()
    return Path.home() / "dev"


# Repos that legitimately ARE vaults (or ship vault scaffolding), so a
# vault-shaped dir inside them is not the VAULT-ARTIFACT-INSIDE-PRODUCT-REPO
# symptom. Substrate default + per-install `vault_shaped_repos` config, because
# the operator's own vault repo is named whatever they named it.
DEFAULT_VAULT_SHAPED_REPOS = frozenset({"ai-brain-starter"})


def vault_shaped_repos() -> frozenset:
    """Repo names exempt from the stray-vault-dir report (default + config)."""
    extra = _config().get("vault_shaped_repos")
    names = set(DEFAULT_VAULT_SHAPED_REPOS)
    if isinstance(extra, list):
        names.update(e.strip() for e in extra if isinstance(e, str) and e.strip())
    return frozenset(names)


def is_no_remote_ok(repo: Path) -> bool:
    """True when this repo is deliberately local-only and must stay silent.

    Two ways to say so, because the right one differs by who is asking: a
    `.no-remote-ok` file in the repo (travels with the repo, obvious to the next
    person who opens it) or the config's `no_remote_ok` list of names/paths
    (central, for someone who does not want to touch the repos).
    """
    repo = Path(repo)
    try:
        if (repo / NO_REMOTE_OK_MARKER).exists():
            return True
    except OSError:
        pass
    allow = _config().get("no_remote_ok")
    if not isinstance(allow, list):
        return False
    try:
        rp = str(repo.resolve())
    except OSError:
        rp = str(repo)
    for entry in allow:
        if not isinstance(entry, str) or not entry.strip():
            continue
        e = entry.strip()
        if e == repo.name:
            return True
        try:
            if str(Path(e).expanduser().resolve()) == rp:
                return True
        except OSError:
            continue
    return False


# Kept as a module constant for back-compat with callers that read it directly;
# every code path that MATTERS calls resolve_dev_root() so config/env win.
DEV_ROOT = Path.home() / "dev"
ACTIVE_WINDOW_SECONDS = 60 * 60  # 60 minutes
MIN_FILES_FOR_MODULE = 3


class ModuleInFlight(NamedTuple):
    repo: Path
    module_dir: str  # relative to repo, e.g. "src/voice"
    file_count: int
    files: list[str]  # relative to repo


class StashEntry(NamedTuple):
    repo: Path
    stash_ref: str  # e.g. "stash@{0}"
    branch: str
    message: str


def find_active_repos(now_ts: Optional[float] = None) -> list[Path]:
    """Dev-root <repo> dirs whose .git or working tree was touched recently."""
    dev_root = resolve_dev_root()
    if not dev_root.exists():
        return []
    now_ts = now_ts or time.time()
    cutoff = now_ts - ACTIVE_WINDOW_SECONDS
    out: list[Path] = []
    try:
        for child in dev_root.iterdir():
            if not child.is_dir():
                continue
            git_dir = child / ".git"
            if not git_dir.exists():
                continue
            # Cheap activity check: stat the .git/HEAD or working tree mtime
            try:
                head_mtime = (git_dir / "HEAD").stat().st_mtime
                index_mtime = (git_dir / "index").stat().st_mtime if (
                    git_dir / "index"
                ).exists() else 0
                # Also check working-tree top-level for recent edits
                try:
                    wt_mtime = max(
                        (
                            p.stat().st_mtime
                            for p in child.iterdir()
                            if p.name not in {".git", "node_modules", ".venv", "__pycache__"}
                        ),
                        default=0,
                    )
                except OSError:
                    wt_mtime = 0
                last_touch = max(head_mtime, index_mtime, wt_mtime)
                if last_touch >= cutoff:
                    out.append(child)
            except OSError:
                continue
    except OSError:
        return []
    return out


def find_modules_in_flight(repo: Path) -> list[ModuleInFlight]:
    """Run git status -uall in <repo> and group untracked source files by directory.

    Returns one ModuleInFlight per directory with 3+ untracked source files.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1", "-uall"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []

    # Untracked entries start with "?? " in porcelain v1
    untracked_sources: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip()
        # git can emit quoted paths for unicode; strip surrounding quotes
        if rel.startswith('"') and rel.endswith('"'):
            try:
                rel = rel[1:-1].encode("utf-8").decode("unicode_escape")
            except Exception:
                pass
        if SOURCE_EXT_RE.search(rel):
            untracked_sources.append(rel)

    # Group by 2-segment directory prefix (e.g. "src/voice/" or "tests/voice/")
    grouped: dict[str, list[str]] = {}
    for rel in untracked_sources:
        parts = rel.split("/")
        if len(parts) < 2:
            # Top-level untracked file; skip (not module-shaped)
            continue
        # 2-segment prefix; if file is deeper (e.g. src/voice/backends/x.py)
        # we still group at the 2-segment level (src/voice/) because that
        # captures the module identity. Subdirs roll up to the same module.
        prefix = "/".join(parts[:2]) + "/"
        grouped.setdefault(prefix, []).append(rel)

    out: list[ModuleInFlight] = []
    for prefix, files in grouped.items():
        if len(files) >= MIN_FILES_FOR_MODULE:
            out.append(
                ModuleInFlight(
                    repo=repo,
                    module_dir=prefix,
                    file_count=len(files),
                    files=files,
                )
            )
    return out


def find_wip_stashes(repo: Path) -> list[StashEntry]:
    """Return stash entries whose message hints at parking/WIP/incomplete work."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "stash", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []

    pattern = re.compile(
        r"(?i)\b(wip|parking|parked|incomplete|in[- ]flight|do not lose|preserve|recover)\b"
    )
    out: list[StashEntry] = []
    for line in result.stdout.splitlines():
        # Format: stash@{0}: On <branch>: <message>
        m = re.match(r"^(stash@\{\d+\}):\s+(?:On|WIP on)\s+([^:]+):\s+(.*)$", line)
        if not m:
            continue
        stash_ref, branch, message = m.group(1), m.group(2).strip(), m.group(3)
        if pattern.search(message):
            out.append(
                StashEntry(
                    repo=repo,
                    stash_ref=stash_ref,
                    branch=branch,
                    message=message,
                )
            )
    return out


class UnpushedCommitEntry(NamedTuple):
    repo: Path
    branch: str  # local branch with the commits
    commits: list[tuple[str, str, float]]  # (sha7, subject, committer_ts)
    upstream: Optional[str]  # tracking ref, e.g. "origin/main", or None


def find_unpushed_local_commits(
    repo: Path,
    min_age_minutes: int = 24 * 60,
) -> list[UnpushedCommitEntry]:
    """Return local branches with commits not on any remote ref.

    Bug class this surfaces: LOCAL-COMMITS-DRIFT-UNPUSHED. A chore commit
    (e.g. a CI pin from an automated hardening pass) sitting on local main
    unpushed for days blocks fast-forward pulls AND silently corrupts any
    deploy that ships whatever is at local HEAD — the operator's local ends
    up several commits behind origin while production serves the stale tree.
    Filters to commits older than `min_age_minutes` (default 24h) so a
    just-committed change in active work doesn't get flagged as drift.

    Bug class this surfaces: LOCAL-COMMITS-DRIFT-UNPUSHED. Observed
    2026-05-29 on mycelium-site when a gh-harden-repos.sh chore CI pin
    commit (`e40fcc4`) sat on local main unpushed for ~3 days, blocking
    fast-forward pulls AND silently corrupting `vercel --prod` (which
    deploys whatever's at local HEAD). Net effect: production
    /app/digest 404'd for 48h after MYC-56 PR #102 merged because the
    operator's local was 5 commits behind origin.
    """
    try:
        # --branches restricts to commits reachable from local branches.
        # --not --remotes excludes anything already on a remote ref.
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "log",
                "--branches",
                "--not",
                "--remotes",
                "--format=%H%x09%s%x09%ct%x09%D",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []

    cutoff_ts = time.time() - (min_age_minutes * 60)
    by_branch: dict[str, list[tuple[str, str, float]]] = {}

    for line in result.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 3:
            continue
        sha, subject, ct_str = parts[0], parts[1], parts[2]
        refs = parts[3] if len(parts) > 3 else ""
        try:
            ct = float(ct_str)
        except ValueError:
            continue
        if ct > cutoff_ts:
            # Too fresh to nudge — operator may be mid-build.
            continue
        # `refs` is like "HEAD -> main, tag: v1, feature/x". Pull out
        # local branch names; skip remote/tag/HEAD pointers.
        branch_names: list[str] = []
        for ref in refs.split(","):
            ref = ref.strip()
            if not ref or ref.startswith("tag:") or ref.startswith("HEAD"):
                continue
            if ref.startswith("HEAD -> "):
                ref = ref[len("HEAD -> "):]
            if ref.startswith("origin/") or ref.startswith("upstream/"):
                continue
            branch_names.append(ref)
        if not branch_names:
            branch_names = ["(detached)"]
        for b in branch_names:
            by_branch.setdefault(b, []).append((sha[:7], subject, ct))

    out: list[UnpushedCommitEntry] = []
    for branch, commits in by_branch.items():
        upstream = _resolve_upstream(repo, branch)
        out.append(
            UnpushedCommitEntry(
                repo=repo,
                branch=branch,
                commits=commits,
                upstream=upstream,
            )
        )
    return out


def _resolve_upstream(repo: Path, branch: str) -> Optional[str]:
    """Return the tracking ref for a local branch, or None if unset."""
    if branch == "(detached)":
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                f"{branch}@{{u}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    upstream = result.stdout.strip()
    return upstream or None


# ---------------------------------------------------------------------------
# Branch lifecycle classifier (MYC-683) — the shared reap/surface primitive.
#
# The drift surfacer + the reaper both need to tell GENUINE-UNIQUE work (the
# only real drift, the only thing worth surfacing/pushing) apart from
# squash-merged-stale + relic branches (content is already on origin → safe to
# reap, NOT drift). `merge-base --is-ancestor` alone CANNOT — a squash-merged
# branch's commits are not ancestors of main (the squash is a new SHA) yet its
# content IS on main. That false-positive is the ~25x cry-wolf the surfacer
# showed (MYC-683 re-verification 2026-06-14). Five classes, evaluated in order:
# ---------------------------------------------------------------------------


class BranchClass:
    """String enum (kept as plain constants for json/log friendliness)."""

    RELIC = "relic"                          # no common ancestor with origin/main → obsolete
    TRUE_MERGED = "true_merged"              # tip is an ancestor of origin/main
    BACKED_UP = "backed_up"                  # pushed to its own origin/<branch>, not merged
    SQUASH_MERGED_STALE = "squash_merged_stale"  # content ⊆ origin/main (squash artifact)
    GENUINE_UNIQUE = "genuine_unique"        # real divergent work NOT on any remote → the only drift
    NO_REMOTE = "no_remote"                  # repo has NO remote-tracking ref → backed up NOWHERE (strongest drift)
    UNKNOWN = "unknown"                      # can't classify safely → always preserve


# Content is provably preserved on origin → the local branch ref is safe to delete.
REAPABLE_CLASSES = frozenset({BranchClass.TRUE_MERGED, BranchClass.SQUASH_MERGED_STALE})
# Real, un-backed-up state → surface for a human, NEVER auto-delete.
SURFACE_CLASSES = frozenset(
    {BranchClass.RELIC, BranchClass.GENUINE_UNIQUE, BranchClass.NO_REMOTE}
)


def _git(repo: Path, *args: str, timeout: int = 15):
    """Run git, never raise. Returns (returncode, stdout, stderr) trimmed."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 99, "", "git-exec-failed"


# Known gh install locations, searched when gh is not on PATH. The reaper runs
# from launchd (com.abs.dev-repo-reaper) with NO PATH set → the launchd
# default `/usr/bin:/bin:/usr/sbin:/sbin`, which does NOT contain `~/.local/bin`
# where gh lives. A bare subprocess(["gh", ...]) then FileNotFounds and the whole
# merged-detection silently fail-opens — dormant in the ONE place it runs
# unattended (MYC-2365). Resolving an absolute path makes it work regardless of
# how the reaper is invoked. `git` doesn't need this (it's at /usr/bin/git, on
# the default PATH).
_GH_CANDIDATES = (
    Path.home() / ".local" / "bin" / "gh",
    Path("/opt/homebrew/bin/gh"),
    Path("/usr/local/bin/gh"),
    Path("/usr/bin/gh"),
)


def _resolve_gh() -> Optional[str]:
    """Absolute path to the gh entrypoint, or None if it cannot be found.

    `shutil.which("gh")` first (honors PATH for interactive / manual runs), then
    the known install locations (robust to the launchd-default PATH that excludes
    ~/.local/bin — see MYC-2365). None → caller fails OPEN (preserve), same as a
    missing gh always did. Prefers `~/.local/bin/gh` (the MYC-657/793 token-
    unshadow wrapper) so its keyring-pinning protection stays on the path; the
    wrapper then needs a real gh on PATH, which `_gh_env` supplies.
    """
    found = shutil.which("gh")
    if found:
        return found
    for cand in _GH_CANDIDATES:
        if cand.exists():
            return str(cand)
    return None


# Dirs prepended to the gh subprocess PATH. The resolved entrypoint is usually
# the ~/.local/bin/gh WRAPPER, which resolves the real gh via "first PATH hit" —
# so under the launchd-default PATH (no /opt/homebrew/bin) the wrapper itself
# can't find real gh and exits "no real gh binary found on PATH". Supplying these
# dirs lets the wrapper resolve the real gh (/opt/homebrew or /usr/local) and lets
# gh shell out to git. ~/.local/bin first keeps the wrapper preferred. MYC-2365.
_GH_PATH_DIRS = (
    str(Path.home() / ".local" / "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
)


def _gh_env() -> dict:
    """os.environ with the gh/real-gh/git dirs prepended to PATH (MYC-2365)."""
    env = dict(os.environ)
    existing = env.get("PATH", "")
    env["PATH"] = ":".join((*_GH_PATH_DIRS, existing) if existing else _GH_PATH_DIRS)
    return env


def _gh(repo: Path, *args: str, timeout: int = 20):
    """Run gh with cwd=<repo>, never raise. Returns (rc, stdout, stderr) trimmed.

    rc=99 on exec failure (gh missing / OS error / timeout). Mirrors `_git` so
    the authoritative merged-PR fetch fails OPEN exactly like every other probe
    here. gh resolves the GitHub repo from the cwd's origin remote. Resolved by
    ABSOLUTE path (`_resolve_gh`) AND run with an augmented PATH (`_gh_env`) so it
    works under the launchd-default PATH the weekly cron runs with — not just an
    interactive PATH (MYC-2365: the wrapper needs real gh + git reachable).
    """
    gh_bin = _resolve_gh()
    if gh_bin is None:
        return 99, "", "gh-not-found"
    try:
        r = subprocess.run(
            [gh_bin, *args],
            cwd=str(repo),
            env=_gh_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 99, "", "gh-exec-failed"


def detect_default_branch(repo: Path) -> Optional[str]:
    """The remote default branch name (no `origin/` prefix), or None.

    Tries `origin/HEAD`, then verifies `origin/main` / `origin/master`.
    """
    rc, out, _ = _git(repo, "rev-parse", "--abbrev-ref", "origin/HEAD")
    if rc == 0 and out and "/" in out:
        cand = out.split("/", 1)[1]
        rc2, _, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"origin/{cand}")
        if rc2 == 0:
            return cand
    for cand in ("main", "master"):
        rc2, _, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"origin/{cand}")
        if rc2 == 0:
            return cand
    return None


def _branch_adds_nothing(repo: Path, base_ref: str, branch: str) -> Optional[bool]:
    """True if `branch` introduces no content `base_ref` lacks (tree ⊆ base).

    Uses `git diff --numstat base..branch`: each line is `added<TAB>deleted<TAB>path`.
    If the total ADDED lines across all files is 0 AND there is no binary change,
    the branch adds nothing beyond base → its content is already absorbed
    (the squash-merged-stale signal that survives main moving on with its own
    unrelated commits). Returns None on any git error (caller preserves).
    """
    rc, out, _ = _git(repo, "diff", "--numstat", f"{base_ref}..{branch}")
    if rc != 0:
        return None
    if not out:
        return True  # identical trees → adds nothing
    total_added = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        added = parts[0]
        if added == "-":
            return False  # binary delta → can't prove ⊆; treat as genuine (preserve)
        try:
            total_added += int(added)
        except ValueError:
            return False
    return total_added == 0


def fetch_merged_pr_branches(repo: Path) -> Optional[frozenset]:
    """Authoritative set of head-branch names whose PRs are MERGED, via `gh`.

    The squash-merge diff heuristic (`_branch_adds_nothing`) goes FALSE-NEGATIVE
    once origin/main advances on a file a merged branch also touched: the branch's
    now-stale content reads as net additions, so a genuinely-merged branch (and
    its worktree) mis-classifies as GENUINE_UNIQUE and is never reaped. That is
    how one repo piled up 47 merged worktrees (~170G) while the weekly
    reaper ran (MYC-2347). `gh`'s PR state is immune to main moving on — a merged
    PR's headRefName is recorded even after delete_branch_on_merge removes
    origin/<branch>.

    FAIL-OPEN by contract: returns None on ANY failure — gh not installed, not a
    GitHub repo, sandbox-stripped auth (MYC-793), timeout, non-zero exit, or
    unparseable JSON. The caller then falls back to the diff heuristic and
    PRESERVES. This signal only ever ADDS a reap when gh AUTHORITATIVELY confirms
    a branch merged; it never withholds a reap the diff heuristic already allows,
    and it NEVER raises.

    Keyed on headRefName, NOT headRefOid (SHA), deliberately. A SHA-strict match
    is more precise against the name-reuse FP below, but on the real problem repo
    (measured 2026-06-30) 6 of 56 genuinely-merged local branches had a
    tip SHA that had DRIFTED from gh's recorded headRefOid (force-push/rebase
    during the PR) — SHA-strict would FALSE-NEGATIVE all 6 and the bug persists.
    Recall wins: the reaper's harm asymmetry is FN = a lingering worktree
    (recoverable) vs the name-reuse FP = reaping new work on a reused merged name.
    That FP is preempted for any PUSHED reuse by the BACKED_UP check (step 3, runs
    BEFORE this override), never reaps a dirty worktree, and is reflog+manifest
    recoverable for the unpushed remainder (0 occurrences observed across 549
    merged PRs in two repos).
    """
    rc, out, _err = _gh(
        repo, "pr", "list", "--state", "merged",
        "--limit", "1000", "--json", "headRefName",
    )
    if rc != 0 or not out:
        return None
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    names = {
        d["headRefName"]
        for d in data
        if isinstance(d, dict) and d.get("headRefName")
    }
    return frozenset(names) if names else None


def classify_branch(
    repo: Path,
    branch: str,
    default_branch: Optional[str] = None,
    *,
    merged_branches: Optional[frozenset] = None,
) -> str:
    """Classify a local branch into one of BranchClass. Fail-safe to UNKNOWN.

    `merged_branches` (keyword-only, optional): the gh-authoritative set of
    head-branch names whose PRs are MERGED (see `fetch_merged_pr_branches`). When
    a branch is in it, it overrides the squash-merge diff FALSE-NEGATIVE and is
    classed SQUASH_MERGED_STALE (reapable) even after origin/main has advanced
    past the merge (MYC-2347). None / empty → no override → the diff heuristic
    runs (fail-open, preserve). The 4 hooks that share this module never pass it,
    so their behavior is unchanged.
    """
    default_branch = default_branch or detect_default_branch(repo)
    if not default_branch:
        # No remote default branch. Distinguish two very different situations
        # (MYC-1894 — the blind spot that hid 15 un-backed-up commits):
        #   * NO remote-tracking ref at all → this repo's commits are backed up
        #     NOWHERE. The STRONGEST drift signal, not an UNKNOWN. The prior
        #     `return UNKNOWN` here is exactly why three whole repos with no
        #     remote (one +13 commits) never surfaced at all.
        #   * Has remotes but no resolvable default (detached origin/HEAD, a
        #     non-main default we couldn't verify) → can't classify safely →
        #     UNKNOWN (preserve).
        if not repo_has_any_remote_ref(repo):
            return BranchClass.NO_REMOTE
        return BranchClass.UNKNOWN
    origin_main = f"origin/{default_branch}"
    rc, _, _ = _git(repo, "rev-parse", "--verify", "--quiet", origin_main)
    if rc != 0:
        return BranchClass.UNKNOWN
    rc, tip, _ = _git(repo, "rev-parse", "--verify", "--quiet", branch)
    if rc != 0 or not tip:
        return BranchClass.UNKNOWN

    # 1. RELIC — no shared history with origin/main.
    rc_mb, mb, _ = _git(repo, "merge-base", origin_main, branch)
    if rc_mb != 0 or not mb:
        return BranchClass.RELIC

    # 2. TRUE_MERGED — tip already reachable from origin/main.
    rc_anc, _, _ = _git(repo, "merge-base", "--is-ancestor", branch, origin_main)
    if rc_anc == 0:
        return BranchClass.TRUE_MERGED

    # 3. BACKED_UP — pushed to its own upstream (tip == origin/<branch> or behind).
    rc_ob, _, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"origin/{branch}")
    if rc_ob == 0:
        rc_bup, _, _ = _git(
            repo, "merge-base", "--is-ancestor", branch, f"origin/{branch}"
        )
        if rc_bup == 0:
            return BranchClass.BACKED_UP

    # 4. SQUASH_MERGED_STALE — content already on origin/main (squash artifact).
    #    Authoritative override FIRST: if `gh` confirms this branch's PR merged,
    #    it IS absorbed into main even after main advanced on files the branch
    #    also touched — the diff FALSE-NEGATIVE (MYC-2347) that _branch_adds_nothing
    #    cannot see once main moves on. Empty / None merged_branches → no signal →
    #    fall through to the diff heuristic (fail-open, preserve).
    if merged_branches and branch in merged_branches:
        return BranchClass.SQUASH_MERGED_STALE
    adds_nothing = _branch_adds_nothing(repo, origin_main, branch)
    if adds_nothing is True:
        return BranchClass.SQUASH_MERGED_STALE
    if adds_nothing is None:
        return BranchClass.UNKNOWN  # git error → preserve

    # 5. GENUINE_UNIQUE — real divergent, un-backed-up work. The only real drift.
    return BranchClass.GENUINE_UNIQUE


# --- Reap planning ---------------------------------------------------------


@dataclass
class ReapTarget:
    branch: str
    cls: str
    sha: str


@dataclass
class WorktreeTarget:
    path: Path
    branch: str
    sha: str


@dataclass
class StashTarget:
    ref: str
    message: str
    reason: str = ""


@dataclass
class ReapPlan:
    repo: Path
    reap_branches: list = field(default_factory=list)      # ReapTarget
    reap_worktrees: list = field(default_factory=list)     # WorktreeTarget
    reap_stashes: list = field(default_factory=list)       # StashTarget
    surface_branches: list = field(default_factory=list)   # ReapTarget (human decision)
    skipped: list = field(default_factory=list)            # (name, reason)


def _list_worktrees(repo: Path) -> list[tuple[Path, Optional[str]]]:
    """Return [(worktree_path, branch_or_None)] incl. the primary checkout."""
    rc, out, _ = _git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        return []
    res: list[tuple[Path, Optional[str]]] = []
    cur_path: Optional[Path] = None
    cur_branch: Optional[str] = None
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            if cur_path is not None:
                res.append((cur_path, cur_branch))
            cur_path = Path(line[len("worktree "):])
            cur_branch = None
        elif line.startswith("branch "):
            cur_branch = line[len("branch "):].replace("refs/heads/", "")
        elif line == "" and cur_path is not None:
            res.append((cur_path, cur_branch))
            cur_path = None
            cur_branch = None
    return res


def _is_dirty(path: Path) -> bool:
    rc, out, _ = _git(path, "status", "--porcelain")
    return rc != 0 or bool(out)  # treat git error as dirty (preserve)


# A destructive tool errs toward PRESERVE: widen the system's own 300s "live"
# window to 15 min so a session that paused briefly still shields its repo.
SESSION_LOCK_LIVE_WINDOW_SEC = 900


def _has_live_session_lock(repo: Path, now_ts: Optional[float] = None) -> bool:
    """True if a Claude session was active in this repo within the live window.

    Reads the REAL shared lock written by session-lock.py at
    `<main_root>/.claude/.session-lock.json` — a multi-session map whose liveness
    field is `last_activity_at` (NOT `.session-lock`, NOT `pid`). A STALE lock
    (all sessions idle past the window) does NOT count, or the reaper would never
    run on a repo that ever hosted a session. Worktrees share the main lock, so
    checking the primary checkout covers the whole worktree set.
    """
    now_ts = now_ts or time.time()
    lock = repo / ".claude" / ".session-lock.json"
    try:
        data = json.loads(lock.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    for s in data.get("sessions", []):
        la = s.get("last_activity_at") if isinstance(s, dict) else None
        if isinstance(la, (int, float)) and (now_ts - la) < SESSION_LOCK_LIVE_WINDOW_SEC:
            return True
    return False


def _plan_stash_reap(
    repo: Path, keep_k: int, ttl_days: float, now_ts: Optional[float] = None
) -> list[StashTarget]:
    """Reap claude-checkpoint stashes beyond keep-K OR older than TTL.

    NEVER touches named (non-checkpoint) stashes — those are hand-parked work.
    `git stash list` is newest-first, so we keep the first keep_k checkpoint
    entries and reap the rest, plus any checkpoint older than TTL.
    """
    now_ts = now_ts or time.time()
    rc, out, _ = _git(repo, "stash", "list", "--format=%gd%x09%ct%x09%gs")
    if rc != 0 or not out:
        return []
    targets: list[StashTarget] = []
    seen_checkpoints = 0
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        ref, ts_str, msg = parts[0], parts[1], parts[2]
        if PREFIX not in msg:  # PREFIX = "claude-checkpoint"
            continue  # named/parked stash → never reap
        try:
            ts = float(ts_str)
        except ValueError:
            ts = now_ts  # unknown age → don't reap on age grounds
        beyond_k = seen_checkpoints >= keep_k
        too_old = (now_ts - ts) > ttl_days * 86400
        seen_checkpoints += 1
        if beyond_k or too_old:
            reason = "beyond keep-K" if beyond_k else f"older than {ttl_days}d"
            targets.append(StashTarget(ref=ref, message=msg, reason=reason))
    return targets


PREFIX = "claude-checkpoint"  # checkpoint stash message prefix (see create-dev-repo-checkpoint.py)


def plan_repo_reap(
    repo: Path,
    keep_k: int = 10,
    ttl_days: float = 14,
    default_branch: Optional[str] = None,
    now_ts: Optional[float] = None,
    merged_branches: Optional[frozenset] = None,
) -> ReapPlan:
    """Compute (do NOT execute) what is safe to reap in one repo.

    Conservative by construction: reap only TRUE_MERGED + SQUASH_MERGED_STALE
    branches that are not checked out, only CLEAN worktrees on reapable
    branches, only surplus/old checkpoint stashes. Everything else is either
    surfaced for a human (RELIC, GENUINE_UNIQUE) or left untouched (BACKED_UP).
    A live .session-lock skips the whole repo.

    `merged_branches` (optional): the gh-authoritative merged head-branch set for
    this repo (see `fetch_merged_pr_branches`). Threaded into branch + worktree
    classification so a branch/worktree merged BEFORE origin/main advanced past it
    is still reaped (MYC-2347). None → diff heuristic only (fail-open, preserve);
    the CLI computes it once per repo, tests inject it. Kept side-effect-free (no
    gh call here) so the existing hermetic tests are unaffected.
    """
    repo = Path(repo)
    plan = ReapPlan(repo=repo)

    if _has_live_session_lock(repo, now_ts):
        plan.skipped.append((repo.name, "live session-lock — repo skipped wholesale"))
        return plan

    default_branch = default_branch or detect_default_branch(repo)
    if not default_branch:
        # No remote default. If the repo has NO remote-tracking ref at all, its
        # local branches are backed up nowhere → SURFACE them (never reapable:
        # there is no origin to prove content-safe). This is the
        # whole-repo-un-backed-up class the session-start surface also
        # now catches (MYC-1894). Otherwise (remotes exist, default unresolved)
        # skip the repo as before.
        if repo_has_any_remote_ref(repo):
            plan.skipped.append((repo.name, "no origin default branch — repo skipped"))
            return plan
        if is_no_remote_ok(repo):
            plan.skipped.append((repo.name, "deliberately local-only (.no-remote-ok)"))
            return plan
        rc, out, _ = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
        for branch in out.splitlines():
            branch = branch.strip()
            if not branch:
                continue
            rc_s, sha, _ = _git(repo, "rev-parse", "--short", branch)
            plan.surface_branches.append(
                ReapTarget(branch=branch, cls=BranchClass.NO_REMOTE, sha=sha)
            )
        return plan

    worktrees = _list_worktrees(repo)
    rc, primary, _ = _git(repo, "rev-parse", "--show-toplevel")
    primary_path = Path(primary) if rc == 0 and primary else repo
    checked_out = {b: p for (p, b) in worktrees if b}

    rc, out, _ = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    for branch in out.splitlines():
        branch = branch.strip()
        if not branch or branch == default_branch:
            continue  # never touch the default branch
        cls = classify_branch(repo, branch, default_branch, merged_branches=merged_branches)
        rc_s, sha, _ = _git(repo, "rev-parse", "--short", branch)
        tgt = ReapTarget(branch=branch, cls=cls, sha=sha)
        if cls in REAPABLE_CLASSES:
            if branch in checked_out:
                plan.skipped.append(
                    (branch, f"{cls} but checked out at {checked_out[branch]} — branch kept")
                )
            else:
                plan.reap_branches.append(tgt)
        elif cls in SURFACE_CLASSES:
            plan.surface_branches.append(tgt)
        # BACKED_UP / UNKNOWN → no-op

    for wt_path, wt_branch in worktrees:
        if wt_branch is None:
            continue  # detached HEAD worktree → leave for human
        if wt_path.resolve() == primary_path.resolve():
            continue  # never reap the primary checkout
        if wt_branch == default_branch:
            continue
        cls = classify_branch(repo, wt_branch, default_branch, merged_branches=merged_branches)
        if cls not in REAPABLE_CLASSES:
            continue
        if _is_dirty(wt_path):
            plan.skipped.append(
                (str(wt_path), f"worktree on {cls} branch but DIRTY — never --force")
            )
            continue
        rc_s, sha, _ = _git(repo, "rev-parse", "--short", wt_branch)
        # Removing the worktree FREES its branch; the (now not-checked-out) branch
        # is reaped on the NEXT run's branch loop — a documented self-heal, so this
        # run stays a minimal worktree-only removal (MYC-2347). The branch ref is
        # ~40 bytes; the 170G harm is the worktree, removed here.
        plan.reap_worktrees.append(WorktreeTarget(path=wt_path, branch=wt_branch, sha=sha))

    plan.reap_stashes = _plan_stash_reap(repo, keep_k, ttl_days, now_ts)
    return plan


def _stash_index(ref: str) -> int:
    m = re.search(r"\{(\d+)\}", ref)
    return int(m.group(1)) if m else 0


def _resolve_ls_sweep_tool() -> Optional[Path]:
    """Locate ls-dead-handler-sweep.py (deployed or in-repo dev checkout).
    Mirrors dev-repo-reaper.py's own _add_lib_to_path candidate-search pattern
    (deployed path first, in-repo bin/ as the dev-checkout fallback)."""
    candidates = [
        os.environ.get("LS_SWEEP_TOOL"),
        str(Path.home() / ".local" / "bin" / "ls-dead-handler-sweep.py"),  # deployed
        str(Path(__file__).resolve().parent.parent.parent / "bin" / "ls-dead-handler-sweep.py"),  # in-repo
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def _ls_unregister_stale_bundles(path: Path) -> None:
    """Best-effort: unregister any *.app bundles under `path` from LaunchServices
    BEFORE it's deleted, so a reaped worktree doesn't leave a dead URL-scheme
    handler behind for macOS to route deep-links to later (MYC-2626). ALWAYS
    fail-open -- the worktree reap already works without this; this is pure
    hygiene riding along, and must never be allowed to block or error it.
    """
    tool = _resolve_ls_sweep_tool()
    if tool is None:
        return
    try:
        subprocess.run(
            [sys.executable, str(tool), "--unregister-path", str(path)],
            capture_output=True, timeout=45,
        )
    except Exception:
        pass


def execute_reap(plan: ReapPlan, apply: bool = False) -> dict:
    """Build a recovery manifest and, only when apply=True, perform the reaps.

    Order: worktrees first (frees their branch), then branches, then stashes
    (highest index first so lower indices stay valid mid-drop). Branch delete is
    `-D` — safe because the class proves the content is on origin. Worktree
    remove is WITHOUT `--force` (git refuses if it somehow turned dirty between
    plan and apply — fail safe). The manifest is ALWAYS returned (even dry-run)
    so a caller can persist it before destruction.
    """
    manifest: dict = {
        "repo": str(plan.repo),
        "applied": apply,
        "branches": [],
        "worktrees": [],
        "stashes": [],
    }
    for wt in plan.reap_worktrees:
        manifest["worktrees"].append(
            {"path": str(wt.path), "branch": wt.branch, "sha": wt.sha}
        )
        if apply:
            _ls_unregister_stale_bundles(wt.path)  # MYC-2626: before removal, not after
            _git(plan.repo, "worktree", "remove", str(wt.path))  # no --force
    for t in plan.reap_branches:
        manifest["branches"].append({"branch": t.branch, "cls": t.cls, "sha": t.sha})
        if apply:
            _git(plan.repo, "branch", "-D", t.branch)
    for s in sorted(plan.reap_stashes, key=lambda x: _stash_index(x.ref), reverse=True):
        manifest["stashes"].append(
            {"ref": s.ref, "message": s.message, "reason": s.reason}
        )
        if apply:
            _git(plan.repo, "stash", "drop", s.ref)
    return manifest


def has_recent_session_commits(repo: Path, minutes: int = 30) -> bool:
    """True if at least one commit landed in the last <minutes>. Used to suppress
    nudges right after the user has actually committed.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "log",
                f"--since={minutes}.minutes.ago",
                "--oneline",
                "-1",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


# ---------------------------------------------------------------------------
# Fleet discovery + fetch + drift collection (MYC-1894).
#
# The session-start unpushed surface used to scan only repos touched in the last
# 60 min (find_active_repos) — so COLD repos with stranded commits never
# surfaced. discover_primary_repos() is the SHARED, worktree-deduped, full-fleet
# discovery that BOTH the surface AND dev-repo-reaper.py use — one source of
# truth, so a future coverage change happens in ONE place, not two drifting
# copies (the second drift hole this ticket is itself an instance of).
# ---------------------------------------------------------------------------


def _common_git_dir(child: Path):
    """(child, canonical common-git-dir) or None when it isn't a git checkout."""
    git_marker = child / ".git"
    try:
        if not git_marker.exists():
            return None
    except OSError:
        return None
    rc, common, _ = _git(child, "rev-parse", "--git-common-dir")
    if rc != 0 or not common:
        return None
    if not os.path.isabs(common):
        common = os.path.normpath(os.path.join(child, common))
    return child, os.path.realpath(common)


def discover_primary_repos(
    dev_root: Optional[Path] = None, max_workers: int = 8
) -> list[Path]:
    """Return one primary checkout per repo identity under the dev root.

    Worktrees share a common git dir; group by it and pick the primary checkout
    (the one whose .git is a real directory) so each repo is scanned exactly once
    (refs/stashes are shared across its worktree set). Covers EVERY git repo
    under the root — no activity-window filter, no hardcoded subset.

    The root defaults to `resolve_dev_root()` (env / config / ~/dev) rather than
    a hardcoded path, and the per-child `git rev-parse` runs in PARALLEL: it was
    O(#dirs) SERIAL subprocesses, which is invisible on a warm cache and is a
    slow session start on a client's cold, large, or network-backed tree
    (MYC-2070). Order is restored by sorting, so the result is deterministic.
    """
    dev_root = Path(dev_root) if dev_root is not None else resolve_dev_root()
    if not dev_root.exists():
        return []
    by_common: dict = {}
    try:
        children = sorted(p for p in dev_root.iterdir() if p.is_dir())
    except OSError:
        return []
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            probed = list(ex.map(_common_git_dir, children))
    except Exception:
        probed = [_common_git_dir(c) for c in children]
    for hit in probed:
        if hit is None:
            continue
        child, common = hit
        git_marker = child / ".git"
        # Canonicalize the dedup KEY (the returned path is still `child`): git
        # reports a worktree's common-dir as a realpath but a primary's as a
        # relative join, so on symlinked roots (macOS /var↔/private/var) the two
        # would not group without this. The dev root isn't symlinked in the
        # common case, but keying on realpath makes the dedup robust everywhere.
        # Canonicalize the dedup KEY (the returned path is still `child`): git
        # reports a worktree's common-dir as a realpath but a primary's as a
        # relative join, so on symlinked roots (macOS /var↔/private/var) the two
        # would not group without this. ~/dev isn't symlinked in prod, but keying
        # on realpath makes the worktree dedup robust everywhere.
        # Primary checkout: .git is a directory (worktrees have a .git FILE).
        is_primary = git_marker.is_dir()
        if common not in by_common or is_primary:
            by_common[common] = child
    return sorted(set(by_common.values()))


def repo_has_any_remote_ref(repo: Path) -> bool:
    """True if the repo has at least one remote-tracking ref (a known backup).

    Emptiness here is the harm signal MYC-1894 catches: NOTHING in this repo is
    on any remote we know about, so every local commit is backed up nowhere.
    Keys on refs/remotes/ (not `git remote`) so a configured-but-never-pushed
    origin still reads as un-backed-up.
    """
    rc, out, _ = _git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/")
    return rc == 0 and bool(out.strip())


# Frequency-capped fetch state — so session-start does NOT fetch the whole fleet
# every launch (only once per cap window). The local coverage scan still runs
# every session; only NETWORK freshness is throttled.
FETCH_STATE_PATH = Path.home() / ".claude" / ".drift-fetch-state.json"


def fetch_repos_capped(
    repos: list[Path],
    cap_hours: float = 4.0,
    timeout: int = 6,
    max_workers: int = 8,
    state_path: Path = FETCH_STATE_PATH,
    now_ts: Optional[float] = None,
) -> tuple[set, bool]:
    """Fetch origin on each repo, at most once per cap_hours (frequency cap).

    Returns (failed_repos, did_fetch):
      * failed_repos: repos WITH an origin whose fetch errored this run — the
        caller surfaces them so a stale-ref miss is never silent (fail-loud).
      * did_fetch: True if a fetch pass actually ran (False = within the cap;
        refs reused from the last fetch).

    Coverage (the local scan) is independent of this cap — only network freshness
    is throttled. Bounded + parallel + per-repo timeout so it can never hang the
    session: each git fetch self-limits via `timeout`; repos with no origin are
    skipped (not counted as failures — they have no refs to be stale).
    """
    now_ts = now_ts or time.time()
    try:
        last = float(json.loads(state_path.read_text()).get("last_fetch_ts", 0))
    except Exception:
        last = 0.0
    if now_ts - last < cap_hours * 3600:
        return set(), False  # within cap → reuse last-fetched refs

    def _one(r: Path):
        rc0, out0, _ = _git(r, "remote")
        if rc0 != 0 or "origin" not in out0.split():
            return r, None  # no origin → nothing to fetch, not a failure
        rc, _, _ = _git(r, "fetch", "--quiet", "origin", timeout=timeout)
        return r, (rc == 0)

    failed: set = set()
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for r, ok in ex.map(_one, repos):
                if ok is False:
                    failed.add(r)
    except Exception:
        return failed, True

    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"last_fetch_ts": now_ts}))
    except Exception:
        pass
    return failed, True


def _no_remote_repo_drift(repo: Path, min_age_minutes: int) -> Optional[tuple]:
    """One drift entry for a whole no-remote repo (backed up nowhere).

    Attributes to the repo's REAL current branch (not the `%D`-label artifact)
    and counts all un-backed-up commits older than the age floor, so a brand-new
    repo mid-first-commit is not nagged. Returns None if nothing qualifies.
    """
    rc, head, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = head if rc == 0 and head and head != "HEAD" else "(detached HEAD)"
    cutoff = time.time() - min_age_minutes * 60
    rc, out, _ = _git(repo, "log", "--branches", "--not", "--remotes", "--format=%ct")
    if rc != 0 or not out.strip():
        return None
    n = 0
    for line in out.splitlines():
        try:
            ct = float(line)
        except ValueError:
            ct = 0.0  # unparseable → treat as old (count it)
        if ct <= cutoff:
            n += 1
    if n == 0:
        return None
    return (repo.name, branch, n, BranchClass.NO_REMOTE)


def _unpushed_count_on_ref(repo: Path, ref: str, min_age_minutes: int) -> int:
    """Commits reachable from <ref> that are on NO remote, older than the age
    floor. One git call. Accurate (no `%D` label artifact)."""
    cutoff = time.time() - min_age_minutes * 60
    rc, out, _ = _git(repo, "log", ref, "--not", "--remotes", "--format=%ct")
    if rc != 0 or not out.strip():
        return 0
    n = 0
    for line in out.splitlines():
        try:
            ct = float(line)
        except ValueError:
            ct = 0.0  # unparseable → treat as old (count it)
        if ct <= cutoff:
            n += 1
    return n


def _count_topic_branches_with_unpushed(repo: Path, default: Optional[str]) -> int:
    """Cheap (ONE git call) count of NON-default local branches whose tips carry
    commits not on any remote.

    APPROXIMATE BY DESIGN — does NOT classify merged-stale vs genuine vs relic
    (that is the reaper's precise, O(branches)-git-call job; doing it here, on
    every session start, is what made the full-fleet scan slow enough to risk a
    hook-timeout → fail-open → silent coverage loss). This is only the
    'N branches to review with dev-repo-reaper.py' pointer.
    """
    rc, out, _ = _git(
        repo, "log", "--branches", "--not", "--remotes",
        "--simplify-by-decoration", "--format=%D",
    )
    if rc != 0 or not out.strip():
        return 0
    seen: set = set()
    for line in out.splitlines():
        for ref in line.split(","):
            ref = ref.strip()
            if not ref or ref.startswith("tag:") or ref.startswith("HEAD"):
                continue
            if ref.startswith("HEAD -> "):
                ref = ref[len("HEAD -> "):]
            if ref.startswith("origin/") or ref.startswith("upstream/"):
                continue
            if default and ref == default:
                continue  # the default branch is headlined separately
            seen.add(ref)
    return len(seen)


def _repo_drift(repo: Path, min_age_minutes: int) -> tuple[list, int]:
    """Drift for ONE repo → (genuine_entries, n_topic). See collect_drift."""
    repo = Path(repo)  # tolerate str paths like the sibling scanners do
    if not repo_has_any_remote_ref(repo):
        # Deliberately local-only → not drift, and nagging about it every
        # session is how a real signal gets trained into background noise
        # (MYC-2070 client-safe posture).
        if is_no_remote_ok(repo):
            return [], 0
        # Whole repo un-backed-up → report once, cleanly, on its real branch.
        entry = _no_remote_repo_drift(repo, min_age_minutes)
        return ([entry] if entry else []), 0
    default = detect_default_branch(repo)
    g: list = []
    if default:
        n_def = _unpushed_count_on_ref(repo, default, min_age_minutes)
        if n_def > 0:
            g.append((repo.name, default, n_def, BranchClass.GENUINE_UNIQUE))
    return g, _count_topic_branches_with_unpushed(repo, default)


def collect_drift(
    repos: list[Path], min_age_minutes: int = 24 * 60, max_workers: int = 8
) -> tuple[list, int]:
    """Fast, O(#repos)-git-call drift scan for the session-start banner.

    Headlines ONLY real LOST-WORK risk — computed cheaply + precisely + in
    PARALLEL so the hook can never grow slow enough to time out and fail open
    (which would silently REINTRODUCE the coverage hole this surface exists to
    close):
      * NO_REMOTE repos — the whole repo is backed up nowhere.
      * Unpushed commits on the repo's DEFAULT branch (main/master) — the
        backbone; this is the MYC-683 incident class (unpushed `main` silently
        corrupting a deploy).

    Demotes everything else to a single COUNT (n_topic): non-default local TOPIC
    branches carrying commits not on any remote. dev-repo-reaper.py classifies +
    drains those precisely on demand; listing all ~150 of them in a session-start
    banner would cry wolf (the EXACT failure MYC-683 exists to prevent).

    Returns (genuine, n_topic). genuine items are (repo_name, branch, n, cls),
    in input-repo order. Shared by the surface and its tests so the buckets are
    proven, not re-implemented per consumer.
    """
    if not repos:
        return [], 0
    # Pure read-only git I/O per repo → parallelize (ex.map preserves order).
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            results = list(ex.map(lambda r: _repo_drift(r, min_age_minutes), repos))
    except Exception:
        results = [_repo_drift(r, min_age_minutes) for r in repos]
    genuine: list = []
    n_topic = 0
    for g, t in results:
        genuine.extend(g)
        n_topic += t
    return genuine, n_topic


# ---------------------------------------------------------------------------
# Claim-bearing branch surface (MYC-3290).
#
# collect_drift above headlines exactly two classes (no-remote repos, unpushed
# commits on a DEFAULT branch) and folds everything else into one suppressed
# count. That volume-based demotion is right for cry-wolf reasons and wrong on
# one axis: a branch named `claude/myc-3252-…` is not topic-branch noise. The
# ticket ID in the name is an explicit statement that this is CLAIMED WORK
# against a tracked issue — the highest-value stranded thing in the tree, and
# precisely what today's policy suppresses.
#
# Measured, twice, on the same class:
#   * MYC-3252 (2026-07-24) — `claude/myc-3252-inkcap-generate-dead` held a
#     complete, tested P1 fix plus a novel CI gate. UNPUSHED, session dead 9h. A
#     second session was on course to re-derive it from scratch; what caught it
#     was the concurrent-session check firing on the local worktree path, which
#     only worked because the worktree happened to still be on disk.
#   * MYC-86 (2026-07-27) — `origin/claude/myc-86-consent-pause` held 1292
#     insertions already through adversarial review. PUSHED, 15h, NO PR, because
#     that session hit its usage limit mid-flight.
#
# The second case widens the predicate and is why this is not simply an
# unpushed-branch feature: pushed-with-no-PR is invisible to a DIFFERENT set of
# detectors, which is why it survived. dev-repo-reaper sees a pushed branch as
# healthy (nothing is at risk of loss); the unpushed surface skips it for the
# same reason; nothing in the PR-watching path sees it because there is no PR.
# It falls in the gap between "safe from loss" and "visible to review" — the
# unpushed case at least trips a loss-risk detector, this one trips nothing.
#
# Volume is a SECOND promotion axis, not a replacement: a 2026-07-25 sample of
# 40 `claude/*` branches on one repo found 3 with no PR ever, one of them
# `claude/adoring-hypatia-f30d1a` carrying 9 commits under a generated name — the
# largest stranded thing on disk and exactly what a name-only rule fails to see.
#
# Bug class CLAIMED-WORK-STRANDED-UNPUSHED-AND-INVISIBLE.
# ---------------------------------------------------------------------------


# Tracker IDs that mark a branch as a claim. Case-insensitive and matched in ANY
# path segment, so `claude/myc-3252-slug`, `codex/MYC-3252`, and a bare
# `ond-1250-fix` all resolve.
TICKET_RE = re.compile(r"(?i)\b(MYC|OND|AB)-(\d+)\b")

# Age floor, measured on the branch TIP's committer timestamp. This is the
# negative control's live-session axis and it is self-regulating: a session
# actively committing keeps resetting its own clock, so it is never nagged about
# the branch it is working on, while a dead session's branch ages past the floor
# on its own. Deliberately NOT keyed on the repo's .session-lock — worktrees
# share one lock per repo, so on a busy repo a single live
# session would suppress every claim branch in it, which is the whole failure.
CLAIM_MIN_AGE_HOURS = 6.0
# Second promotion axis (see header): an unnamed branch this far ahead of the
# default branch is promoted on volume alone.
CLAIM_VOLUME_MIN_COMMITS = 5


class ClaimState:
    """Which side of the pipeline the claimed work is stranded on."""

    UNPUSHED = "unpushed"            # commits on no remote -> also a LOSS risk
    PUSHED_NO_PR = "pushed_no_pr"    # pushed + ahead of default + no PR ever opened


@dataclass
class ClaimBranch:
    repo: str
    branch: str
    ticket: Optional[str]      # normalized "MYC-3252", or None when promoted on volume
    state: str                 # ClaimState
    commits: int               # ahead of the default branch
    age_hours: float           # since the tip's committer timestamp
    subject: str               # tip commit subject — tells finished from mid-edit
    tree: str                  # tip tree sha, for identical-content dedupe
    promoted_by: str           # "ticket-id" | "volume"
    duplicates: list = field(default_factory=list)  # sibling branch names, same tree


def extract_ticket_id(branch: str) -> Optional[str]:
    """Normalized tracker ID carried by a branch name, or None.

    `claude/myc-3252-inkcap-generate-dead` -> "MYC-3252".
    """
    m = TICKET_RE.search(branch or "")
    return f"{m.group(1).upper()}-{m.group(2)}" if m else None


def is_claim_bearing(branch: str) -> bool:
    """True when a branch name states a claim against a tracked issue."""
    return extract_ticket_id(branch) is not None


def fetch_pr_head_branches(repo: Path) -> Optional[frozenset]:
    """Every head-branch name that has EVER had a PR, any state, via `gh`.

    Distinct from fetch_merged_pr_branches (merged only) because the question
    here is the opposite one: not "did this land?" but "was it ever offered for
    review at all?". A closed-without-merge PR still counts as offered — the work
    was seen and rejected, which is not stranded.

    FAIL-CLOSED FOR THE CALLER BY CONTRACT: returns None on ANY failure (gh
    missing, not a GitHub repo, sandbox-stripped auth, timeout, bad JSON) and the
    caller then reports NO pushed-no-PR findings for that repo. Absence of
    evidence is not evidence of absence: claiming "no PR exists" from a failed
    lookup would assert external state we never read. The unpushed half of the
    surface is pure local git and is unaffected.
    """
    rc, out, _err = _gh(
        repo, "pr", "list", "--state", "all",
        "--limit", "1000", "--json", "headRefName",
    )
    if rc != 0 or not out:
        return None
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    names = {
        d["headRefName"]
        for d in data
        if isinstance(d, dict) and d.get("headRefName")
    }
    return frozenset(names)


def _clean_subject(subject: str, limit: int = 120) -> str:
    """A commit subject safe to drop into the SessionStart banner.

    Strips non-printable characters and truncates. This text is embedded in the
    JSON the harness parses and comes from whatever any branch's tip commit
    happens to say, so an unescaped control byte is a correctness problem rather
    than a cosmetic one — a real one broke JSON parsing on the first live run.
    """
    cleaned = "".join(ch for ch in (subject or "") if ch.isprintable()).strip()
    return cleaned[: limit - 1] + "…" if len(cleaned) > limit else cleaned


def _candidate_claim_refs(repo: Path, default: str) -> list:
    """[(branch, ref, tip_ts, subject)] for refs NOT merged into origin/<default>.

    ONE `for-each-ref` per scope — name, tip timestamp and subject together, with
    `--no-merged` doing the "is this ahead of the default branch?" prune inside
    git instead of as a rev-list per branch.

    That prune is the whole performance story. The first version asked git three
    questions per candidate ref across every ~/dev repo and took 68s on the real
    fleet. A SessionStart hook that slow gets killed, fails open, and silently
    restores the very coverage hole it exists to close — so the cost had to come
    out of the algorithm, not out of the coverage.

    Local branches UNION origin/* heads: remote refs matter because the MYC-86
    shape lives there (pushed from a session whose worktree is gone). A branch on
    both is listed once, read from the LOCAL ref (at least as new).
    """
    seen: dict = {}
    # %(tree) comes back in this same call, so the per-ref `rev-parse ref^{tree}`
    # the dedupe needs disappears entirely.
    fmt = (
        "--format=%(refname:short)%09%(committerdate:unix)"
        "%09%(tree)%09%(contents:subject)"
    )
    for scope in ("refs/heads/", "refs/remotes/origin/"):
        rc, out, _ = _git(
            repo, "for-each-ref", "--no-merged", f"origin/{default}", fmt, scope
        )
        if rc != 0:
            continue
        for line in out.splitlines():
            parts = line.split("\t", 3)
            if len(parts) < 3:
                continue
            name, ref = parts[0].strip(), parts[0].strip()
            try:
                ts = float(parts[1])
            except ValueError:
                continue
            tree = parts[2].strip()
            subject = parts[3] if len(parts) > 3 else ""
            if not tree:
                continue
            if scope.endswith("origin/"):
                if not name.startswith("origin/"):
                    continue
                name = name[len("origin/"):]
                if name in seen:
                    continue  # local ref already recorded and is at least as new
            if not name or name == default or name == "HEAD":
                continue
            seen[name] = (ref, ts, tree, subject)
    return sorted((n, r, t, tr, s) for n, (r, t, tr, s) in seen.items())


def _claim_branches_for_repo(
    repo: Path,
    *,
    min_age_hours: float,
    volume_min_commits: int,
    now_ts: Optional[float] = None,
    deadline: Optional[float] = None,
) -> list:
    """Stranded claim-bearing branches in ONE repo. Read-only; never raises."""
    repo = Path(repo)
    now_ts = now_ts or time.time()
    default = detect_default_branch(repo)
    if not default:
        return []  # no origin default -> the no-remote surface already owns this repo

    origin_default = f"origin/{default}"
    found: list = []
    pushed_pending: list = []

    for branch, ref, tip_ts, tree, raw_subject in _candidate_claim_refs(repo, default):
        if deadline and time.time() > deadline:
            break
        # Age and name are already in hand from the single for-each-ref, so both
        # cheap filters run BEFORE any git call: a live session's in-flight
        # branch and the ordinary topic-branch bulk cost nothing at all.
        age_hours = (now_ts - tip_ts) / 3600.0
        if age_hours < min_age_hours:
            continue  # a live session's own branch -> never nag it
        ticket = extract_ticket_id(branch)
        promoted_by = "ticket-id" if ticket is not None else "volume"

        rc, out, _ = _git(repo, "rev-list", "--count", f"{origin_default}..{ref}")
        if rc != 0:
            continue
        try:
            ahead = int(out.strip())
        except ValueError:
            continue
        if ahead == 0:
            continue  # nothing the default branch lacks -> not stranded work
        if ticket is None and ahead < volume_min_commits:
            continue  # ordinary topic branch -> stays in the suppressed count

        # UNPUSHED whenever any commit sits on no remote at all (also a loss
        # risk); otherwise the work is on a remote and the question becomes
        # whether it was ever offered for review.
        rc, out, _ = _git(repo, "rev-list", "--count", ref, "--not", "--remotes")
        try:
            unpushed = int(out.strip()) if rc == 0 else 0
        except ValueError:
            unpushed = 0

        entry = ClaimBranch(
            repo=repo.name, branch=branch, ticket=ticket,
            state=ClaimState.UNPUSHED if unpushed else ClaimState.PUSHED_NO_PR,
            commits=ahead, age_hours=age_hours,
            subject=_clean_subject(raw_subject), tree=tree,
            promoted_by=promoted_by,
        )
        (found if unpushed else pushed_pending).append(entry)

    # One gh call per repo, and ONLY when a pushed candidate exists — most repos
    # have none, so the network cost stays near zero on a full-fleet scan.
    if pushed_pending and not (deadline and time.time() > deadline):
        pr_heads = fetch_pr_head_branches(repo)
        if pr_heads is not None:  # None -> cannot prove "no PR" -> report nothing
            found.extend(e for e in pushed_pending if e.branch not in pr_heads)

    return _dedupe_claims(found)


def _dedupe_claims(entries: list) -> list:
    """Collapse branches holding IDENTICAL content into one entry.

    Two branches held byte-identical work for MYC-3179 on 2026-07-25; a surface
    that lists both without saying they are the same wastes the reader's
    judgment, which is the scarce resource here. Grouped by (ticket, tree) —
    same claim AND same tip tree — keeping the entry with the most commits and
    naming the rest as duplicates.
    """
    by_key: dict = {}
    for e in entries:
        key = (e.ticket or f"~{e.branch}", e.tree)
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = e
        elif e.commits > prev.commits:
            e.duplicates = prev.duplicates + [prev.branch]
            by_key[key] = e
        else:
            prev.duplicates.append(e.branch)
    return sorted(
        by_key.values(), key=lambda e: (-e.age_hours, e.repo, e.branch)
    )


def collect_claim_bearing(
    repos: list,
    *,
    min_age_hours: Optional[float] = None,
    volume_min_commits: Optional[int] = None,
    max_workers: int = 8,
    now_ts: Optional[float] = None,
    budget_sec: Optional[float] = None,
) -> list:
    """Stranded claim-bearing branches across the fleet, oldest first.

    Thresholds fall back to CLAIM_BRANCH_MIN_AGE_HOURS / CLAIM_BRANCH_MIN_COMMITS
    in the environment so the negative control can drive them without editing
    code. Parallel + read-only; any repo that errors contributes nothing rather
    than failing the scan.

    HARD WALL-CLOCK BUDGET (CLAIM_BRANCH_BUDGET_SEC, default 15s). The algorithm
    is fast now, but "fast on this fleet today" is not a guarantee — repo count
    and branch count only grow, and a hung `gh` on a flaky network answers to no
    algorithm. Past the deadline the scan stops and returns what it has. Partial
    output is the right failure mode for a surfacer: a hook that overruns is
    killed and reports NOTHING, which is indistinguishable from a clean fleet and
    is exactly the silent coverage loss this whole surface exists to prevent.
    """
    if not repos:
        return []
    if budget_sec is None:
        try:
            budget_sec = float(os.environ.get("CLAIM_BRANCH_BUDGET_SEC", 15.0))
        except (TypeError, ValueError):
            budget_sec = 15.0
    deadline = time.time() + budget_sec if budget_sec > 0 else None
    if min_age_hours is None:
        try:
            min_age_hours = float(os.environ.get("CLAIM_BRANCH_MIN_AGE_HOURS", CLAIM_MIN_AGE_HOURS))
        except (TypeError, ValueError):
            min_age_hours = CLAIM_MIN_AGE_HOURS
    if volume_min_commits is None:
        try:
            volume_min_commits = int(
                os.environ.get("CLAIM_BRANCH_MIN_COMMITS", CLAIM_VOLUME_MIN_COMMITS)
            )
        except (TypeError, ValueError):
            volume_min_commits = CLAIM_VOLUME_MIN_COMMITS

    def _one(r):
        try:
            return _claim_branches_for_repo(
                r, min_age_hours=min_age_hours,
                volume_min_commits=volume_min_commits, now_ts=now_ts,
                deadline=deadline,
            )
        except Exception:
            return []

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            results = list(ex.map(_one, repos))
    except Exception:
        results = [_one(r) for r in repos]

    flat: list = []
    for r in results:
        flat.extend(r)
    return sorted(flat, key=lambda e: (-e.age_hours, e.repo, e.branch))


def format_claim_section(entries: list, limit: int = 10) -> Optional[str]:
    """The headlined claim-bearing block, or None when there is nothing to say.

    TICKET-BEARING branches are headlined individually; VOLUME-promoted ones are
    demoted to a single counted line. That split is the ticket's own framing —
    "volume is the wrong axis; claim-bearing-ness is the right one" — and it is
    load-bearing, not cosmetic. Measured on the real fleet: 20 ticket-bearing vs
    16 volume-only, and the volume-only set was topped by 364/330/146-commit
    ancient forks. Listing all 36 as equals buries the 20 real claims under
    relics and rebuilds the exact cry-wolf banner MYC-683 exists to prevent.
    Volume still earns full protection in the REAPER guard, which is where the
    follow-up comment actually asked for it (item 3) — dev-repo-reaper.py can
    classify a RELIC properly, and this surfacer deliberately does not try.
    """
    if not entries:
        return None
    ticketed = [e for e in entries if e.ticket]
    volume_only = [e for e in entries if not e.ticket]
    if not ticketed:
        # Nothing claimed against a tracked issue. The volume-only set is the
        # reaper's job, and it is not worth a banner of its own.
        return None

    n = len(ticketed)
    plural = "es" if n != 1 else ""
    lines = [
        "[claimed-work-stranded]",
        "",
        (
            f"{n} branch{plural} carry CLAIMED work against a tracked issue that no "
            f"other session can act on. A fresh session's default path is to write it "
            f"AGAIN from scratch; the cost is duplicated judgment, not lost bytes. "
            f"Check here BEFORE starting work on any ticket named below."
        ),
        "",
    ]
    for e in ticketed[:limit]:
        who = e.ticket or f"unnamed, {e.commits} commits"
        if e.state == ClaimState.UNPUSHED:
            where = "UNPUSHED — invisible to every other session, and backed up nowhere"
        else:
            where = "pushed, NO PR ever opened — safe from loss, invisible to review"
        age = (
            f"{e.age_hours / 24:.1f}d" if e.age_hours >= 48 else f"{e.age_hours:.0f}h"
        )
        lines.append(f"- **{who}** · `{e.repo}` :: `{e.branch}`")
        lines.append(f"    {e.commits} commit(s), idle {age} — {where}")
        # The subject alone usually settles finished-vs-abandoned, which is the
        # judgment the reader has to make (MYC-86: "close the two gaps
        # adversarial review found" reads as done, not mid-edit).
        lines.append(f'    last commit: "{e.subject}"')
        if e.duplicates:
            dupes = ", ".join(f"`{d}`" for d in e.duplicates)
            lines.append(f"    identical content also on: {dupes} (same tree — one piece of work)")
    if n > limit:
        lines.append(f"- ... and {n - limit} more claim-bearing branch(es)")
    if volume_only:
        biggest = max(e.commits for e in volume_only)
        lines += [
            "",
            (
                f"({len(volume_only)} unnamed branch(es) also hold >= "
                f"{CLAIM_VOLUME_MIN_COMMITS} unmerged commits, largest {biggest} — "
                f"NOT headlined: no ticket ID, and many are ancient forks. "
                f"`dev-repo-reaper.py` classifies relic-vs-genuine properly. They "
                f"are still protected from the reaper.)"
            ),
        ]
    lines += [
        "",
        (
            "**Per branch:** read it before rebuilding it — `git log origin/main..<branch>` "
            "then `git diff origin/main...<branch>`. Finished → open a PR "
            "(`gh pr create -H <branch> -B main`). Unpushed → `git push origin <branch>` "
            "NOW, so the claim is visible where other sessions look. Superseded → delete it."
        ),
        (
            "Bug class: `CLAIMED-WORK-STRANDED-UNPUSHED-AND-INVISIBLE` (MYC-3290) — "
            "collides with the codified rule *A Claim Is Only Real Once It Is Visible "
            "to Other Sessions*, which nothing detected until now."
        ),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hub-refresh classifier (MYC-1893) — keep bare ~/dev/<repo> hubs fresh.
#
# Bare hubs ROT: parked on stale feature branches, dirtied by edits, written
# into by background jobs (STALE-BARE-CHECKOUT-READ, MYC-670 / MYC-1127). The
# read-time guard warn-stale-dev-checkout.py DETECTS the rot; this is the
# PREVENTION half. It has exactly ONE auto-action — fast-forward a hub that is
# clean, on its default branch, behind, and carries no local commits (a
# GUARANTEED ff, fully reversible via reflog). Everything else is SURFACED for a
# human, NEVER auto-switched / auto-cleaned / auto-recovered (the over-engineered
# version was cut). Fetch-first is the CALLER's job (a stale local ref misleads
# in BOTH directions — MYC-1893 correction); this classifier reads refs only.
# ---------------------------------------------------------------------------


def discover_hubs(dev_root: Optional[Path] = None) -> list[Path]:
    """The BARE hubs under ~/dev (linked worktrees + bare-origin mirrors excluded).

    A thin bare-hub filter over the shared discover_primary_repos (MYC-1894) so
    fleet discovery stays ONE source of truth: hub-refresh only ever ff's /
    surfaces bare hubs (`.git` a real DIRECTORY, which can rot), never worktrees
    (`.git` a gitdir-pointer FILE, fresh off origin by construction).
    """
    root = Path(dev_root) if dev_root is not None else resolve_dev_root()
    return [p for p in discover_primary_repos(root) if (p / ".git").is_dir()]


class HubAction:
    """String enum (plain constants for json/log friendliness)."""

    FF = "ff"                                  # clean + on default + behind>0 + ahead==0 → ff (only auto action)
    SURFACE_DIRTY = "surface:dirty"            # uncommitted changes → never touch
    SURFACE_OFF_DEFAULT = "surface:off-default"  # detached or on a feature branch → never switch
    SURFACE_DIVERGED = "surface:diverged"      # on default but carries unpushed local commits
    SKIP_CURRENT = "skip:current"              # already up to date with origin default
    SKIP_NO_ORIGIN = "skip:no-origin"          # no origin default branch (local-only repo)
    SKIP_SESSION_LOCK = "skip:session-lock"    # a live sibling session → don't fight it
    SKIP_WORKTREE = "skip:worktree"            # a linked worktree, not a primary bare hub


# A hub is only ever AUTO-advanced (ff); every other non-skip action is surfaced.
HUB_AUTO_ACTIONS = frozenset({HubAction.FF})
HUB_SURFACE_ACTIONS = frozenset({
    HubAction.SURFACE_DIRTY,
    HubAction.SURFACE_OFF_DEFAULT,
    HubAction.SURFACE_DIVERGED,
})


@dataclass
class HubState:
    repo: Path
    action: str
    default_branch: Optional[str] = None
    current_branch: Optional[str] = None  # None if HEAD is detached
    behind: int = 0
    ahead: int = 0
    dirty: bool = False
    head: str = ""


def _current_branch(repo: Path) -> Optional[str]:
    """The checked-out branch name, or None if HEAD is detached."""
    rc, out, _ = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out if rc == 0 and out else None


def _ahead_behind(repo: Path, upstream: str) -> tuple[int, int]:
    """(ahead, behind) of HEAD vs `upstream` (e.g. 'origin/main'); (0, 0) on error.

    `git rev-list --left-right --count upstream...HEAD` prints '<behind>\\t<ahead>'
    (left = commits in upstream not in HEAD; right = commits in HEAD not in upstream).
    """
    rc, out, _ = _git(repo, "rev-list", "--left-right", "--count", f"{upstream}...HEAD")
    if rc != 0 or not out:
        return 0, 0
    parts = out.split()
    if len(parts) != 2:
        return 0, 0
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0
    return ahead, behind


def classify_hub_action(
    repo: Path,
    *,
    default_branch: Optional[str] = None,
    now_ts: Optional[float] = None,
) -> HubState:
    """Classify a bare hub into exactly one HubAction. Fail-safe: anything the
    classifier cannot prove safe is SURFACEd or SKIPped, never auto-advanced.

    Decision order (first match wins):
      1. live .session-lock        → SKIP_SESSION_LOCK (don't fight a session)
      2. no origin default branch  → SKIP_NO_ORIGIN
      3. off the default branch    → SURFACE_OFF_DEFAULT (detached / feature branch)
      4. dirty working tree        → SURFACE_DIRTY
      5. unpushed local commits    → SURFACE_DIVERGED (ahead > 0)
      6. clean, on default, behind → FF   (the only auto action; a guaranteed ff)
         clean, on default, even   → SKIP_CURRENT
    """
    repo = Path(repo)
    st = HubState(repo=repo, action=HubAction.SKIP_CURRENT)
    rc, head, _ = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD")
    st.head = head if rc == 0 else ""

    if _has_live_session_lock(repo, now_ts):
        st.action = HubAction.SKIP_SESSION_LOCK
        return st

    default_branch = default_branch or detect_default_branch(repo)
    st.default_branch = default_branch
    if not default_branch:
        st.action = HubAction.SKIP_NO_ORIGIN
        return st
    upstream = f"origin/{default_branch}"

    st.current_branch = _current_branch(repo)
    st.dirty = _is_dirty(repo)
    st.ahead, st.behind = _ahead_behind(repo, upstream)

    # Off the default branch (detached or a feature branch) → SURFACE, never
    # auto-switch. ff-only is only meaningful while sitting on the default.
    if st.current_branch != default_branch:
        st.action = HubAction.SURFACE_OFF_DEFAULT
        return st

    # On the default branch from here down.
    if st.dirty:
        st.action = HubAction.SURFACE_DIRTY
        return st

    # Un-backed-up local commits (ahead of origin) → SURFACE; an ff-only into a
    # branch carrying its own commits is either a no-op or impossible.
    if st.ahead > 0:
        st.action = HubAction.SURFACE_DIVERGED
        return st

    st.action = HubAction.FF if st.behind > 0 else HubAction.SKIP_CURRENT
    return st


def execute_hub_refresh(
    state: HubState,
    apply: bool = False,
    now_ts: Optional[float] = None,
) -> dict:
    """Advance exactly the FF hubs by a fast-forward; everything else is a no-op.

    apply=False is a dry-run that mutates nothing. apply=True re-classifies the
    hub immediately before mutating (a read-then-act atomicity gate: a hub that
    turned dirty / switched branch / diverged between plan and apply drops out of
    the FF set), then runs `git merge --ff-only` — which itself fails closed if
    the move is somehow no longer a fast-forward. A fast-forward only advances the
    branch pointer, so it is fully reversible from the reflog.
    """
    res: dict = {
        "repo": str(state.repo),
        "action": state.action,
        "applied": False,
        "ff_to": None,
        "ok": None,
    }
    if state.action != HubAction.FF:
        return res
    if not apply:
        res["ff_to"] = f"origin/{state.default_branch}"
        return res

    fresh = classify_hub_action(state.repo, now_ts=now_ts)
    if fresh.action != HubAction.FF:
        res["action"] = fresh.action
        res["skipped"] = "state-changed-since-plan"
        return res

    # Reclaim an abandoned index.lock first (MYC-3175). A crashed git strands it
    # and then EVERY ff here fails forever, so the hub silently rots while the
    # report keeps calling it merely "behind" — the same permanent freeze the
    # install clone hit. Conservative: only an old, unheld lock is removed.
    reclaimed = reclaim_stale_git_locks(state.repo)
    if reclaimed:
        res["reclaimed_locks"] = reclaimed

    upstream = f"origin/{fresh.default_branch}"
    rc, _out, err = _git(state.repo, "merge", "--ff-only", upstream)
    res["ok"] = rc == 0
    res["applied"] = rc == 0
    if rc == 0:
        rc2, head, _ = _git(state.repo, "rev-parse", "HEAD")
        res["ff_to"] = head if rc2 == 0 else upstream
    else:
        res["error"] = err[:200]
    return res


def summarize_hub_states(states) -> dict:
    """Roll a fleet of HubState into counts + worst offenders, for the cheap
    SessionStart surfacer. 'offenders' = surfaced hubs + any hub still behind,
    sorted by commits-behind descending."""
    states = list(states)
    ff = sum(1 for s in states if s.action == HubAction.FF)
    surfaced = sum(1 for s in states if s.action in HUB_SURFACE_ACTIONS)
    skipped = sum(1 for s in states if s.action.startswith("skip:"))
    max_behind = max((s.behind for s in states), default=0)
    offenders = sorted(
        (
            (s.repo.name, s.action, s.behind)
            for s in states
            if s.action in HUB_SURFACE_ACTIONS or s.behind > 0
        ),
        key=lambda t: t[2],
        reverse=True,
    )
    return {
        "ff": ff,
        "surfaced": surfaced,
        "skipped": skipped,
        "max_behind": max_behind,
        "offenders": offenders,
    }


_HUB_ACTION_LABEL = {
    HubAction.SURFACE_DIRTY: "dirty working tree",
    HubAction.SURFACE_OFF_DEFAULT: "off the default branch",
    HubAction.SURFACE_DIVERGED: "unpushed local commits",
    HubAction.FF: "behind (auto-ff pending)",
}


def format_hub_surface_line(
    summary: dict, threshold: int = 50, limit: int = 6
) -> Optional[str]:
    """A one-line SessionStart nudge, or None when nothing is worth saying.

    Speaks up when ANY hub needs a human (surfaced: dirty / off-default /
    diverged) OR a hub is >= `threshold` commits behind origin (a sign the
    launchd auto-refresh has stalled). A clean main only a little behind is
    auto-fixable and stays silent."""
    surfaced = summary.get("surfaced", 0)
    max_behind = summary.get("max_behind", 0)
    if surfaced == 0 and max_behind < threshold:
        return None
    offenders = summary.get("offenders", [])
    parts = [
        "[dev-hub-refresh]",
        "",
        (
            f"{surfaced} bare ~/dev hub(s) need a human; worst is {max_behind} "
            f"commit(s) behind origin. Auto-ff handles clean mains — these need you:"
        ),
    ]
    for name, action, behind in offenders[:limit]:
        label = _HUB_ACTION_LABEL.get(action, action)
        parts.append(f"- `{name}` — {label} ({behind} behind)")
    if len(offenders) > limit:
        parts.append(f"- … and {len(offenders) - limit} more")
    parts += [
        "",
        (
            "Resolve each in its repo (commit or stash local work, then "
            "`git pull --ff-only`), or run `dev-hub-refresh.py` (dry-run) "
            "then `--apply` to handle the clean ones in bulk."
        ),
    ]
    return "\n".join(parts)
