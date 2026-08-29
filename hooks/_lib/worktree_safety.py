"""Shared worktree-safety helpers for the worktree-lifecycle hooks.

Why this exists
---------------
Git worktrees under `.claude/worktrees/<slug>/` are throwaway per-session
scratch checkouts. Nothing in stock Claude Code removes them when a session
ends, so on an active machine they accumulate — each one a FULL checkout of
the vault. Left alone they reach hundreds of worktrees / millions of files,
which then melts any cloud-sync daemon (iCloud / OneDrive / Dropbox) pointed
at the vault and burns disk. This module is the safe-cleanup core shared by:

  - remove-ended-worktree.py      (SessionEnd: clean up the just-ended one)
  - enforce-worktree-cap.py       (SessionStart: bound the total, reclaim-then-allow)
  - worktree-footprint-signal.py  (SessionStart: observe before it bloats)

The recovery-content boundary
-----------------------------
The lifecycle behavior in this module predates the cloud-safe reader. This
module does not claim to solve exclusion against a concurrent writer. It does
guarantee that recovery classification never performs an unbounded content read:

  * COMMITTED work       -> preserved by the branch ref (`git worktree remove`
                            keeps the `claude/<slug>` branch; only the working
                            directory is deleted).
  * content already in git -> bytes are read once through `safe_read`, hashed
                            locally, then checked with `git cat-file`; Git never
                            reopens a candidate path.
  * genuinely-unsaved    -> content that hashes to a blob NOT in the object DB.
                            We SNAPSHOT these to the main repo before deletion.

An offline placeholder, special file, timeout, oversize file, or Git hiccup is
uncertainty and therefore makes the existing caller refuse cleanup. Strong
atomic deletion still requires an upstream writer lease / pre-create contract.

Portable: no hardcoded paths; resolves the repo from cwd / CLAUDE_PROJECT_DIR.
Pure stdlib.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
import shutil
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .safe_read import safe_read_bytes, safe_read_text

# Canonical reaper-artifact locations, relative to the VAULT ROOT (never to the
# repo being reaped) — MUST match snapshot-pending-work-on-stop.py and
# surface-orphan-worktree-snapshots.py so orphan-surfacing + weekly prune find
# snapshots written here. Both of those resolve an ABSOLUTE vault path, so the
# helpers below do too. When no vault can be identified, artifacts go to a
# machine-local dir under ~/.claude — never into `main_repo`.
SNAPSHOT_REL = "⚙️ Meta/Worktree Snapshots"
SNAPSHOT_REL_FALLBACK = "worktree-snapshots"
CLEANUP_LOG_REL = "⚙️ Meta/logs/worktree-cleanup.log"
WORKTREES_SEG = ".claude/worktrees"

# Heavy machine-exhaust dirs: never counted as "work" for idle detection.
EXHAUST = {
    ".git", ".smart-env", ".claude", "node_modules", ".venv", "__pycache__",
    ".codegraph", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".obsidian",
    ".trash", ".DS_Store",
}

GIT_TIMEOUT = 120
RECOVERY_READ_TIMEOUT_S = 5.0
RECOVERY_MAX_FILE_BYTES = 64 * 1024 * 1024
RECOVERY_MAX_TOTAL_BYTES = 128 * 1024 * 1024
RECOVERY_MAX_CANDIDATES = 5_000
RECOVERY_SCAN_DEADLINE_S = 30.0


def find_main_repo(cwd: Path | None = None) -> Path | None:
    """Resolve the main checkout that owns `.claude/worktrees/`.

    1. CLAUDE_PROJECT_DIR env var (set by Claude Code) if it's a real dir.
       If that value itself sits inside `.../.claude/worktrees/<slug>/`
       (Claude Code commonly sets it to the session cwd, which IS the
       worktree path), collapse it back to the part before so logs and
       snapshots written via this helper never strand on a throwaway
       claude/<slug> branch.
    2. If cwd is inside `.../.claude/worktrees/<slug>/`, the part before
       `.claude/worktrees`.
    3. cwd itself if it contains `.claude/worktrees/`.
    4. Walk up cwd for a parent containing `.claude/worktrees/`.
    Returns None if nothing resolves.
    """
    cwd = (cwd or Path.cwd()).resolve()
    marker = "/" + WORKTREES_SEG + "/"

    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        cand = Path(env_root)
        if cand.is_dir():
            cand = cand.resolve()
            s = str(cand)
            if marker in s:
                return Path(s.split(marker, 1)[0]).resolve()
            return cand

    s = str(cwd)
    if marker in s:
        return Path(s.split(marker, 1)[0]).resolve()

    if (cwd / WORKTREES_SEG).is_dir():
        return cwd
    for parent in cwd.parents:
        if (parent / WORKTREES_SEG).is_dir():
            return parent
    return None


def current_worktree(cwd: Path | None = None) -> tuple[Path, str] | None:
    """If cwd is inside `.../.claude/worktrees/<slug>/`, return (path, slug)."""
    cwd = (cwd or Path.cwd()).resolve()
    marker = "/" + WORKTREES_SEG + "/"
    s = str(cwd)
    if marker not in s:
        return None
    head, tail = s.split(marker, 1)
    slug = tail.split("/", 1)[0]
    if not slug:
        return None
    return Path(head + marker + slug), slug


def vault_root_or_none(main_repo: Path) -> Path | None:
    """The vault this machine writes session artifacts to, or None if we cannot
    identify one CONFIDENTLY.

    NEVER returns `main_repo` or anything inside it. Reaper artifacts (the cleanup
    log, snapshots of unsaved work) describe activity ACROSS repos and must not be
    written into whichever repo is being reaped: a broad `git add` then stages vault
    artifacts into a product repo. Measured 2026-07-23 on one machine: 8 repos
    contaminated this way, two with files already staged for commit.

    `resolve_vault_root()` alone is NOT sufficient here — its documented last resort
    is `cwd`, which for these hooks is routinely a product repo. Its answer is
    therefore accepted only when it independently looks like a vault AND is not the
    repo being reaped.
    """
    try:
        repo = main_repo.expanduser().resolve()
    except OSError:
        return None

    candidates: list[Path] = []
    # vault-root-ok: read raw, then fed to resolve_vault_root() below as its env
    # argument (the sanctioned cascade call). The raw value is ALSO tried first
    # because this helper must not inherit that resolver's two fallbacks: it
    # returns `cwd` when nothing else matches, and it prefers any ancestor repo
    # declaring its own close cascade. For a reaper the cwd IS routinely the
    # product repo being reaped, and reaping a vault-shaped repo (this one,
    # mycelium-vault) would resolve to that repo rather than the user's vault.
    # Every candidate is still validated below (must look like a vault, must not
    # be the reaped repo), so a wrong env value is rejected, not trusted.
    env = os.environ.get("VAULT_ROOT")
    if env:
        candidates.append(Path(env))
    try:  # optional: absent in a partial install, and this must never be fatal
        from .vault_root import resolve_vault_root

        candidates.append(resolve_vault_root(Path.cwd(), env))
    except Exception:
        pass

    for cand in candidates:
        try:
            c = cand.expanduser().resolve()
        except OSError:
            continue
        if c == repo or repo in c.parents:
            continue  # the repo being reaped, or inside it — the exact bug
        if (c / "⚙️ Meta").is_dir():
            return c
    return None


def artifact_base(main_repo: Path) -> tuple[Path, bool]:
    """(base directory for reaper artifacts, whether it is vault-shaped).

    `WORKTREE_ARTIFACT_ROOT` overrides everything. That env var exists for TESTS:
    these helpers now resolve to the caller's REAL vault, so a suite that builds a
    throwaway repo and calls snapshot_dir_for() would write its fixtures into the
    user's actual vault. Before this module took an absolute path, such a suite was
    contained by accident — the old repo-relative join kept fixtures inside the temp
    repo. Making the path correct for production therefore REMOVED that containment,
    and a real run of the recovery suite deposited `regular/UNSAVED.md` and
    `mode-window/secret.bin` into a live vault before this seam was added.
    """
    override = os.environ.get("WORKTREE_ARTIFACT_ROOT")
    if override:
        return Path(override).expanduser(), True
    vault = vault_root_or_none(main_repo)
    if vault is not None:
        return vault, True
    return Path.home() / ".claude", False


def snapshot_dir_for(main_repo: Path) -> Path:
    """Canonical snapshot root — the vault when identifiable, else machine-local.

    Always ABSOLUTE and always OUTSIDE `main_repo`. A snapshot exists to survive the
    destruction of the worktree it came from, so storing it inside the repo being
    reaped is self-defeating.

    This previously returned `main_repo / SNAPSHOT_REL` whenever `main_repo/"⚙️ Meta"`
    merely EXISTED — treating a directory name as proof the repo was the vault. That
    inference was false, and it CASCADED: the sibling cleanup-log writer created
    `⚙️ Meta/logs/` inside every product repo it reaped, which then made this sniff
    answer "vault!" for those repos, so snapshots of UNSAVED WORK began landing
    in-repo. The old dot-dir fallback was no safer — `.worktree-snapshots` was
    gitignored in none of the repos checked; it was simply losing to `⚙️ Meta`.
    """
    base, is_vault = artifact_base(main_repo)
    return base / SNAPSHOT_REL if is_vault else base / SNAPSHOT_REL_FALLBACK


def cleanup_log_path(main_repo: Path) -> Path:
    """Absolute path of the shared worktree-cleanup log. Never inside `main_repo`."""
    base, is_vault = artifact_base(main_repo)
    return base / CLEANUP_LOG_REL if is_vault else base / "logs" / "worktree-cleanup.log"


def append_cleanup_log(main_repo: Path, msg: str) -> None:
    """Append one line to the shared cleanup log. Best-effort, never fatal.

    ONE log now collects every repo's reaping, so the repo name travels IN the line
    rather than in the file's location — which is what made a repo-relative path look
    reasonable in the first place.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        path = cleanup_log_path(main_repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] [{main_repo.name}] {msg}\n")
    except OSError:
        pass


