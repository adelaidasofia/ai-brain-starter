#!/usr/bin/env python3
"""
Stop hook: blocks responses that CLAIM to close the session without actually
running the FULL cascade (session file + session-close-runner.sh report).

Failure modes this prevents:
  - 2026-05-10 busy-pasteur: model wrote summary saying "closing the session"
    but never ran Phase 0-3 of ⚙️ Meta/rules/session-close.md.
  - 2026-05-11 gallant-kalam: model wrote session file + committed it, then
    posted a "## Session ... — final summary" message claiming closure
    without running session-close-runner.sh (aggregators, Phase 0c-e,
    worktree-settle). the user flagged: "this keeps happening, make the fix
    permanent."

Three-gate check when a closing claim is detected:
  1. Session file exists at <meta>/Sessions/YYYY-MM-DD*<worktree>*.md
  2. session-close-runner.sh ran in the last 30 min — verified via
     /tmp/abs-session-close-runner.report ending in `RUNNER COMPLETE @ <ts>`.
  3. No uncommitted session-close artifacts (today's Sessions/Decisions/
     Captures files must be staged + committed via vault-safe-commit.sh).

All three must be true. Two-gate version was the 2026-05-12 funny-golick
gap: session file existed, runner ran, but 5 files (session + 3 decisions
+ Captures + to-do append) sat uncommitted until the worktree archive
prompt caught them. Permanent-fix-pattern: don't rely on user-visible UI
warnings; block at the model layer.

Spanish closing patterns added 2026-05-13 — the same session's goodbye
("Que descanses, Ade") slipped past the English-only regex, so the
three-gate check never even fired.

2026-06-30: VAULT_ROOT is now resolved repo-aware (see _lib/vault_root.py),
in lockstep with detect-closing-signal.py. Before this fix, VAULT_ROOT was
read straight from the env var — permanently, for every repo, whenever a
machine-wide default was configured. A session working inside its own
vault-shaped repo (own CLAUDE.md, own Session End/Close cascade) had its
session file correctly written there by detect-closing-signal.py's own
repo-aware fix, but THIS hook still checked the unrelated default vault's
Sessions/ dir and runner state — turning a silent mis-filing into an
active false hard-block quoting the wrong vault's missing files.

FAIL-SAFE / conditional enforcement (so this hook is safe to wire by
default for every vault):
  - The hard-block (exit 2) is gated on the session-close cascade actually
    being INSTALLED in this vault — i.e. <meta>/scripts/session-close-runner.sh
    exists. If it does NOT, the vault never opted into the cascade and the
    hook NEVER blocks; it degrades to a non-blocking advisory. This prevents
    the "missing runner blocks every close forever" failure: gate 2 can only
    fire against a runner that is actually present to run.
  - When the runner IS installed, the user has opted into the close
    machinery, so all three gates get teeth.

Bypass / overrides:
  - VERIFY_CASCADE_BYPASS=1  — skip the check entirely (no block, no advisory).
  - VERIFY_CASCADE_SOFT=1    — force advisory mode even when the runner is
                               installed (warn, never block).
"""

# MYC-3529: REQUIRED, not cosmetic. This module annotates with PEP-604
# `X | None`, which is evaluated at def-time and is a TypeError on Python
# 3.9 -- the floor version scripts/ci.sh's gate actually runs. py_compile
# does NOT catch it (the annotation compiles fine and only blows up when
# the def executes), so the import crash is invisible to the lint gates and
# shows up only as a hook that silently does nothing.
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Shared close-claim detector - single source of truth (_lib/closing_claim.py).
# MENTION-vs-USE aware: a sign-off QUOTED as an example or DISCUSSED as meta is
# not a close claim. Replaces this hook's previously-duplicated CLOSING_PATTERNS
# / NEGATION_PATTERNS / is_closing_claim (which had drifted from the copy in
# verify-discoverability-on-close.py). MYC-791.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _lib.closing_claim import is_closing_claim  # noqa: E402
except Exception:  # fail-open: if the lib cannot load, never block a close
    def is_closing_claim(_text: str) -> bool:  # type: ignore
        return False

