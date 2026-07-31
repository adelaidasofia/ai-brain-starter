#!/usr/bin/env python3
"""Auto-prep worktree before Claude Code's archive prompt scans git status.

Permanent fix for the false-alarm archive warning that flagged 7+
hookify files as "permanent loss" when they were byte-identical to
main.

Mechanism: registered as a `Stop` hook in ~/.claude/settings.json.
Fires after every assistant turn. Detects whether the current working
directory is inside a vault worktree
(`<vault>/.claude/worktrees/<slug>/`); if so, invokes the existing
`worktree-archive-prep.py` script from the main vault path, which
removes byte-identical untracked duplicates and clears byte-identical
modified-file entries.

Idempotent — short-circuits silently when there's nothing to clean.
No-op when not in a worktree (most sessions).

Why a Stop hook (not SessionEnd):
- Stop fires on every assistant response, so the worktree stays
  clean continuously throughout the session.
- The archive prompt the harness shows when the user clicks
  "archive worktree" scans `git status` in real time. If we only
  cleaned at SessionEnd, there'd be a window where the user could
  click archive between turns and still see the false alarm.
- Stop fires often, so the script must short-circuit fast.
  worktree-archive-prep.py exits 0 silently when 0 untracked + 0
  modified, so the cost is one git status call per assistant turn.

Bypass: WORKTREE_AUTOPREP_BYPASS=1 in env if the prep itself misbehaves.

Codified 2026-05-09 after the false alarm fired across multiple
sessions despite Phase 2c (worktree-archive-prep) running at session
close. The Phase 2c run cleared the duplicates, then the worktree's
git status got recomputed (session resume / branch sync) and the same
false alarm re-fired. Stop-hook continuous cleanup closes that gap.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _lib.vault_root import vault_root_for  # noqa: E402
except Exception:  # fail-open: a Stop hook must never break the turn
    def vault_root_for(target: Path):  # type: ignore
        return None

# MYC-3529 — the MAIN vault is derived from the WORKTREE WE ARE IN, not from
# $VAULT_ROOT. This hook only ever runs inside `<vault>/.claude/worktrees/<slug>/`,
# so the cwd already names the vault unambiguously; vault_root_for collapses the
# worktree segment and confirms the result is really a vault. The old code bound
# `MAIN_VAULT = os.environ.get("VAULT_ROOT", str(Path.home() / "vault"))` at
# import: UNSET it looked for the prep script under ~/vault, found nothing, and
# returned 0 silently on every install whose vault is not named "vault" — the
# false-alarm archive warning this hook exists to kill fired anyway, with no
# signal that the fix was inert. SET, a worktree of a SECOND vault ran the FIRST
# vault's prep script against it (or, more often, silently skipped).


def _prep_script_for(cwd: Path) -> Path | None:
    """The main vault's worktree-archive-prep.py for the vault owning `cwd`.

    Must resolve to the MAIN vault path: the worktree checkout won't have the
    script at the same path, because the worktree is a sparse checkout of the
    same git repo.
    """
    vault = vault_root_for(cwd)
    if vault is None:
        return None
    return vault / "⚙️ Meta/scripts/worktree-archive-prep.py"


def main() -> int:
    if os.environ.get("WORKTREE_AUTOPREP_BYPASS") == "1":
        return 0

    cwd = Path.cwd().resolve()
    cwd_str = str(cwd).replace("\\", "/")

    # Only run when actually inside a worktree.
    if "/.claude/worktrees/" not in cwd_str:
        return 0

    prep_script = _prep_script_for(cwd)
    if prep_script is None:
        # No vault owns this worktree (and no $VAULT_ROOT fallback) — nothing
        # to run. Silent no-op, same as a missing prep script.
        return 0

    if not prep_script.exists():
        # Prep script missing — silent no-op. Don't break Stop hook
        # chain on a transient state.
        return 0

    # Run the prep script from the worktree cwd (it reads `git
    # status` from cwd to decide what to clean).
    # Use sys.executable (the python that launched this hook) instead of
    # bare "python3" so PATH shims (e.g. trailofbits/modern-python's
    # uv-nudge shim installed 2026-05-09) don't silently 1-out the call.
    try:
        result = subprocess.run(
            [sys.executable, str(prep_script)],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # Hook never blocks the assistant turn. Silent on transient
        # failure; the next Stop fires fresh.
        return 0

    # Surface output ONLY when the prep actually did something
    # (exit 0 + non-empty stdout). Silent otherwise so the hook
    # doesn't fill the transcript with no-op messages.
    if result.returncode == 0 and result.stdout.strip():
        # Send to stderr so it shows in the harness log without
        # polluting the conversation transcript.
        print(
            f"[worktree-archive-autoprep] {result.stdout.strip()}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