def git(repo: Path, args: list[str], timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, timeout=timeout,
    )


def _parse_worktree_porcelain(payload: bytes) -> list[Path]:
    """Raw paths from ``git worktree list --porcelain -z``."""
    return [
        Path(os.fsdecode(field[len(b"worktree "):]))
        for field in payload.split(b"\x00")
        if field.startswith(b"worktree ")
    ]


def list_worktrees(repo: Path) -> list[Path] | None:
    """Registered worktree paths, or None when Git cannot prove the set."""
    try:
        out = git(repo, ["worktree", "list", "--porcelain", "-z"])
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    paths = _parse_worktree_porcelain(out.stdout)
    repo_r = repo.resolve()
    return [p for p in paths if p.resolve() != repo_r]


def is_scratch_worktree(wt: Path) -> bool:
    """True if `wt` is a throwaway scratch worktree under `.claude/worktrees/`.

    The cap + reclaim only AUTO-REMOVE scratch worktrees. A deliberate
    `~/dev/<repo>-<slug>` sibling worktree (created by a per-session worktree
    pattern, often on its own feature branch) is NEVER auto-removed even when
    idle and on a `claude/*` branch — its lifecycle belongs to that workflow,
    not to this cap. Location, not branch name, is the safe discriminator.
    """
    marker = "/" + WORKTREES_SEG + "/"
    try:
        return marker in str(wt.resolve())
    except OSError:
        return marker in str(wt)


