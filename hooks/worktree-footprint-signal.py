#!/usr/bin/env python3
"""Observe local footprint before it bloats — SessionStart signal.

The 102-worktree pileup was discovered only when the machine fell over. That
is the real failure: it was invisible until catastrophic. This hook makes the
footprint observable at every SessionStart so "12 worktrees and climbing" is
seen early, not "102, machine dead" later. Cheap: a couple of git/stat calls,
no per-worktree filesystem walk.

Surfaces (only when something warrants attention — silent when healthy):
  * worktree count over the soft threshold (WORKTREE_WARN, default 8)
  * on-disk worktree dirs git no longer tracks (orphans)
  * AGGREGATE file count across the known bloat-prone dirs (MYC-577). The
    2026-06-06 ~234K-file melt was MULTI-SOURCE (~108K worktrees + ~32K
    .smart-env + ~17K snapshots + ~14K Sessions), but worktree COUNT alone
    sees only one source, so non-worktree regrowth — .smart-env re-indexing
    above all — stayed invisible until the machine fell over. The melt
    threshold (~200K+ files) is the real constraint, so the aggregate is what
    must be observable. Frequency-capped to ~daily (cached count + mtime) so
    SessionStart never pays a full-tree walk on a fragile machine.
  * low free disk on the vault's volume
  * THE DANGEROUS COMBO: an AI-brain vault inside a consumer cloud-sync folder
    (iCloud / OneDrive / Dropbox / Google Drive / Box) — worktree/.git churn
    there is what melts the sync daemon. Detected for ANY such vault — the one
    cwd is in, $CLAUDE_PROJECT_DIR, OR any vault in Obsidian's registry, so a
    pre-existing iCloud vault is caught even outside guided onboarding — and the
    auto-capable relocate is OFFERED as ONE recommended safe action (keep the
    notes where they are, move only the churning machinery out) — never a
    two-option menu a non-technical user can't answer — self-silencing once
    relocated. (MYC-2360 + offer-simplification follow-up)

Bypass: WORKTREE_FOOTPRINT_BYPASS=1.
Aggregate-bloat tunables: WORKTREE_FOOTPRINT_FILE_WARN (default 80000),
WORKTREE_FOOTPRINT_BLOAT_DIRS (extra vault-relative dirs, comma-separated),
WORKTREE_FOOTPRINT_BLOAT_TTL_H (recount interval, default 24),
WORKTREE_FOOTPRINT_CACHE (cache file path; tests point this at a temp dir).

WIRING (SessionStart):
  "SessionStart": [
    {"hooks": [{
      "type": "command",
      "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/worktree-footprint-signal.py 2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'"
    }]}
  ]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))

from _lib.safe_read import safe_read_text  # noqa: E402
from _lib.worktree_safety import (  # noqa: E402
    WORKTREES_SEG,
    detect_cloud_sync,
    find_main_repo,
    is_scratch_worktree,
    list_worktrees,
    obsidian_vault_paths,
)

DEFAULT_WARN = 8

# Absolute floor: below this, any machine is in trouble.
DEFAULT_FREE_GB = 5.0
# Proportional floor, because an absolute number does not scale with the disk.
# A flat 5 GB on a 1 TB volume is not an early warning, it is a post-mortem: by
# then builds already fail and sync daemons already stall. Measured 2026-08-05 on
# a 926 GB machine — build artifacts filled it to 2.3 GB free and this hook stayed
# silent the whole way down, because 5 GB is the only line it had. 5% (≈46 GB
# there, ≈13 GB on a 256 GB laptop) fires while the fix is still cheap.
# The effective floor is max(absolute, proportional); WORKTREE_FREE_GB pins it.
DEFAULT_FREE_PCT = 0.05

# ── aggregate bloat-dir footprint (MYC-577) ──────────────────────────────────
# Default warn at 80K files across the bloat-prone dirs — a deliberate fraction
# of the ~200K+ melt threshold, so the signal lands while there is still room to
# act rather than after the machine is unusable.
DEFAULT_FILE_WARN = 80_000
DEFAULT_BLOAT_TTL_H = 24.0
# Wall-clock ceiling for one measurement pass. A SessionStart hook that can run
# long gets killed, and a killed hook reports nothing — indistinguishable from a
# healthy machine.
DEFAULT_BLOAT_DEADLINE_S = 3.0
# Vault-relative dirs that regrow. Each is REGENERABLE (a cache or a scratch
# checkout), which is why the remedy can be "delete it" rather than "triage it".
DEFAULT_BLOAT_DIRS = (
    ".claude/worktrees",
    ".smart-env",
    ".codegraph",
    "graphify-out",
)
# Scripts dir of the installed skill — where the auto-capable relocate helpers
# live. The offer prints ready-to-run commands against this canonical path.
SKILL_SCRIPTS = "~/.claude/skills/ai-brain-starter/scripts"



def _read_text_bounded(path: Path) -> str | None:
    """Shared regular-file boundary: skips placeholders/special files, bounded
    in size and wall-clock. Required because this module now walks recursively,
    and the dirs it walks (.smart-env especially) commonly live inside a synced
    vault where a plain read materializes placeholders and can stall."""
    result = safe_read_text(path, timeout=2.0, max_bytes=1_000_000)
    return result.text if result.ok else None


def _emit(ctx: str | None) -> int:
    if ctx:
        print(json.dumps({"continue": True, "additionalContext": ctx}))
    else:
        print(json.dumps({"continue": True, "suppressOutput": True}))
    return 0


# ── cloud-sync auto-detect + relocate OFFER (MYC-2360) ───────────────────────
# Fires UNCONDITIONALLY at SessionStart — independent of guided onboarding and
# of whether cwd is inside the vault. The freeze class (a git-backed brain inside
# iCloud/Drive/Dropbox/Box) is detected for ANY such vault and the auto-capable
# relocate scripts are OFFERED in plain language, rather than left as a passive
# "see docs" warning the user must act on. Self-silences once relocated:
# Shape A (relocate-vault.sh: move-local + symlink) resolves clean through
# detect_cloud_sync; Shape B (relocate-machinery-sidecar.sh: machinery out, notes
# stay) is suppressed by its sidecar manifest. Bounded by construction: a small
# JSON read + a few stats + a tiny manifest glob, never a corpus walk.


def _cwd_git_root() -> Path | None:
    """The git checkout cwd sits in — bounded upward `.git` search, so a vault
    that is git-backed but has no `.claude/worktrees/` yet (find_main_repo only
    resolves worktree-owners) is still discovered when you're cwd'd in it."""
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        return None
    for d in (cwd, *cwd.parents):
        try:
            if (d / ".git").exists():
                return d
        except OSError:
            continue
    return None


def _candidate_vaults(main_repo: Path | None) -> list[Path]:
    """Every vault to consider, INDEPENDENT of guided onboarding: the worktree-
    owning repo (find_main_repo, collapses an in-worktree cwd to its main vault)
    ∪ $CLAUDE_PROJECT_DIR ∪ the git checkout cwd is in ∪ the Obsidian registry
    ∪ $VAULT_ROOT. De-duped by resolved path, order preserved."""
    cands: list[Path] = []
    if main_repo is not None:
        cands.append(main_repo)
    pd = os.environ.get("CLAUDE_PROJECT_DIR")
    if pd:
        p = Path(pd)
        try:
            if p.is_dir():
                cands.append(p)
        except OSError:
            pass
    cwd_root = _cwd_git_root()
    if cwd_root is not None:
        cands.append(cwd_root)
    cands.extend(obsidian_vault_paths())
    vr = os.environ.get("VAULT_ROOT")
    if vr:
        cands.append(Path(vr).expanduser())
    out: list[Path] = []
    seen: set[str] = set()
    for c in cands:
        try:
            key = str(c.resolve())
        except OSError:
            key = str(c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _is_brain_vault(vault: Path) -> bool:
    """Offer only for an actual AI brain — git-backed or carrying substrate
    machinery — never a pristine markdown folder (markdown alone is low-churn,
    not the freeze class). Mirrors surface-backup-status.py's _is_brain."""
    try:
        if (vault / ".git").exists() or (vault / "CLAUDE.md").is_file():
            return True
        for c in vault.iterdir():
            if c.is_dir() and (c.name.endswith("Meta")
                               or c.name in (".smart-env", ".codegraph")):
                return True
    except OSError:
        return False
    return False