# Shared vault-root resolver - single source of truth (_lib/vault_root.py).
# Repo-aware: a session working inside its own vault-shaped repo (own
# CLAUDE.md declaring a Session End/Close cascade) resolves to that repo,
# not a global VAULT_ROOT default. Must stay in lockstep with
# detect-closing-signal.py's resolution — that hook decides where the model
# writes the session file; this hook must look in the SAME place, or a
# correctly-written artifact false-blocks the close because this hook is
# still checking an unrelated default vault.
try:
    from _lib.vault_root import resolve_vault_root  # noqa: E402
except Exception:  # fail-open: if the lib cannot load, fall back to env/home
    def resolve_vault_root(cwd: Path, env_vault_root: str | None) -> Path:  # type: ignore
        return Path(env_vault_root) if env_vault_root else (cwd or Path.home() / "vault")


def _find_meta_dir(vault_root: Path) -> Path:
    """Deterministically resolve THIS vault's human-memory Meta folder.

    Mirrors hooks/detect-closing-signal.py — decorated "⚙️ Meta" is probed
    BEFORE plain "Meta". Vaults intentionally run two meta folders: "⚙️ Meta"
    (human memory: Sessions/, Decisions/) and plain "Meta" (instinct-engine
    machine memory). A naive `sorted(iterdir())[0]` picks plain "Meta" first
    (the letter M sorts before the emoji codepoint), which would point this
    hook's session-file + runner checks at the wrong folder. The explicit
    decorated-first probe avoids that.
    """
    for candidate_name in ("⚙️ Meta", "Meta"):
        candidate = vault_root / candidate_name
        if candidate.is_dir():
            return candidate
    try:
        for child in sorted(vault_root.iterdir()):
            if child.is_dir() and child.name.endswith("Meta"):
                return child
    except OSError:
        pass
    return vault_root / "Meta"


# Import-time placeholders so the module stays importable without a hook
# payload. main() calls _resolve_vault_context(cwd) immediately after reading
# cwd from stdin, before any gate function runs, rebinding these to the SAME
# repo-aware vault detect-closing-signal.py resolved for this session.
#
# MYC-3529: these used to be seeded from a naive
# `os.environ.get("VAULT_ROOT", str(Path.home() / "vault"))`. The seed was
# always overwritten before use, but it is the exact #375/#404 shape and it
# made the module's import-time answer wrong for every vault not literally
# named "vault" — including for anything that imports this module without
# calling main(). They are now seeded through the SAME sanctioned resolver
# the rebind uses, so the import-time answer and the per-invocation answer
# can never disagree by construction.
VAULT_ROOT: Path
META_DIR: Path
META_NAME: str
SESSIONS_DIR: Path
RUNNER_SCRIPT: Path


def _resolve_vault_context(cwd: str) -> None:
    """Recompute VAULT_ROOT/META_DIR/META_NAME/SESSIONS_DIR/RUNNER_SCRIPT for
    THIS invocation's cwd, repo-aware. Every gate function below reads these
    as module globals, so rebinding here (called once, early in main()) is
    sufficient to put the whole hook in lockstep with the cwd it was invoked
    with — no signature changes needed downstream.
    """
    global VAULT_ROOT, META_DIR, META_NAME, SESSIONS_DIR, RUNNER_SCRIPT
    VAULT_ROOT = resolve_vault_root(Path(cwd) if cwd else Path.cwd(), os.environ.get("VAULT_ROOT"))
    META_DIR = _find_meta_dir(VAULT_ROOT)
    META_NAME = META_DIR.name
    SESSIONS_DIR = META_DIR / "Sessions"
    RUNNER_SCRIPT = META_DIR / "scripts" / "session-close-runner.sh"


# Seed the module globals declared above. Going through _resolve_vault_context
# rather than repeating the expression is the point: one resolution path, so an
# import-time read can never drift from the per-invocation rebind.
_resolve_vault_context("")