def is_idle(path: Path, idle_min: int = 60) -> bool:
    """True if no work file under `path` was modified in the last idle_min minutes.

    Prunes heavy hidden dirs for speed and early-exits on the first recent file,
    so active worktrees are detected fast. Uses mtime (not atime), so merely
    reading files does not mark a worktree active.
    """
    cutoff = time.time() - idle_min * 60
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in EXHAUST]
            for f in files:
                try:
                    if os.path.getmtime(os.path.join(root, f)) > cutoff:
                        return False
                except OSError:
                    continue
    except OSError:
        return True
    return True


def _parse_status_porcelain(worktree: Path, payload: bytes) -> list[Path]:
    """Paths from ``git status --porcelain -z`` with byte-preserving decode."""
    entries = payload.split(b"\x00")
    paths: list[Path] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        if len(entry) >= 4 and entry[3:]:
            paths.append(worktree / os.fsdecode(entry[3:]))
            # In -z mode a rename/copy has a second raw source-path field with
            # no XY prefix. It carries no remaining inode and must not be parsed
            # as another status record.
            if b"R" in entry[:2] or b"C" in entry[:2]:
                index += 1
        index += 1
    return paths


def dirty_files(worktree: Path) -> list[Path] | None:
    """Dirty paths, or None when Git cannot prove the set."""
    try:
        out = subprocess.run(
            [
                "git", "-C", str(worktree), "status", "--porcelain", "-z",
                "--untracked-files=all",
            ],
            capture_output=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    return _parse_status_porcelain(worktree, out.stdout)


@dataclass
class _RecoveryScan:
    unique: list[tuple[Path, bytes, int]]
    unsafe: list[tuple[Path, str]]
    recoverable: int


def _blob_hash(data: bytes, object_format: str) -> str:
    digest = hashlib.new(object_format)
    digest.update(b"blob " + str(len(data)).encode("ascii") + b"\x00" + data)
    return digest.hexdigest()


def _recovery_scan(repo: Path, files: list[Path]) -> _RecoveryScan:
    """Bounded-read candidates once, then classify their local blob hashes."""
    reads: list[tuple[Path, bytes, int]] = []
    unsafe: list[tuple[Path, str]] = []
    missing = 0
    total = 0
    started = time.monotonic()
    if len(files) > RECOVERY_MAX_CANDIDATES:
        return _RecoveryScan([], [(files[0], "candidate-cap")], 0)
    for file in files:
        if time.monotonic() - started > RECOVERY_SCAN_DEADLINE_S:
            unsafe.append((file, "scan-deadline"))
            break
        remaining_total = RECOVERY_MAX_TOTAL_BYTES - total
        if remaining_total <= 0:
            unsafe.append((file, "recovery-total-cap"))
            break
        per_file_limit = min(RECOVERY_MAX_FILE_BYTES, remaining_total)
        result = safe_read_bytes(
            file,
            timeout=RECOVERY_READ_TIMEOUT_S,
            max_bytes=per_file_limit,
        )
        if result.status == "missing":
            missing += 1
            continue
        if not result.ok:
            unsafe.append((file, result.status))
            if result.status == "too-large" and per_file_limit < RECOVERY_MAX_FILE_BYTES:
                break
            continue
        data = result.data or b""
        if len(data) > remaining_total:
            unsafe.append((file, "recovery-total-cap"))
            break
        total += len(data)
        reads.append((file, data, result.mode if result.mode is not None else 0o600))
    if not reads:
        return _RecoveryScan([], unsafe, missing)
    try:
        fmt = git(repo, ["rev-parse", "--show-object-format"], timeout=15)
        object_format = fmt.stdout.decode("ascii", "replace").strip()
        if fmt.returncode != 0 or object_format not in {"sha1", "sha256"}:
            raise ValueError("unknown git object format")
        hashes = [_blob_hash(data, object_format) for _path, data, _mode in reads]
        cp = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch-check"],
            input=("\n".join(hashes) + "\n").encode("ascii"),
            capture_output=True,
            timeout=60,
        )
        if cp.returncode != 0:
            raise OSError("git cat-file failed")
        present: dict[str, bool] = {}
        for line in cp.stdout.decode("ascii", "replace").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                present[parts[0]] = parts[1] != "missing"
        unique = [
            (path, data, mode)
            for (path, data, mode), digest in zip(reads, hashes)
            if not present.get(digest, False)
        ]
        return _RecoveryScan(
            unique,
            unsafe,
            missing + len(reads) - len(unique),
        )
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return _RecoveryScan(reads, unsafe, missing)