def _sidecar_roots() -> list[Path]:
    roots: list[Path] = []
    for env in ("BRAIN_SIDECAR", "MYCELIUM_SIDECAR"):
        v = os.environ.get(env)
        if v:
            roots.append(Path(v).expanduser())
    roots.append(Path.home() / ".brain-sidecar")
    return roots


def _machinery_relocated(vault: Path) -> bool:
    """True if a machinery-sidecar manifest names this vault (Shape B already
    applied: the vault may still sit in the cloud folder, but its churn is out,
    so the offer must go quiet — an existing user must not be nagged forever)."""
    try:
        vkey = str(vault.resolve())
    except OSError:
        vkey = str(vault)
    for root in _sidecar_roots():
        try:
            manifests = list((root / "manifests").glob("*.json"))
        except OSError:
            continue
        for man in manifests:
            raw = _read_text_bounded(man)
            if raw is None:
                continue
            try:
                doc = json.loads(raw)
            except ValueError:
                continue
            mv = doc.get("vault") if isinstance(doc, dict) else None
            if not isinstance(mv, str):
                continue
            try:
                if str(Path(mv).resolve()) == vkey:
                    return True
            except OSError:
                if mv == vkey:
                    return True
    return False


def _suggest_local_dest(vault: Path) -> Path:
    """A safe local destination to pre-fill the move-local command with."""
    name = vault.name or "brain"
    home = Path.home()
    cand = home / name
    try:
        if cand.exists():
            return home / "vaults" / name
    except OSError:
        pass
    return cand