# Default is the exact path session-close-runner.sh writes; the env override is
# for hermetic tests (and any setup where both sides agree to relocate it).
# The literal /tmp (NOT tempfile.gettempdir()) is deliberate on POSIX: the bash
# runner writes literally to /tmp, and macOS GUI processes see TMPDIR as
# /var/folders/... — gettempdir() there would look in the wrong place and
# false-block every close. Windows has no /tmp, so use the real temp dir there
# (the bash runner can't run on Windows anyway; this hook stays advisory).
import tempfile
_DEFAULT_RUNNER_REPORT = (
    str(Path(tempfile.gettempdir()) / "abs-session-close-runner.report")
    if os.name == "nt" else "/tmp/abs-session-close-runner.report"
)
RUNNER_REPORT = Path(os.environ.get("ABS_RUNNER_REPORT", _DEFAULT_RUNNER_REPORT))
RUNNER_FRESH_SECONDS = 1800  # 30 minutes


def runner_installed() -> bool:
    """True iff session-close-runner.sh is installed in THIS vault's meta dir.

    This is the fail-safe signal. When the runner is NOT installed, the vault
    never opted into the session-close cascade, so the hook must NOT hard-block
    — a missing runner would otherwise block EVERY close forever (the exact
    failure this fail-safe prevents). Enforcement (hard-block) is gated on this
    returning True; otherwise the hook degrades to a non-blocking advisory.
    """
    return RUNNER_SCRIPT.is_file()

# Closing-claim detection (the pattern lists + the matcher) now lives in the
# shared _lib/closing_claim.py imported at the top, so this hook and
# verify-discoverability-on-close.py share ONE de-drifted source with the
# MENTION-vs-USE guards. MYC-791.