def unrecoverable_content(repo: Path, files: list[Path]) -> list[Path]:
    """Paths unique to the worktree plus every path unsafe to bounded-read."""
    scan = _recovery_scan(repo, files)
    return [path for path, _data, _mode in scan.unique] + [
        path for path, _why in scan.unsafe
    ]


def _write_recovery_copy(dst: Path, data: bytes, source_mode: int) -> bool:
    """Atomically write bounded bytes; private content is never briefly public."""
    temp: Path | None = None
    fd: int | None = None
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        temp = dst.with_name(
            f".{dst.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        )
        fd = os.open(
            str(temp),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "wb") as stream:
            fd = None  # the stream owns it now
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), source_mode & 0o7777)
            else:  # Windows: the temp is already restrictive; chmod is best-effort.
                os.chmod(temp, source_mode & 0o7777)
        os.replace(temp, dst)
        return True
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp is not None:
            try:
                temp.unlink()
            except OSError:
                pass
        return False


def snapshot_unrecoverable(main_repo: Path, worktree: Path, slug: str) -> tuple[int, int, bool]:
    """Snapshot genuinely-unsaved files before removal.

    Returns (snapshotted, recoverable_discarded, all_safe). all_safe is False
    only if a genuinely-unsaved file could NOT be copied — caller must then
    refuse to delete the worktree.
    """
    files = dirty_files(worktree)
    if files is None:
        return 0, 0, False
    scan = _recovery_scan(main_repo, files)
    recoverable = scan.recoverable
    snap_root = snapshot_dir_for(main_repo) / slug
    snapped = 0
    all_safe = not scan.unsafe
    for src, data, mode in scan.unique:
        try:
            rel = src.relative_to(worktree)
        except ValueError:
            all_safe = False
            continue
        if _write_recovery_copy(snap_root / rel, data, mode):
            snapped += 1
        else:
            all_safe = False
    return snapped, recoverable, all_safe