def _offer_os() -> str:
    """Command flavor to print: 'windows' (PowerShell .ps1) or 'posix' (bash .sh).

    Keys on the running OS so a Windows/OneDrive user gets commands they can
    actually run (MYC-2383: the bash scripts are not natively runnable on
    Windows). WORKTREE_FOOTPRINT_OS={nt|posix} forces the branch so both can be
    covered on a single-OS CI runner; it changes ONLY which command strings
    render, never whether the offer fires.
    """
    forced = os.environ.get("WORKTREE_FOOTPRINT_OS", "").strip().lower()
    if forced in ("nt", "win", "windows"):
        return "windows"
    if forced in ("posix", "unix", "mac", "linux"):
        return "posix"
    return "windows" if os.name == "nt" else "posix"


def _offer_block(vault: Path, service: str, dest: Path) -> str:
    if _offer_os() == "windows":
        # Windows PowerShell 5.1 is always present; -ExecutionPolicy Bypass lets
        # an unsigned local script run. The .ps1 leaves a junction (no admin /
        # Developer Mode needed), which OneDrive does not sync through.
        move_cmd = (
            f'powershell -ExecutionPolicy Bypass -File '
            f'"{SKILL_SCRIPTS}/relocate-vault.ps1" "{vault}" "{dest}"'
        )
        sidecar_cmd = (
            f'powershell -ExecutionPolicy Bypass -File '
            f'"{SKILL_SCRIPTS}/relocate-machinery-sidecar.ps1" "{vault}"'
        )
        rb = "-Rollback"
        # The .ps1 does not yet stand up a backup itself (MYC-2383 tracks Windows
        # parity for --ensure-backup), so keep asking the assistant to stand one up
        # first for Windows/OneDrive users.
        move_note = (
            "move the whole vault to a local disk (leaves a link so nothing "
            "breaks). Stand up a verified off-machine backup FIRST"
        )
    else:
        # --ensure-backup makes THIS the one approved step: it stands up + verifies
        # an off-machine backup before moving, and refuses if that can't be verified
        # — so a non-technical user never has to know --force or run a backup by hand
        # (MYC-2404 — fulfills this offer's "stand up a backup first" promise in the
        # mechanism instead of relying on the assistant to remember).
        move_cmd = f'bash {SKILL_SCRIPTS}/relocate-vault.sh --ensure-backup "{vault}" "{dest}"'
        sidecar_cmd = f'bash {SKILL_SCRIPTS}/relocate-machinery-sidecar.sh "{vault}"'
        rb = "--rollback"
        move_note = (
            "move the whole vault to a local disk. This ONE command stands up + "
            "verifies an off-machine backup FIRST, then moves (leaves a link so "
            "nothing breaks), and refuses to move if the backup can't be verified"
        )
    return (
        f"🟡 [cloud-sync] The user's brain `{vault}` is inside **{service}** — the "
        f"#1 cause of the \"whole machine froze\" failure (the sync daemon chokes on "
        f"constant `.git`/index churn and pegs the CPU). FIX IT FOR THEM. Do NOT make "
        f"them choose between options or understand the internals — a non-technical "
        f"user will be overwhelmed and won't know what to answer. Recommend ONE thing, "
        f"explain it in a single plain sentence, ask a simple yes, then run it (stand "
        f"up a backup first).\n"
        f"  DEFAULT FIX — run THIS one. It leaves their notes exactly where they are "
        f"(still synced, still on their phone) and just moves the noisy hidden "
        f"machinery (`.git`, worktrees, caches) out of the sync folder so the computer "
        f"stops choking. Reversible (`{rb}`), deletes nothing:\n"
        f"      {sidecar_cmd}\n"
        f"  Fallback — ONLY if they explicitly say they don't want {service} at all: "
        f"{move_note}:\n"
        f"      {move_cmd}\n"
        f"Say it to them roughly like: \"Your notes are in {service}, and that combo "
        f"can freeze your computer. I can fix it safely in the background — your notes "
        f"don't move and nothing gets deleted. Want me to?\" Don't list both commands "
        f"to them; don't explain machinery-vs-notes. Repeats each session until fixed."
    )