def get_last_assistant_text(transcript_path: str) -> str:
    """Read the last assistant message text from the transcript JSONL."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except Exception:
        return ""
    # Walk backwards to find the most recent assistant message
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message", {})
        content = msg.get("content", [])
        text_parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text_parts.append(c.get("text", ""))
        if text_parts:
            return "\n".join(text_parts)
    return ""


def extract_worktree_slug(cwd: str) -> str:
    """Extract the worktree slug from a cwd like .../worktrees/<slug>/..."""
    m = re.search(r"/worktrees/([^/]+)", cwd or "")
    return m.group(1) if m else ""


def session_file_exists_for_today(worktree_slug: str) -> bool:
    """True iff a session file exists for today (or yesterday, to handle the
    midnight-roll case) matching the worktree slug.

    Yesterday-fallback added 2026-05-13 after funny-golick-fe8400: session
    was authored at 23:59 on 2026-05-12, cascade ran past midnight, hook
    checked for 2026-05-13-dated file only and false-flagged the existing
    file as missing. Sessions span calendar boundaries; the check should too.
    """
    if not SESSIONS_DIR.exists():
        return False
    from datetime import timedelta
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    for date_prefix in (today, yesterday):
        for path in SESSIONS_DIR.glob(f"{date_prefix}*.md"):
            if worktree_slug and worktree_slug in path.name:
                return True
    return False


def _decision_belongs_to_worktree(path_str: str, worktree_slug: str) -> bool:
    """Check decision-file frontmatter for `worktree: <slug>` match.

    Decision filenames are date-only (no worktree slug), so frontmatter is
    the only attribution signal. If frontmatter is missing or unreadable,
    we ERR ON THE SIDE OF NOT BLOCKING — false-positives here block the
    legitimate goodbye of an unrelated session, which is worse than
    missing a real uncommitted artifact.
    """
    try:
        full_path = VAULT_ROOT / path_str
        if not full_path.exists():
            return False
        head = full_path.read_text(encoding="utf-8", errors="replace")[:2000]
    except Exception:
        return False
    m = re.search(r"^worktree:\s*(\S+)\s*$", head, re.MULTILINE)
    if not m:
        return False
    return m.group(1).strip() == worktree_slug


def uncommitted_session_artifacts(worktree_slug: str) -> list[str]:
    """Return paths of uncommitted session-close artifacts owned by THIS
    worktree's session.

    Scoping rules (added 2026-05-13 after the funny-golick gate caught
    parallel-session work):
      - Sessions/: match filename contains today's OR yesterday's date AND
        the worktree slug.
      - Decisions/: filename has no worktree, so read frontmatter
        `worktree: <slug>` and match.
      - Session Captures.md: shared across sessions; flag only if THIS
        worktree also has an uncommitted session file (likely-same-batch
        proxy). This avoids blocking goodbye on another session's append.

    Empty list = clean for this worktree. Non-empty = block.

    Original failure: funny-golick-fe8400 wrote 5 files at session close
    and almost lost them at archive. Permanent-fix-pattern: block at the
    model layer, not the UI layer.
    """
    import subprocess
    from datetime import timedelta
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        result = subprocess.run(
            ["git", "-C", str(VAULT_ROOT), "status", "--short",
             "--", f"{META_NAME}/Sessions/", f"{META_NAME}/Decisions/",
             f"{META_NAME}/Session Captures.md"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []

    sessions_unc: list[str] = []
    decisions_unc: list[str] = []
    captures_unc: list[str] = []
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line or len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if "Session Captures.md" in path:
            captures_unc.append(path)
            continue
        if "/Sessions/" in path or path.startswith(f"{META_NAME}/Sessions/"):
            if (today in path or yesterday in path) and worktree_slug and worktree_slug in path:
                sessions_unc.append(path)
            continue
        if "/Decisions/" in path or path.startswith(f"{META_NAME}/Decisions/"):
            if today in path or yesterday in path:
                if _decision_belongs_to_worktree(path, worktree_slug):
                    decisions_unc.append(path)
            continue

    # Captures only flags when this worktree also has an uncommitted session
    # file (same-batch proxy); otherwise it's another session's append.
    flagged_captures = captures_unc if sessions_unc else []
    return sessions_unc + decisions_unc + flagged_captures


def runner_ran_recently() -> bool:
    """True iff session-close-runner.sh wrote a fresh RUNNER COMPLETE marker.

    The runner writes to /tmp/abs-session-close-runner.report on every run
    and ends with `RUNNER COMPLETE @ <ISO8601-UTC>`. The marker is fresh
    iff timestamp is within RUNNER_FRESH_SECONDS of now (UTC).

    Missing report file, missing marker, or stale marker → False.
    """
    if not RUNNER_REPORT.exists():
        return False
    try:
        text = RUNNER_REPORT.read_text(errors="replace")
    except Exception:
        return False
    m = re.search(r"RUNNER COMPLETE @ (\S+)", text)
    if not m:
        return False
    ts_raw = m.group(1).strip()
    # Accept either trailing Z (zulu) or +00:00 offset
    if ts_raw.endswith("Z"):
        ts_raw = ts_raw[:-1] + "+00:00"
    # Normalize a no-colon UTC offset (e.g. -0500 / +0530) to +05:00 form.
    # session-close-runner.sh stamps the report with `date '+%z'`, which emits
    # the no-colon form, but datetime.fromisoformat() rejects it before Python
    # 3.11 — without this a fresh report parses as stale and spuriously blocks.
    ts_raw = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", ts_raw)
    try:
        ts = datetime.fromisoformat(ts_raw)
    except Exception:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - ts).total_seconds() <= RUNNER_FRESH_SECONDS


def main() -> int:
    if os.environ.get("VERIFY_CASCADE_BYPASS") == "1":
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0  # malformed payload — don't block

    cwd = payload.get("cwd", "")
    transcript_path = payload.get("transcript_path", "")

    worktree_slug = extract_worktree_slug(cwd)
    if not worktree_slug:
        return 0  # not in a worktree — skip

    last_text = get_last_assistant_text(transcript_path)
    if not is_closing_claim(last_text):
        return 0  # no closing claim — skip

    # Repo-aware vault resolution, now that we know this is worth the work:
    # put every gate below in lockstep with whichever vault
    # detect-closing-signal.py resolved for THIS cwd (see _lib/vault_root.py).
    _resolve_vault_context(cwd)

    # Enforcement (hard-block) is conditional on the session-close cascade
    # being INSTALLED in this vault — see runner_installed(). This is what
    # makes the hook safe to wire by default: a vault that never set up the
    # cascade can always close (the "missing runner blocks every close
    # forever" failure is structurally impossible). VERIFY_CASCADE_SOFT=1
    # forces advisory mode even when the runner IS installed.
    enforce = runner_installed() and os.environ.get("VERIFY_CASCADE_SOFT") != "1"

    today = datetime.now().strftime("%Y-%m-%d")
    file_ok = session_file_exists_for_today(worktree_slug)
    uncommitted = uncommitted_session_artifacts(worktree_slug)
    commit_ok = not uncommitted

    if not enforce:
        # Advisory mode: NEVER block. The only signal worth surfacing without
        # the cascade is genuinely-uncommitted session artifacts (lost-work
        # risk). Don't nag about the runner (absent by design here) or the
        # session file (a non-cascade vault legitimately may not author one).
        if uncommitted:
            sample = "\n".join(f"      {p}" for p in uncommitted[:8])
            more = f"\n      ... and {len(uncommitted) - 8} more" if len(uncommitted) > 8 else ""
            print(
                "verify-session-close-cascade (advisory — session-close cascade\n"
                "not installed in this vault, so NOT blocking):\n"
                f"  • {len(uncommitted)} uncommitted session artifact(s) at risk:\n"
                f"{sample}{more}\n"
                f"    Commit them before closing (e.g. vault-safe-commit.sh), or\n"
                f"    install the cascade for automatic handling.",
                file=sys.stderr,
            )
        return 0

    # Enforce mode: all three gates must pass; hard-block on any failure.
    runner_ok = runner_ran_recently()
    if file_ok and runner_ok and commit_ok:
        return 0  # all three gates clear — cascade ran fully

    # Block with diagnostic naming WHICH gate failed
    failures = []
    if not file_ok:
        failures.append(
            f"  • Session file missing at {META_NAME}/Sessions/{today}T*-{worktree_slug}.md\n"
            f"    Author it manually (Phase 2 of session-close.md) before retry."
        )
    if not runner_ok:
        runner_state = "missing" if not RUNNER_REPORT.exists() else "stale (>30min old)"
        failures.append(
            f"  • session-close-runner.sh report is {runner_state}\n"
            f"    Path: {RUNNER_REPORT}\n"
            f"    Run: bash \"{META_NAME}/scripts/session-close-runner.sh\"\n"
            f"    The runner handles Phase 0c-0e + Phase 2 aggregators +\n"
            f"    Phase 2c worktree settle deterministically."
        )
    if not commit_ok:
        sample = "\n".join(f"      {p}" for p in uncommitted[:8])
        more = f"\n      ... and {len(uncommitted) - 8} more" if len(uncommitted) > 8 else ""
        failures.append(
            f"  • Session-close artifacts uncommitted ({len(uncommitted)} files):\n"
            f"{sample}{more}\n"
            f"    Run vault-safe-commit.sh BEFORE the goodbye:\n"
            f"      bash \"{META_NAME}/scripts/vault-safe-commit.sh\" \\\n"
            f"        \"session-close: <worktree> — <one-line summary>\" \\\n"
            f"        \"<path1>\" \"<path2>\" ..."
        )

    msg = (
        f"BLOCKED by verify-session-close-cascade hook.\n\n"
        f"Your last response claims to close the session, but the cascade\n"
        f"did not fully run. Three-gate check (ALL required):\n\n"
        + "\n".join(failures) + "\n\n"
        f"Manual phases (not in runner — still your job): Phase 0b\n"
        f"(incomplete-work gate), Phase 1 (conversation scan + Pending\n"
        f"Signals), Phase 2 (session file authorship), Phase 2b\n"
        f"(vault-safe-commit), Phase 3 (functional audit on public ships).\n\n"
        f"Bypass (use sparingly): VERIFY_CASCADE_BYPASS=1 (skip) or\n"
        f"VERIFY_CASCADE_SOFT=1 (advisory, never block).\n"
    )
    print(msg, file=sys.stderr)
    return 2  # block


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    # A hook that print()s non-ASCII raises UnicodeEncodeError on a cp1252
    # console: the gate then fails silently OPEN, or denies the tool call with
    # no legible cause. Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