def remove_worktree(main_repo: Path, worktree: Path, force: bool = True) -> bool:
    """`git worktree remove` (keeps the branch ref). True on success.

    `timeout=None` is LOAD-BEARING. `git worktree remove` unlinks the working
    tree in raw readdir order, so killing it partway leaves a contiguous PREFIX
    of the checkout deleted while `.git`, HEAD and the admin record survive --
    the worktree still lists as registered and gets re-damaged on the next run.
    Tracked files come back with `git checkout -- .`; an untracked `.env.local`
    does not come back at all. This helper defaulted to GIT_TIMEOUT (120s) AND
    force=True, which is the same hazard with a longer fuse and no confirmation
    prompt. Measured on a reaper carrying this bug 2026-08-02: 26+ damaged
    worktrees.

    Reporting the side effect rather than `returncode == 0` is the other half:
    git exiting 0 is its report, not the outcome. A caller must never be able to
    record a half-removal as a removal. `os.path.lexists` (not `Path.exists`)
    because the latter follows symlinks and would call a leftover dangling link
    at the worktree path a clean removal.

    Negative control: hooks/test_worktree_remove_verifies_side_effect.py
    """
    args = ["worktree", "remove"] + (["--force"] if force else []) + [str(worktree)]
    try:
        if git(main_repo, args, timeout=None).returncode != 0:
            return False
    except (subprocess.TimeoutExpired, OSError):
        return False
    return not os.path.lexists(worktree)


def list_orphan_dirs(main_repo: Path) -> list[Path]:
    """Dirs under `.claude/worktrees/` that git no longer registers.

    These accumulate when a worktree's git registration is pruned (or never
    completed) but the directory is left on disk — each a full stale checkout.
    The cap/remove hooks only see REGISTERED worktrees (`git worktree list`),
    so orphan dirs are invisible to them and need this dedicated sweep. This
    is the gap that let the vault reach 19 orphan dirs even with the hooks live.
    """
    wt_dir = main_repo / WORKTREES_SEG
    if not wt_dir.is_dir():
        return []
    worktrees = list_worktrees(main_repo)
    if worktrees is None:
        return []  # unknown registration state can never authorize orphan reclaim
    registered = {p.resolve() for p in worktrees}
    orphans: list[Path] = []
    try:
        for c in sorted(wt_dir.iterdir()):
            if c.is_dir() and c.resolve() not in registered:
                orphans.append(c)
    except OSError:
        return []
    return orphans


def _gitfile_target(orphan: Path) -> Path | None:
    """If `orphan/.git` is a `gitdir: <path>` pointer file, return <path>, else None.

    A worktree's `.git` is a one-line pointer file (not a dir). A relocation
    copies it verbatim, so it still points at the OLD location's gitdir.
    """
    gitfile = orphan / ".git"
    result = safe_read_text(
        gitfile,
        timeout=RECOVERY_READ_TIMEOUT_S,
        max_bytes=64 * 1024,
        errors="replace",
        skip_binary=True,
    )
    if not result.ok:
        return None
    txt = (result.text or "").strip()
    if not txt.startswith("gitdir:"):
        return None
    target = txt[len("gitdir:"):].strip()
    if not target:
        return None
    p = Path(target)
    if not p.is_absolute():
        p = orphan / p
    return p


def _is_relocation_orphan(orphan: Path, main_repo: Path) -> bool:
    """True iff `orphan/.git` points at a gitdir that is DANGLING (gone) or
    EXTERNAL (outside this repo's `.git`) — the vault-relocation / copied-checkout
    class that `git status` can't evaluate but the MAIN object DB still can.

    Conservative: no clear pointer → False (keep, unknown provenance).
    """
    target = _gitfile_target(orphan)
    if target is None:
        return False
    try:
        main_git = (main_repo / ".git").resolve()
    except OSError:
        return False
    if not target.exists():
        return True  # dangling: original gitdir is gone (the relocation case)
    try:
        target.resolve().relative_to(main_git)
        return False  # inside this repo's own .git tree — a real registration
    except (ValueError, OSError):
        return True  # external: points at a different/foreign .git tree