def cloud_sync_offer(main_repo: Path | None) -> list[str]:
    """One plain-language offer block per cloud-resident, not-yet-relocated brain."""
    lines: list[str] = []
    for vault in _candidate_vaults(main_repo):
        try:
            if not _is_brain_vault(vault):
                continue
            service = detect_cloud_sync(vault)
            if not service:
                continue  # local disk, or Shape A symlink->local resolved clean
            if _machinery_relocated(vault):
                continue  # Shape B already applied
        except OSError:
            continue
        lines.append(_offer_block(vault, service, _suggest_local_dest(vault)))
    return lines


# ── aggregate bloat-dir footprint (MYC-577) ──────────────────────────────────
# worktree COUNT is one source of a multi-source failure. This measures the
# thing that actually melts the machine: total FILES across every regenerable
# bloat dir, whatever regrew them.


def _bloat_dirs() -> list:
    """Vault-relative bloat dirs: the shipped defaults plus any the operator
    adds via WORKTREE_FOOTPRINT_BLOAT_DIRS (comma-separated). De-duped, order
    preserved so the defaults report first."""
    dirs = list(DEFAULT_BLOAT_DIRS)
    extra = os.environ.get("WORKTREE_FOOTPRINT_BLOAT_DIRS", "")
    for raw in extra.split(","):
        rel = raw.strip().strip("/")
        if rel and rel not in dirs:
            dirs.append(rel)
    return dirs


def _deadline_seconds() -> float:
    """Wall-clock ceiling for the whole measurement pass."""
    try:
        return max(0.2, float(os.environ.get("WORKTREE_FOOTPRINT_DEADLINE_S",
                                             DEFAULT_BLOAT_DEADLINE_S)))
    except ValueError:
        return DEFAULT_BLOAT_DEADLINE_S


