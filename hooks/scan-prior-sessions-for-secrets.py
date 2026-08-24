#!/usr/bin/env python3
"""SessionStart hook: scan every project's session JSONL for unredacted secrets.

Defense layer 4 of the secret-detection stack. PreToolUse hookify blocks
forward, PostToolUse redacts on detect, SessionEnd scrubs on close, and
THIS hook catches anything historical that slipped through — from sessions
that ended before the scrub hook existed, or before a given pattern was
added to the registry.

Resource governance (added after a 2026-06-05 freeze on the maintainer's
machine, where the OLD version stamped its 6h cooldown AFTER the slow scan:
every session that started during the scan window blew past the cooldown
and launched its OWN full corpus scan, and four concurrent multi-minute
scans pegged the CPU until the machine froze). The pile-up class is now
closed by FOUR guards, all in this file:

  * single-instance flock — at most ONE scan runs at a time; a concurrent
    session backs off immediately instead of starting a second scan;
  * stamp-at-START — the cooldown marker is claimed BEFORE the work, so a
    session that starts mid-scan sees the cooldown and skips;
  * incremental — once a full pass completes, later runs only read files
    modified since that pass (a handful, not the whole corpus);
  * wall-clock budget + per-file size cap + os.nice(10) — a single pass can
    never run away or starve the foreground session.

Cross-platform note: this stays a SessionStart hook rather than a launchd /
cron worker. ai-brain-starter installs on macOS, Linux, AND Windows, so a
launchd-only worker would silently disable the historical sweep everywhere
except macOS. The four in-session guards above bound the cost so the cold-start
path is safe on every platform; a maintainer who wants the heavy walk fully off
cold-start can schedule this script out-of-band (it is safe to run standalone —
the single-instance flock + budget apply either way).

Behavior:
  - Detect: scans every `~/.claude/projects/**/*.jsonl` against the
    registry (`_lib/secret_patterns.scan`). Rate-limited to one run per 6h
    via `.last-secret-scan` marker.
  - Auto-scrub (opt-in via `VAULT_ROOT` env var): if a JSONL's parent
    worktree is NOT currently in `git worktree list`, backups + redacts
    the file in-place. Logs each scrub to
    `~/.claude/secret-detection-log.jsonl` (structured records, NEVER
    secret values).
  - Mid-session safety: a JSONL whose worktree IS active is SKIPPED with
    "will retry next SessionStart after close" — scrubbing a mid-session
    JSONL corrupts Claude Code's resume state.
  - Fail-closed scrub decision: if the active-worktree set cannot be
    positively determined (git error) OR is empty, scrub NOTHING — an
    empty/unknown set is never proof a JSONL is safe (the current session may
    run from the main checkout, which has no `claude/*` worktree entry). See
    `partition_for_scrub`.
  - Warn: surfaces residual findings (post-scrub) + threat-model reminder
    so the next response checks the leak vector before recommending
    rotation.

Environment:
  - `VAULT_ROOT` — absolute path to the user's vault git repo. **Required
    for auto-scrub.** Without it the hook cannot verify which worktrees
    are active and falls back to warn-only mode (safe default — never
    risks corrupting a mid-session JSONL).
  - `SCAN_PRIOR_AUTO_SCRUB_BYPASS=1` — forensic mode: skip auto-scrub
    even when VAULT_ROOT is set. Useful for inspecting findings before
    scrubbing.
  - `SECRET_SCAN_BUDGET_SECONDS` — override the per-pass wall-clock budget
    (applies to both the cold and warm budgets). Ops escape hatch.

Pairs with the other secret-defense layers; see `_lib/secret_patterns.py`
docstring for the full architecture.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Platform locking primitives. `import fcntl` at module scope crashed this hook
# at IMPORT on every Windows install -- fcntl is POSIX-only -- so a SessionStart
# secret scanner that the install reports as present has never run there. Same
# idiom as hooks/session-lock.py, which already guards this correctly.
try:
    import fcntl  # POSIX only; absent on Windows.
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None
try:
    import msvcrt  # Windows only; absent on POSIX.
except ImportError:  # pragma: no cover - platform-dependent
    msvcrt = None

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))

from _lib.safe_read import safe_read_bytes, safe_read_text  # noqa: E402
from _lib.secret_patterns import redact, scan  # noqa: E402

MARKER = HOOK_DIR / ".last-secret-scan"            # last ATTEMPT (cooldown, stamped at start)
COOLDOWN_SECONDS = 6 * 60 * 60  # 6h
FULL_MARKER = HOOK_DIR / ".last-secret-scan-full"  # last COMPLETED full pass (incremental baseline)
LOCK = HOOK_DIR / ".secret-scan.lock"              # single-instance guard (flock)
MAX_FILE_BYTES = 3 * 1024 * 1024                    # skip outsized transcripts (cost without proportional secret risk)
# Per-pass wall-clock ceiling. COLD start (no completed full pass yet) gets a
# generous budget so it can finish ONE full pass and prime the incremental
# baseline — else it truncates forever and silently covers only part of the
# corpus. WARM runs are incremental (only changed files) so a short budget is
# ample. Env override wins for ops.
_BUDGET_OVERRIDE = os.environ.get("SECRET_SCAN_BUDGET_SECONDS")
COLD_BUDGET_SECONDS = int(_BUDGET_OVERRIDE) if _BUDGET_OVERRIDE else 600
WARM_BUDGET_SECONDS = int(_BUDGET_OVERRIDE) if _BUDGET_OVERRIDE else 60

# Auto-scrub additions
AUTO_SCRUB_LOG = Path.home() / ".claude" / "secret-detection-log.jsonl"
VAULT_ROOT_ENV = "VAULT_ROOT"
BYPASS_ENV = "SCAN_PRIOR_AUTO_SCRUB_BYPASS"
BACKUP_SUFFIX_TEMPLATE = ".bak.{date}-secret-scrub"


def _read_epoch(path: Path) -> float | None:
    # A cooldown marker is a handful of bytes in the hook's own directory, but
    # it goes through safe_read like everything else: one read primitive, no
    # judgement calls at each call site about which reads are "small enough".
    result = safe_read_text(path, max_bytes=64)
    if not result.ok:
        return None
    try:
        return float(result.text.strip())
    except ValueError:
        return None


def _write_epoch(path: Path) -> None:
    try:
        path.write_text(f"{time.time():.0f}\n")
    except OSError:
        pass


def _stamp() -> None:
    _write_epoch(MARKER)


def _vault_root() -> Path | None:
    """Resolve VAULT_ROOT or return None (disables auto-scrub safely)."""
    # vault-root-ok: here VAULT_ROOT is an explicit opt-in SWITCH for auto-scrub,
    # not vault detection (see "Environment" above). There is no ~/vault default:
    # unset returns None and the hook drops to warn-only, the safe mode. Detecting
    # a vault instead would silently ENABLE in-place redaction on a vault the user
    # never opted in for, so the naive read is the correct behavior here.
    val = os.environ.get(VAULT_ROOT_ENV, "").strip()
    if not val:
        return None
    p = Path(val).expanduser()
    return p if p.exists() else None


def _active_worktree_slugs(vault_root: Path) -> set[str] | None:
    """Slugs of currently active vault worktrees, or None when UNKNOWN.

    Slugs look like `silly-edison-565ec5` from branches named
    `claude/silly-edison-565ec5`. Used to skip scrubbing JSONLs whose
    parent worktree is mid-session — a mid-session scrub corrupts Claude
    Code's resume state.

    Returns None — the UNKNOWN sentinel — when the set cannot be determined
    (git error / timeout / non-zero exit). The OLD code returned an EMPTY set
    on these errors, which the consumer could NOT tell apart from "git ran,
    genuinely zero active worktrees" — so on any git error it scrubbed
    EVERYTHING, including active-session JSONLs (fail-OPEN; the exact
    corruption the skip exists to prevent). Callers MUST treat None (and an
    empty set) as "do not scrub". See partition_for_scrub().

    output_mapping: error / cannot-determine -> None ; ran-ok -> set (maybe empty).
    """
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(vault_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    slugs: set[str] = set()
    for line in proc.stdout.splitlines():
        if line.startswith("branch refs/heads/claude/"):
            slugs.add(line.split("refs/heads/claude/", 1)[1].strip())
    return slugs


def _jsonl_is_in_active_worktree(jsonl: Path, active_slugs: set[str]) -> bool:
    """A JSONL belongs to an active worktree if its project-dir basename
    contains any active slug. Project dirs encode the worktree path with `/`
    replaced by `-`. Subagent files live one level deeper in `subagents/`;
    walk up to the project root before matching.
    """
    project_dir = jsonl.parent
    if project_dir.name == "subagents":
        project_dir = project_dir.parent.parent
    name = project_dir.name
    return any(slug in name for slug in active_slugs)


def partition_for_scrub(
    finding_paths: list[Path], active_slugs: set[str] | None
) -> tuple[list[Path], list[Path], list[Path]]:
    """Decide which findings are safe to auto-scrub. PURE + the fail-closed core.

    Returns (to_scrub, skipped_active, skipped_unknown).

    FAIL-CLOSED: when `active_slugs` is None (could not determine) OR an EMPTY
    set (no worktree positively identified as active), we cannot confirm any
    JSONL is safe to scrub — the current session may be running from the main
    checkout, which leaves no `claude/*` worktree entry. So scrub NOTHING; every
    finding goes to `skipped_unknown`. The warn surface still reports them for
    manual review.

    With a NON-EMPTY active set we have a positive boundary: scrub everything
    that is NOT inside an active worktree (`to_scrub`), skip the ones that are
    (`skipped_active`).

    output_mapping: `not active_slugs` (None or empty) -> to_scrub is ALWAYS []
    (the safe side). Only a non-empty set ever yields scrub targets.
    """
    if not active_slugs:  # None or empty set -> fail closed, scrub nothing
        return [], [], list(finding_paths)
    to_scrub: list[Path] = []
    skipped_active: list[Path] = []
    for p in finding_paths:
        if _jsonl_is_in_active_worktree(p, active_slugs):
            skipped_active.append(p)
        else:
            to_scrub.append(p)
    return to_scrub, skipped_active, []


def _log_scrub(record: dict) -> None:
    """Append a structured record to ~/.claude/secret-detection-log.jsonl.

    Records what was scrubbed, when, and why. NEVER includes secret values.
    Schema: timestamp_iso, action, jsonl_path, backup_path, redacted_patterns,
    redaction_count, reason.
    """
    try:
        AUTO_SCRUB_LOG.parent.mkdir(parents=True, exist_ok=True)
        # Builtin open(path, mode), not AUTO_SCRUB_LOG.open(mode): the
        # cloud-safe-walker guard reads the mode from args[1], which only exists
        # on the builtin form, so the bound-method form is misread as a READ and
        # falsely flagged (filed separately). encoding is explicit because the
        # record is dumped with ensure_ascii=False -- on a Windows cp1252
        # default this would raise UnicodeEncodeError on any non-ASCII secret name.
        with open(AUTO_SCRUB_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # Logging is best-effort; never blocks the hook.


def _auto_scrub(jsonl: Path) -> tuple[bool, str]:
    """Backup + scrub a single JSONL. Returns (success, message).

    Backup naming: <file>.bak.YYYYMMDD-secret-scrub. Skips if the backup
    already exists (idempotent across cooldown re-fires within one day).
    """
    today = _dt.date.today().strftime("%Y%m%d")
    backup = jsonl.with_name(jsonl.name + BACKUP_SUFFIX_TEMPLATE.format(date=today))
    if backup.exists():
        return False, f"backup {backup.name} already exists — skipping"
    # ONE bounded, cloud-safe read serves both the backup and the scrub. The
    # backup is written from the raw BYTES, not from decoded text, so it stays
    # byte-exact even when the transcript contains invalid UTF-8 -- a lossy
    # backup of a file we are about to overwrite would be worse than none.
    # A file we cannot safely read is not scrubbed: refusing is the safe
    # direction when the alternative is overwriting an original we never saw.
    result = safe_read_bytes(jsonl, max_bytes=MAX_FILE_BYTES)
    if not result.ok:
        return False, f"unreadable ({result.status}) — not scrubbed"
    raw = result.data or b""
    try:
        backup.write_bytes(raw)
    except OSError as exc:
        return False, f"backup failed: {exc}"
    redacted, hit_names = redact(raw.decode("utf-8", errors="replace"))
    if not hit_names:
        backup.unlink(missing_ok=True)  # No-op scrub; drop the backup.
        return False, "no patterns matched on re-scan (registry may have tightened)"
    try:
        jsonl.write_text(redacted)
    except OSError as exc:
        return False, f"scrub failed: {exc}"
    _log_scrub(
        {
            "timestamp_iso": _dt.datetime.now().isoformat(),
            "action": "auto_scrub_closed_session_jsonl",
            "jsonl_path": str(jsonl),
            "backup_path": str(backup),
            "redacted_patterns": hit_names,
            "redaction_count": len(hit_names),
            "reason": "SessionStart auto-scrub (closed worktree)",
        }
    )
    return True, f"scrubbed {len(hit_names)} pattern(s): {', '.join(hit_names)}"


def _acquire_single_instance(fh) -> bool:
    """Take the exclusive, non-blocking single-instance lock. True = acquired.

    This lock is what stops the SessionStart pile-up that pegged the CPU and
    froze the maintainer's machine (load 36) on 2026-06-05, so it has to
    actually hold on every platform this installs on. Failing OPEN when the
    POSIX primitive is missing would quietly restore that incident on Windows,
    which is why the no-primitive branch backs off instead of scanning.

    fcntl on POSIX, msvcrt on Windows. Exactly one of the two exists on any
    real interpreter, so the final return is a belt-and-braces branch.
    """
    if fcntl is not None:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if msvcrt is not None:
        try:
            # Locks one byte from the current offset; the region may sit past
            # EOF, which is how a zero-length lockfile is locked on Windows.
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    return False


def main() -> int:
    # Cooldown (fast path): skip if a scan was ATTEMPTED < COOLDOWN_SECONDS ago.
    # The marker is stamped at the START of a run (below), so a session that
    # starts mid-scan sees a fresh marker and backs off here.
    last_attempt = _read_epoch(MARKER)
    if last_attempt is not None and (time.time() - last_attempt) < COOLDOWN_SECONDS:
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    # Single-instance lock. If another scan already holds it, back off NOW.
    # This is the primary guard against the SessionStart pile-up that froze
    # the maintainer's machine on 2026-06-05: without it, N concurrent
    # sessions each launched their own full corpus scan.
    try:
        # Builtin form for the same guard reason as _log_scrub's append.
        _lock_fh = open(LOCK, "w")  # noqa: F841 (held open to keep the lock for the process lifetime)
    except OSError:
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0
    if not _acquire_single_instance(_lock_fh):
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    # Claim the cooldown NOW (before the work) so concurrent starts back off.
    _stamp()

    # De-prioritise: never starve the foreground session, even mid-scan.
    # AttributeError, not just OSError: os.nice does not EXIST on Windows, and
    # `except OSError` would not have caught that -- a second, later crash in
    # the same hook on the same platform.
    try:
        os.nice(10)
    except (OSError, AttributeError):
        pass

    projects = Path.home() / ".claude" / "projects"
    if not projects.exists():
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    # Incremental: once a full pass has completed, only read files modified
    # since then (unchanged files were already covered). Turns a 1000+-file
    # walk into the handful touched in the last 6h. Bounded by a wall-clock
    # budget and a per-file size cap so a single pass can never run away.
    full_baseline = _read_epoch(FULL_MARKER)
    incremental = full_baseline is not None
    cutoff = (full_baseline or 0) - 5  # small clock-skew buffer
    deadline = time.time() + (WARM_BUDGET_SECONDS if incremental else COLD_BUDGET_SECONDS)
    truncated = False

    findings: list[tuple[Path, list[tuple[str, int]]]] = []
    unreadable = 0
    for jsonl in projects.rglob("*.jsonl"):
        if time.time() > deadline:
            truncated = True
            break
        try:
            st = jsonl.stat()
        except OSError:
            continue
        if incremental and st.st_mtime < cutoff:
            continue
        if st.st_size > MAX_FILE_BYTES:
            continue  # outsized transcript: cost without proportional secret risk
        # Bounded, cloud-safe read: a transcript sitting in an offline OneDrive /
        # iCloud placeholder must never be materialised (and never block) just to
        # be scanned. `unreadable` is COUNTED and surfaced below -- for a secret
        # scanner "could not read" means "not scanned", and a coverage gap that
        # reports nothing is the failure mode this whole hook exists to avoid.
        result = safe_read_text(jsonl, max_bytes=MAX_FILE_BYTES, errors="replace")
        if not result.ok:
            unreadable += 1
            continue
        hits = scan(result.text)
        if hits:
            findings.append((jsonl, hits))

    # Advance the incremental baseline only after a pass that finished without
    # truncation — so a truncated pass never leaves an unscanned hole.
    if not truncated:
        _write_epoch(FULL_MARKER)

    if not findings:
        # `unreadable` rides along even on the clean path: "no findings" and
        # "no findings in the files I could actually open" are different claims.
        out = {"continue": True, "suppressOutput": True}
        if unreadable:
            out["unreadable"] = unreadable
        print(json.dumps(out))
        return 0

    # Cap to most-recent 5 to keep the warning short.
    findings.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
    findings = findings[:5]

    # Auto-scrub: requires VAULT_ROOT set + not bypassed. Without VAULT_ROOT
    # we cannot detect active worktrees, so we fall back to warn-only (safe).
    bypass_auto_scrub = os.environ.get(BYPASS_ENV, "").strip() not in ("", "0", "false")
    vault_root = _vault_root()
    auto_scrub_enabled = (vault_root is not None) and (not bypass_auto_scrub)

    # FAIL-CLOSED partition. `_active_worktree_slugs` returns None on a git
    # error and a (possibly empty) set when it ran cleanly. partition_for_scrub
    # treats BOTH None and empty as "cannot confirm safe -> scrub nothing", so a
    # git hiccup (or a session running from the main checkout, which has no
    # `claude/*` worktree entry) can never scrub an active session's JSONL — the
    # corruption the active-worktree skip exists to prevent.
    finding_paths = [path for path, _hits in findings]
    if auto_scrub_enabled:
        active_slugs = _active_worktree_slugs(vault_root)  # set | None
        to_scrub, skipped_active, skipped_unknown = partition_for_scrub(
            finding_paths, active_slugs
        )
    else:
        active_slugs = None
        to_scrub, skipped_active, skipped_unknown = [], [], []

    auto_scrubbed: list[tuple[Path, str]] = []
    for path in to_scrub:
        ok, msg = _auto_scrub(path)
        if ok:
            auto_scrubbed.append((path, msg))

    # Re-scan remaining findings post-scrub so the warning surface reflects
    # actual residual exposure, not pre-scrub state.
    residual_findings: list[tuple[Path, list[tuple[str, int]]]] = []
    for path, _hits in findings:
        result = safe_read_text(path, max_bytes=MAX_FILE_BYTES, errors="replace")
        if not result.ok:
            # It scanned dirty a moment ago, so failing to re-read it now must
            # not silently downgrade it to "clean" -- keep the pre-scrub hits.
            residual_findings.append((path, _hits))
            continue
        new_hits = scan(result.text)
        if new_hits:
            residual_findings.append((path, new_hits))

    lines = [
        "🔒 [secret-scan] Found unredacted secrets in prior session JSONLs:",
    ]
    if unreadable:
        lines.append(
            f"NOTE: {unreadable} transcript(s) could not be read (offline "
            f"cloud placeholder, or too large) and were NOT scanned — this "
            f"report is not a clean bill of health for those."
        )
    if auto_scrubbed:
        lines.append(f"Auto-scrubbed {len(auto_scrubbed)} closed-worktree JSONL(s):")
        for path, msg in auto_scrubbed:
            lines.append(f"  ✓ {path.name}: {msg}")
    if skipped_active:
        lines.append(f"Skipped {len(skipped_active)} active-worktree JSONL(s) (mid-session safety):")
        for path in skipped_active:
            lines.append(f"  ⏸  {path.name}: will retry next SessionStart after close")
    if skipped_unknown and auto_scrub_enabled:
        lines.append(
            f"Skipped {len(skipped_unknown)} JSONL(s) — could not confirm which "
            f"worktrees are active (git error, or none detected). FAIL-CLOSED: "
            f"scrubbed nothing (a mid-session scrub corrupts resume state):"
        )
        for path in skipped_unknown:
            lines.append(f"  ⏸  {path.name}: left intact; retries once active worktrees are detectable")
    if residual_findings:
        lines.append("Residual findings (re-scan post-scrub):")
        for path, hits in residual_findings:
            summary = ", ".join(f"{n}×{c}" for n, c in hits)
            lines.append(f"  ⚠ {path.name}: {summary}")
    if not (auto_scrubbed or skipped_active or residual_findings):
        # Defensive fallback (shouldn't reach given the earlier early-return).
        for path, hits in findings:
            summary = ", ".join(f"{n}×{c}" for n, c in hits)
            lines.append(f"  {path.name}: {summary}")
    if not auto_scrub_enabled:
        if vault_root is None:
            lines.append(
                f"Auto-scrub DISABLED ({VAULT_ROOT_ENV} env var not set or path missing). "
                f"Export VAULT_ROOT=/path/to/your/vault to enable closed-session auto-scrub."
            )
        elif bypass_auto_scrub:
            lines.append(f"Auto-scrub BYPASSED via {BYPASS_ENV}=1.")
    lines.append(f"Auto-scrub log: {AUTO_SCRUB_LOG}")
    lines.append(
        "Threat-model check before recommending rotation: when a leak is "
        "confined to local disk + the model API context only, rotation is "
        "rarely the right call; rotate when the vector extends to public "
        "internet / third-party services / untrusted machines / broadcast "
        "channels. See hookify rule "
        "`warn-rotation-push-on-local-only-leak.local.md`."
    )
    lines.append(
        f"Manual scrub: python3 -c \"import sys; sys.path.insert(0, '{HOOK_DIR}'); "
        f"from _lib.secret_patterns import redact; from pathlib import Path; "
        f"p = Path('<path>'); p.write_text(redact(p.read_text())[0])\""
    )

    warning = "\n".join(lines)
    print(warning, file=sys.stderr)
    print(
        json.dumps(
            {
                "continue": True,
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": warning,
                },
            }
        )
    )
    return 0


if __name__ == "__main__":
    # This hook prints a lock emoji and warning glyphs in its findings block, so
    # on a Windows cp1252 console an unguarded print() raises UnicodeEncodeError
    # and the caller reads the empty output as "no findings" (#313). Latent
    # until now only because `import fcntl` killed the module before any of it
    # ran; making the hook work on Windows makes this the NEXT crash in line.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