def _bounded_recovery_files(root: Path) -> list[Path] | None:
    """Enumerate a disconnected tree with count/deadline bounds and no reads."""
    files: list[Path] = []
    pending = [root]
    started = time.monotonic()
    while pending:
        if time.monotonic() - started > RECOVERY_SCAN_DEADLINE_S:
            return None
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if time.monotonic() - started > RECOVERY_SCAN_DEADLINE_S:
                        return None
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in EXHAUST:
                            pending.append(Path(entry.path))
                        continue
                    if entry.name in EXHAUST:
                        continue
                    files.append(Path(entry.path))
                    if len(files) > RECOVERY_MAX_CANDIDATES:
                        return None
        except OSError:
            return None
    return files


def _reclaim_disconnected_orphan(main_repo: Path, orphan: Path, slug: str) -> tuple[str, int]:
    """Reclaim a relocation-orphan whose own git metadata is unusable.

    A worktree is a checkout of commits in the MAIN repo's shared object store,
    so bounded local blob hashing can still classify content when the orphan's
    `.git` pointer is dead. Snapshot the currently observed unique bytes, then
    preserve the existing lifecycle behavior. This content boundary fails closed
    on read/Git uncertainty; it does not provide concurrent-writer exclusion.
    """
    files = _bounded_recovery_files(orphan)
    if files is None:
        return ("kept-unsafe", 0)
    scan = _recovery_scan(main_repo, files)
    snap_root = snapshot_dir_for(main_repo) / slug
    snapped = 0
    all_safe = not scan.unsafe
    for src, data, mode in scan.unique:
        try:
            rel = src.relative_to(orphan)
        except ValueError:
            all_safe = False
            continue
        if _write_recovery_copy(snap_root / rel, data, mode):
            snapped += 1
        else:
            all_safe = False
    if not all_safe:
        return ("kept-unsafe", snapped)
    try:
        shutil.rmtree(orphan)
    except OSError:
        return ("kept-unsafe", snapped)
    return ("relocation-orphan+removed" if snapped else "relocation-orphan-removed", snapped)


def reclaim_orphan_dir(main_repo: Path, orphan: Path, idle_min: int = 60) -> tuple[str, int]:
    """Safely reclaim one orphan worktree dir. Returns (action, snapshotted).

    action ∈ {removed, snapshot+removed, kept-active, kept-unsafe, kept-dangling,
              relocation-orphan-removed, relocation-orphan+removed}.

    Fast + fail-safe — never `rm -rf` a dir we can't reason about:
      * idle gate: a dir touched < idle_min ago is left (a live/paused session).
      * `git -C <orphan> status` decides recoverability cheaply (uses the index):
          - clean       → rm (every file is committed/recoverable from a branch)
          - dirty       → snapshot ONLY the dirty set (small; bounded object-DB
                          content classification) then rm iff every unsaved file was
                          safely copied.
          - git errors  → the dir is disconnected from git. If its `.git` pointer
                          is dangling/external (the vault-RELOCATION copied-checkout
                          class that let `.claude/worktrees` reach 100k+ files), the
                          MAIN repo's object DB can still classify the bounded-read
                          bytes: snapshot the currently unique files, then remove.
                          Otherwise (unknown provenance) KEEP + report `kept-dangling`.

    This is safer than the old blind `rm -rf`, but it is not an atomic writer
    lease. Strong exclusion remains a runtime/upstream lifecycle dependency.
    """
    slug = orphan.name
    if not is_idle(orphan, idle_min):
        return ("kept-active", 0)
    try:
        st = git(
            orphan,
            ["status", "--porcelain", "-z", "--untracked-files=all"],
            timeout=60,
        )
        status_rc = st.returncode
    except (subprocess.TimeoutExpired, OSError):
        status_rc = -1
    if status_rc != 0:
        # git can't evaluate this dir. A relocation-orphan (dangling/external
        # .git pointer — copied during a vault move; original gitdir gone) is
        # still reclaimable against the MAIN object DB; anything else stays kept.
        if _is_relocation_orphan(orphan, main_repo):
            return _reclaim_disconnected_orphan(main_repo, orphan, slug)
        return ("kept-dangling", 0)
    dirty = _parse_status_porcelain(orphan, st.stdout)
    snapped = 0
    if dirty:
        scan = _recovery_scan(main_repo, dirty)
        snap_root = snapshot_dir_for(main_repo) / slug
        all_safe = not scan.unsafe
        for src, data, mode in scan.unique:
            try:
                rel = src.relative_to(orphan)
            except ValueError:
                all_safe = False
                continue
            if _write_recovery_copy(snap_root / rel, data, mode):
                snapped += 1
            else:
                all_safe = False
        if not all_safe:
            return ("kept-unsafe", snapped)
    try:
        shutil.rmtree(orphan)
    except OSError:
        return ("kept-unsafe", snapped)
    return ("snapshot+removed" if snapped else "removed", snapped)