def _walk_lock():
    """Non-blocking single-instance lock, or None when another session holds it
    (or locking is unavailable, as on Windows — there the TTL alone throttles).
    Returns the open handle; the caller must _release_lock it."""
    try:
        import fcntl
    except ImportError:
        return _NO_FCNTL
    path = _bloat_cache_path().with_name(_bloat_cache_path().name + ".lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "w")
    except OSError:
        return _NO_FCNTL
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None  # someone else is walking → report from the last reading
    return fh


class _NoFcntl:
    """Sentinel: proceed without a lock (no fcntl on this platform)."""


_NO_FCNTL = _NoFcntl()


def _release_lock(handle) -> None:
    if handle is _NO_FCNTL or handle is None:
        return
    try:
        handle.close()  # closing drops the flock
    except OSError:
        pass


def _bloat_cache_path() -> Path:
    override = os.environ.get("WORKTREE_FOOTPRINT_CACHE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "footprint-bloat-cache.json"


def _count_files(root: Path, budget: int, deadline: float) -> tuple:
    """(files_counted, stopped_early) under `root`, never following symlinks.

    THREE independent stops, because a SessionStart walk that can run long is a
    hook-timeout waiting to happen, and a timed-out hook reports nothing — which
    reads exactly like a healthy machine (docs/HOOK_FLEET_RESOURCE_GOVERNANCE.md):
      * `budget`   — the only question here is "past the threshold?", so a melted
                     tree costs a bounded walk, never a full one;
      * `deadline` — wall-clock, checked per directory, so a slow or network
                     volume cannot stall the session even under the file budget;
      * symlinks are not followed — a vault commonly links back out of itself,
                     and following turns a bounded walk into a cyclic one.
    """
    seen = 0
    try:
        for _dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            seen += len(filenames)
            if seen >= budget:
                return budget, True
            if time.monotonic() >= deadline:
                return seen, True
            del dirnames  # walked in place; no pruning (every subtree counts)
    except OSError:
        pass
    return seen, False


def _measure_bloat(vault: Path, budget: int, deadline: float = None) -> dict:
    """One measurement pass, bounded by `budget` across ALL dirs combined (not
    per-dir) and by a shared wall-clock deadline, so one session's total cost
    never exceeds one budget and one deadline."""
    if deadline is None:
        deadline = time.monotonic() + _deadline_seconds()
    per_dir = {}
    total = 0
    truncated = False
    for rel in _bloat_dirs():
        d = vault / rel
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        remaining = max(0, budget - total)
        if remaining == 0 or time.monotonic() >= deadline:
            truncated = True
            break
        n, hit = _count_files(d, remaining, deadline)
        per_dir[rel] = n
        total += n
        if hit:
            truncated = True
            break
    return {"ts": time.time(), "total": total, "per_dir": per_dir,
            "truncated": truncated}


def _load_cache() -> dict:
    raw = _read_text_bounded(_bloat_cache_path())
    if raw is None:
        return {}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


MAX_CACHE_ENTRIES = 32


def _store_cache(doc: dict) -> None:
    """Persist, keeping only the most recent MAX_CACHE_ENTRIES vaults. Every
    distinct vault path ever seen would otherwise accumulate forever — a
    footprint tool that slowly grows its own footprint."""
    if len(doc) > MAX_CACHE_ENTRIES:
        def _ts(kv):
            try:
                return float(kv[1].get("ts", 0))
            except (AttributeError, TypeError, ValueError):
                return 0.0
        doc = dict(sorted(doc.items(), key=_ts, reverse=True)[:MAX_CACHE_ENTRIES])
    path = _bloat_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # observability is best-effort; never break SessionStart over it


def bloat_reading(vault: Path) -> dict:
    """Cached-or-fresh aggregate reading for `vault`.

    Frequency cap: recompute only when the cached entry is older than the TTL.
    Between recomputes the CACHED count still drives the warning, so a machine
    that crossed the threshold keeps being told every session — the cap bounds
    the WALK, not the signal.
    """
    try:
        warn_at = int(os.environ.get("WORKTREE_FOOTPRINT_FILE_WARN", DEFAULT_FILE_WARN))
    except ValueError:
        warn_at = DEFAULT_FILE_WARN
    warn_at = max(1, warn_at)
    try:
        ttl_h = float(os.environ.get("WORKTREE_FOOTPRINT_BLOAT_TTL_H", DEFAULT_BLOAT_TTL_H))
    except ValueError:
        ttl_h = DEFAULT_BLOAT_TTL_H

    try:
        key = str(vault.resolve())
    except OSError:
        key = str(vault)

    cache = _load_cache()
    entry = cache.get(key) if isinstance(cache.get(key), dict) else None
    now = time.time()
    fresh = False
    if entry is not None:
        try:
            fresh = (now - float(entry.get("ts", 0))) < ttl_h * 3600
        except (TypeError, ValueError):
            fresh = False

    if not fresh:
        # SINGLE INSTANCE. Several sessions commonly start at once (a relaunch,
        # a fleet of worktrees); without this they would all walk the same tree
        # simultaneously — N times the cost, at the worst possible moment on the
        # exact machine this signal exists to protect. A session that loses the
        # race reports from the previous reading instead of waiting.
        lock = _walk_lock()
        if lock is None:
            entry = dict(entry or {"total": 0, "per_dir": {}, "truncated": False})
        else:
            try:
                # STAMP THE COOLDOWN FIRST, carrying the previous counts forward.
                # If the walk is killed (hook timeout, session torn down), the
                # cooldown still holds — otherwise every session would re-walk
                # forever, which is the pathological case — and the last GOOD
                # reading survives rather than being replaced by a partial zero.
                # stamp-at-start: the cache entry below IS the cooldown marker,
                # written before _measure_bloat runs. The boundedness auditor
                # scans linearly for a stamp above the os.walk LINE, and the walk
                # lives in _count_files (defined earlier in this file), so it
                # cannot see the real ordering. The annotation is not a
                # suppression — hooks/test_footprint_aggregate_bloat.py asserts
                # the stamp actually lands before the walk, and fails if the two
                # are ever reordered.
                prev = dict(entry or {})
                cache[key] = {"ts": now,
                              "total": int(prev.get("total", 0) or 0),
                              "per_dir": prev.get("per_dir") or {},
                              "truncated": bool(prev.get("truncated", False)),
                              "pending": True}
                _store_cache(cache)
                # Budget the walk at 2x the threshold: enough headroom to report
                # a meaningfully-over number, still bounded on a melted tree.
                entry = _measure_bloat(vault, warn_at * 2)
                cache = _load_cache()          # re-read: another session may have
                cache[key] = entry             # stored a different vault meanwhile
                _store_cache(cache)
            finally:
                _release_lock(lock)

    entry = dict(entry or {})
    entry["warn_at"] = warn_at
    entry["stale_seconds"] = max(0.0, now - float(entry.get("ts", now) or now))
    return entry


def bloat_signal(vault: Path) -> str:
    """The warning line, or "" when the aggregate is under threshold."""
    r = bloat_reading(vault)
    total = int(r.get("total", 0))
    warn_at = int(r.get("warn_at", DEFAULT_FILE_WARN))
    if total < warn_at:
        return ""
    per_dir = r.get("per_dir") or {}
    top = sorted(
        ((k, int(v)) for k, v in per_dir.items() if isinstance(v, int)),
        key=lambda kv: kv[1], reverse=True,
    )[:4]
    breakdown = ", ".join(f"{k} {n:,}" for k, n in top if n) or "spread across bloat dirs"
    count = f"{total:,}+" if r.get("truncated") else f"{total:,}"
    age_h = float(r.get("stale_seconds", 0)) / 3600
    when = "just measured" if age_h < 1 else f"measured {age_h:.0f}h ago"
    return (
        f"⚠️  [footprint] {count} files across the vault's regenerable bloat dirs "
        f"(warn at {warn_at:,}; {when}). The machine-freeze class starts near ~200K "
        f"files, and it is the AGGREGATE that gets there — worktrees alone never "
        f"do. Biggest: {breakdown}.\n"
        f"  Remedy — every one of these regenerates, so deleting is safe:\n"
        f"    bash scripts/worktree-prune.sh            # scratch checkouts\n"
        f"    rm -rf '{vault}/.smart-env'               # Obsidian Smart Connections index\n"
        f"    python3 scripts/graphify_prune_stale_cache.py   # stale graph cache\n"
        f"  Recount is frequency-capped to ~{DEFAULT_BLOAT_TTL_H:.0f}h, so this line "
        f"repeats until the next pass sees it drop."
    )


DEFAULT_SIBLING_WARN = 20


def sibling_worktree_signal() -> str:
    """Orphaned <dev-root>/<repo>-<slug> session worktrees (MYC-587).

    `claude-dev-worktree cleanup` removes these; a session that ends without it
    leaves one behind, and nothing swept them at all — the vault's own pruner
    excludes them by design. A 2026-06-07 probe found 60+.

    Bounded by construction: ONE iterdir level and an is_file() per child, no
    git calls and no recursion, so this stays cheap even on a large fleet.
    """
    try:
        warn_at = max(1, int(os.environ.get("DEV_WORKTREE_WARN", DEFAULT_SIBLING_WARN)))
    except ValueError:
        warn_at = DEFAULT_SIBLING_WARN
    root = os.environ.get("ABS_DEV_ROOT", "").strip()
    dev_root = Path(root).expanduser() if root else Path.home() / "dev"
    try:
        if not dev_root.is_dir():
            return ""
        # A LINKED worktree's `.git` is a pointer FILE; a real checkout's is a
        # directory. That discriminator is what keeps actual clones out.
        n = sum(1 for c in dev_root.iterdir()
                if c.is_dir() and (c / ".git").is_file())
    except OSError:
        return ""
    if n <= warn_at:
        return ""
    return (
        f"[footprint] {n} leftover session worktree(s) under {dev_root} "
        f"(soft cap {warn_at}). Each is close to a full checkout. The daily "
        f"maintenance pass reclaims the safe ones automatically; force it now "
        f"with `python3 scripts/dev-worktree-prune.py` (dry-run) then `--apply` "
        f"— dirty ones are snapshotted first and un-backed-up ones are kept."
    )


def main() -> int:
    if os.environ.get("WORKTREE_FOOTPRINT_BYPASS") == "1":
        return _emit(None)

    main_repo = find_main_repo()

    lines: list[str] = []

    # Cloud-sync relocate OFFER — runs even when cwd is NOT inside a vault, so a
    # pre-existing iCloud Obsidian vault (Obsidian's common default) or a skipped
    # onboarding is still caught and the fix is OFFERED, not just documented.
    # (MYC-2360) Replaces the old passive "move it out, see docs" warning.
    lines.extend(cloud_sync_offer(main_repo))

    # Aggregate bloat-dir footprint (MYC-577). Deliberately NOT gated on
    # main_repo: the dirs that regrew in the melt (.smart-env above all) live in
    # the Obsidian vault, which need not be the worktree-owning repo — and the
    # whole point is that this fires with ZERO excess worktrees. Capped at the
    # first 3 brain vaults so a machine registering many vaults still pays a
    # bounded SessionStart.
    for vault in _candidate_vaults(main_repo)[:3]:
        try:
            if not _is_brain_vault(vault):
                continue
            sig = bloat_signal(vault)
        except OSError:
            continue
        if sig:
            lines.append(sig)

    # Leftover session worktrees under the dev root (MYC-587). Independent of
    # main_repo: these are code-repo siblings, not vault scratch worktrees.
    sib = sibling_worktree_signal()
    if sib:
        lines.append(sib)

    # Footprint observability needs a repo to measure — gated on main_repo.
    if main_repo is not None:
        try:
            warn_at = max(1, int(os.environ.get("WORKTREE_WARN", DEFAULT_WARN)))
        except ValueError:
            warn_at = DEFAULT_WARN
        # None => derive from the volume; an explicit WORKTREE_FREE_GB pins it.
        try:
            pinned_floor = os.environ.get("WORKTREE_FREE_GB")
            free_floor = float(pinned_floor) if pinned_floor else None
        except ValueError:
            free_floor = None

        # Count only scratch worktrees for the cap warning; deliberate sibling
        # worktrees (~/dev/<repo>-<slug>) are not part of the pileup problem.
        registered = sum(
            1 for w in (list_worktrees(main_repo) or []) if is_scratch_worktree(w)
        )

        wt_dir = main_repo / WORKTREES_SEG
        on_disk = 0
        if wt_dir.is_dir():
            try:
                on_disk = sum(1 for c in wt_dir.iterdir() if c.is_dir())
            except OSError:
                on_disk = 0
        orphans = max(0, on_disk - registered)

        if registered > warn_at or orphans > 0:
            bits = [f"{registered} registered worktree(s) (soft cap {warn_at})"]
            if orphans:
                bits.append(f"{orphans} orphan dir(s) git no longer tracks")
            lines.append(
                f"[footprint] {'; '.join(bits)}. Each worktree is ~a full checkout. "
                f"They auto-trim at SessionEnd + via the cap; force now with "
                f"`python3 scripts/worktree-prune.sh` or the reclaim tool."
            )

        free_gb = None
        try:
            usage = shutil.disk_usage(main_repo)
            free_gb = usage.free / 1024 ** 3
            if free_floor is None:
                free_floor = max(
                    DEFAULT_FREE_GB, (usage.total / 1024 ** 3) * DEFAULT_FREE_PCT
                )
        except OSError:
            pass
        if free_gb is not None and free_floor is not None and free_gb < free_floor:
            pct = free_gb / (usage.total / 1024 ** 3) * 100
            lines.append(
                f"⚠️  [footprint] Low free disk: {free_gb:.1f} GB free "
                f"({pct:.0f}% of the volume) — warns below {free_floor:.0f} GB. "
                f"Biggest reclaimable win is usually regenerable build output "
                f"(node_modules / target / .venv / .next) in idle worktrees."
            )

    return _emit("\n".join(lines) if lines else None)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({"continue": True, "suppressOutput": True}))
        sys.exit(0)