DRIVEFS_ROOTS_DB = (
    Path.home() / "Library/Application Support/Google/DriveFS/root_preference_sqlite.db"
)


def drive_mirror_root_paths(db_path: str | Path | None = None) -> list[str]:
    """Native-path Google Drive "Mirror" roots (sync_type=1) from the DriveFS DB.

    Mirror roots live at an arbitrary local path and never appear under
    ~/Library/CloudStorage, so a path-marker check cannot see them. This is the
    SINGLE source of that signal, shared by detect_cloud_sync (the install guard
    + the SessionStart footprint signal) and the check-sync-folder-machinery
    audit (MYC-1130) so they can never drift. Read FAIL-OPEN: a missing / locked
    / malformed DB returns [] and never raises. `immutable=1` so a LIVE Drive
    holding a write lock still reads (a plain `mode=ro` can return empty).
    """
    db = Path(db_path) if db_path is not None else DRIVEFS_ROOTS_DB
    if not db.exists():
        return []
    roots: list[str] = []
    try:
        con = sqlite3.connect(db.as_uri() + "?immutable=1", uri=True, timeout=2.0)
        try:
            cur = con.execute(
                "SELECT last_seen_absolute_path, root_path FROM roots "
                "WHERE sync_type = 1"
            )
            for last_seen, root_path in cur.fetchall():
                cand = (last_seen or "").strip() or (root_path or "").strip()
                if cand:
                    roots.append(cand)
        finally:
            con.close()
    except Exception:
        return []  # fail-open: a DB hiccup must never break the advisory guard
    return roots


def detect_cloud_sync(path: Path, *, _drivefs_db: str | Path | None = None) -> str | None:
    """Name of a consumer cloud-sync service whose scope contains `path`, else None.

    Cross-platform best-effort: a brain/vault under iCloud / OneDrive / Dropbox /
    Google Drive / Box is the exact combination that turns worktree churn into a
    machine-melting sync storm. The index belongs server-side; the local vault
    belongs on a real local disk, never in a consumer sync folder.

    `_drivefs_db` overrides the DriveFS roots-DB path for hermetic tests; in
    production it stays None and resolves to the real per-user location.
    """
    p = str(path.resolve())
    home = str(Path.home())
    markers = {
        "OneDrive": ["/OneDrive", "\\OneDrive"],
        "Dropbox": ["/Dropbox", "\\Dropbox"],
        "Google Drive": ["/Google Drive", "/GoogleDrive",
                          "CloudStorage/GoogleDrive", "\\Google Drive"],
        "Box": ["/Box/", "\\Box\\", "/Box Sync"],
        "iCloud Drive": ["/Mobile Documents/com~apple~CloudDocs",
                         "/Library/Mobile Documents"],
    }
    for name, subs in markers.items():
        if any(sub in p for sub in subs):
            return name
    # macOS iCloud "Desktop & Documents" sync: ~/Desktop or ~/Documents are
    # synced when ~/Library/Mobile Documents/com~apple~CloudDocs/<folder> exists.
    icloud = Path(home) / "Library/Mobile Documents/com~apple~CloudDocs"
    for folder in ("Desktop", "Documents"):
        try:
            base = (Path(home) / folder).resolve()
        except OSError:
            continue
        if (p == str(base) or p.startswith(str(base) + "/")) and (icloud / folder).exists():
            return f"iCloud Drive ({folder} sync)"
    # Native-path Google Drive "Mirror" roots (sync_type=1) are invisible to the
    # markers above; the DriveFS roots DB is the only signal (MYC-1130).
    # WORKTREE_SAFETY_DRIVEFS_DB overrides the DB path for hermetic tests of the
    # install-guard chain (check-cloud-sync.py has no _drivefs_db arg).
    for root in drive_mirror_root_paths(_drivefs_db or os.environ.get("WORKTREE_SAFETY_DRIVEFS_DB")):
        try:
            rp = str(Path(root).resolve())
        except OSError:
            continue
        if p == rp or p.startswith(rp + "/"):
            return "Google Drive (Mirror)"
    return None


def _obsidian_config_path(explicit: str | Path | None = None) -> Path | None:
    """Locate Obsidian's obsidian.json: explicit arg > $OBSIDIAN_CONFIG > per-OS
    default. Returns the explicit/env path verbatim (caller guards is_file); for
    the defaults, the first that exists, else None."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("OBSIDIAN_CONFIG")
    if env:
        return Path(env)
    home = Path.home()
    candidates = [
        home / "Library/Application Support/obsidian/obsidian.json",  # macOS
        home / ".config/obsidian/obsidian.json",                      # Linux
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "obsidian" / "obsidian.json")  # Windows
    candidates.append(home / "AppData/Roaming/obsidian/obsidian.json")
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def obsidian_vault_paths(config_path: str | Path | None = None) -> list[Path]:
    """Absolute paths of every vault Obsidian has registered, from obsidian.json.

    Lets the cloud-sync offer find a pre-existing Obsidian vault that was never
    pasted into guided setup — the "user already had an iCloud vault (Obsidian's
    common default)" case the SessionStart footprint signal otherwise misses (it
    only sees the vault you are cwd'd inside). Bounded + fail-open by construction:
    one small JSON read, never a filesystem walk, and [] on any missing /
    malformed / unreadable config — a registry hiccup must never break a hook.

    Schema: {"vaults": {"<id>": {"path": "<abs>", "open": bool, ...}, ...}}.
    """
    cfg = _obsidian_config_path(config_path)
    if cfg is None:
        return []
    result = safe_read_text(cfg, timeout=2.0, max_bytes=1_000_000)
    if not result.ok:
        return []
    try:
        data = json.loads(result.text or "")
    except ValueError:
        return []
    vaults = data.get("vaults") if isinstance(data, dict) else None
    if not isinstance(vaults, dict):
        return []
    out: list[Path] = []
    seen: set[str] = set()
    for entry in vaults.values():
        if not isinstance(entry, dict):
            continue
        p = entry.get("path")
        if isinstance(p, str) and p and p not in seen:
            seen.add(p)
            out.append(Path(p))
    return out


# Optional session-liveness file written by session-lock.py (a sibling hook).
# Reading it lets the cap reaper distinguish "this scratch worktree belongs to a
# session that is still running" from "its session is gone" — so a crashed
# session's worktree can be reclaimed promptly (regardless of the count cap)
# while a live-but-idle session's worktree is never pulled out from under it.
SESSION_LOCK_REL = ".claude/.session-lock.json"


def _pid_alive(pid: int) -> bool:
    """True if `pid` names a running process. Errs toward 'alive' (over-preserve)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another uid
    except OSError:
        return True  # unsure → treat as alive so we never reap a maybe-live session


def live_session_cwds(main_repo: Path, grace_min: int = 35) -> set[str] | None:
    """Resolved cwd paths of currently-LIVE Claude sessions, from the session lock.

    A session counts as live if its PID is running OR it was active within the
    last `grace_min` minutes (the lock prunes idle entries at ~30 min, so 35 min
    gives a margin). Both checks err toward "live" — the only safe direction,
    since the consumer uses this to decide what NOT to reclaim.

    Returns None when liveness is UNKNOWN (lock absent / unreadable / wrong shape).
    None is distinct from an empty set: callers MUST treat None as "do not reap on
    liveness" (never interpret a missing lock as "no sessions are live → reap all").
    """
    lock = main_repo / SESSION_LOCK_REL
    result = safe_read_text(lock, timeout=2.0, max_bytes=1_000_000)
    if not result.ok:
        return None
    try:
        data = json.loads(result.text or "")
    except ValueError:
        return None
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, dict):
        return None
    cutoff = time.time() - grace_min * 60
    live: set[str] = set()
    for s in sessions.values():
        if not isinstance(s, dict):
            continue
        cwd = s.get("cwd")
        if not cwd:
            continue
        la = s.get("last_activity_at")
        recent = isinstance(la, (int, float)) and la >= cutoff
        if recent or _pid_alive(s.get("pid")):
            try:
                live.add(str(Path(cwd).resolve()))
            except OSError:
                live.add(str(cwd))
    return live
