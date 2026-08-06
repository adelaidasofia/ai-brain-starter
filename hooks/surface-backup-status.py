#!/usr/bin/env python3
"""Surface vault off-machine-backup status at SessionStart.

The failure this exists for: a brain in active daily use whose only copy is the
local disk it runs on. It works perfectly right up until the drive dies — silent
until catastrophic — so it needs a LOUD, REPEATED signal, not a doc. A real
incident: a 1,100-note brain with no iCloud, no Time Machine, no git remote, one
disk. One failure from total loss, and nothing in the product ever said so.

This hook routes through the single source of truth (scripts/check-vault-backup.py)
and reacts:

  * NO off-machine backup at all -> a loud line, EVERY session, until one exists.
    Distinct from "stale": this is "you have zero copies", the worst case.
  * backup configured but never run / destination unreachable -> run-it nudge.
  * our backup present but STALE (older than VAULT_BACKUP_STALE_DAYS, default 3)
    -> run-it nudge.
  * our backup present but a restore was NEVER verified -> verify-it nudge
    (a backup you have never restored is a hope, not a backup).
  * a real copy exists (fresh vault-backup / Time Machine / cloud / git remote)
    -> silent. The cloud-sync churn case is owned by worktree-footprint-signal.py,
    so this hook does not double-warn about it.

Advisory only: it NEVER blocks. Bypass: VAULT_BACKUP_BYPASS=1.

WIRING (SessionStart):
  {"hooks": [{"type": "command",
    "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/surface-backup-status.py 2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'"
  }]}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))

try:
    from _lib.worktree_safety import find_main_repo
except Exception:  # pragma: no cover - fail open if the lib can't load
    find_main_repo = None  # type: ignore[assignment]

DETECTOR = HOOK_DIR.parent / "scripts" / "check-vault-backup.py"
CONF_PATH = Path(os.environ.get(
    "VAULT_BACKUP_CONF", str(Path.home() / ".claude" / ".vault-backup.conf")
))
DEFAULT_STALE_DAYS = 3.0
VERIFY_STALE_DAYS = 30.0

# Platform-appropriate commands — on Windows the bash form is a dead end, so
# point at vault-backup.ps1 (same setup/run/verify subcommands) by absolute
# path (no ~ / $HOME / %USERPROFILE%: none expands in every Windows shell).
if os.name == "nt":
    _PS1 = Path.home() / ".claude" / "skills" / "ai-brain-starter" / "scripts" / "vault-backup.ps1"
    _BACKUP_PREFIX = f'powershell -ExecutionPolicy Bypass -File "{_PS1}"'
else:
    _BACKUP_PREFIX = "bash ~/.claude/skills/ai-brain-starter/scripts/vault-backup.sh"
SETUP_CMD = f"{_BACKUP_PREFIX} setup"
RUN_CMD = f"{_BACKUP_PREFIX} run"
VERIFY_CMD = f"{_BACKUP_PREFIX} verify"
# The stale-snapshot message used to tell the user to "check the daily schedule
# is still installed" — handing the human the exact check the product can now
# make AND repair itself. A dead schedule is the single most likely reason
# snapshots went stale, so name the command that fixes it. (MYC-3528)
SCHEDULE_CMD = f"{_BACKUP_PREFIX} schedule"


def _emit(ctx: str | None) -> int:
    if ctx:
        print(json.dumps({"continue": True, "additionalContext": ctx}))
    else:
        print(json.dumps({"continue": True, "suppressOutput": True}))
    return 0


def _is_brain(repo: Path) -> bool:
    """Only nag for an actual brain vault, not every code repo with worktrees."""
    if (repo / "CLAUDE.md").is_file():
        return True
    try:
        return any(c.name.endswith("Meta") and c.is_dir() for c in repo.iterdir())
    except OSError:
        return False


def _porcelain(repo: Path) -> str | None:
    if not DETECTOR.is_file():
        return None
    try:
        out = subprocess.run(
            [sys.executable, str(DETECTOR), "--porcelain", str(repo)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (out.stdout or "").strip().splitlines()[0] if out.stdout.strip() else None


def _verify_age_days(repo: Path) -> float | None:
    """Days since the last verified restore for this vault, or None if never."""
    try:
        # utf-8-sig: tolerate a UTF-8 BOM. vault-backup.ps1 writes this config
        # via Windows PowerShell 5.1 Set-Content -Encoding UTF8, which prepends
        # one; plain read_text() raises "Unexpected UTF-8 BOM" (a ValueError),
        # zeroing verify-age so a verified backup reads as never-verified. The
        # sibling reader (check-vault-backup.py _read_conf) was fixed in #301;
        # this direct reader is the same file's second consumer.
        conf = json.loads(CONF_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    entry = (conf.get("vaults") or {}).get(str(repo))
    if not entry:
        # try the resolved form too
        try:
            entry = (conf.get("vaults") or {}).get(str(repo.resolve()))
        except OSError:
            entry = None
    if not entry:
        return None
    last = entry.get("last_verify")
    if not last:
        return None
    try:
        dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def main() -> int:
    if os.environ.get("VAULT_BACKUP_BYPASS") == "1":
        return _emit(None)
    if find_main_repo is None:
        return _emit(None)

    repo = find_main_repo()
    if repo is None or not _is_brain(repo):
        return _emit(None)

    token = _porcelain(repo)
    if not token:
        return _emit(None)

    try:
        stale_days = float(os.environ.get("VAULT_BACKUP_STALE_DAYS", DEFAULT_STALE_DAYS))
    except ValueError:
        stale_days = DEFAULT_STALE_DAYS

    # --- the worst case: zero off-machine copies. Loud, every session. ---
    if token == "NO_BACKUP":
        return _emit(
            "⚠️  [backup] Your brain has NO off-machine backup. It lives on one "
            "disk — one failure and everything is gone (notes, journals, the lot). "
            "This warning repeats every session until a backup exists. Fix it now, "
            f"one command:\n      {SETUP_CMD}\n"
            "Picks a destination you already have (an external disk or a cloud "
            "folder), writes one compressed daily snapshot (no sync-storm), "
            "encrypts if you ask. See docs/BACKUP.md."
        )

    if token == "NO_BACKUP:configured-not-run":
        return _emit(
            "⚠️  [backup] Backup is configured but no snapshot exists yet (or the "
            "destination is unreachable — external disk unplugged?). You are not "
            f"protected until one lands. Run it:\n      {RUN_CMD}"
        )

    # --- our archive exists: check freshness + that a restore was ever verified ---
    if token.startswith("BACKED_UP:vault-backup:"):
        try:
            age = float(token.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            age = 0.0
        if age > stale_days:
            return _emit(
                f"[backup] Last vault snapshot was {age:.1f} days ago "
                f"(> {stale_days:.0f}d). Take one now: `{RUN_CMD}`. The usual "
                f"cause is the daily schedule no longer being loaded by the OS. "
                f"This checks it and repairs it:\n      {SCHEDULE_CMD}"
            )
        v_age = _verify_age_days(repo)
        if v_age is None:
            return _emit(
                "[backup] Snapshots are fresh, but you have never verified a "
                "restore. A backup you have never restored is a hope, not a "
                f"backup. Prove it once:\n      {VERIFY_CMD}"
            )
        if v_age > VERIFY_STALE_DAYS:
            return _emit(
                f"[backup] Snapshots are fresh, but the last verified restore was "
                f"{v_age:.0f} days ago. Re-verify: `{VERIFY_CMD}`."
            )
        return _emit(None)

    # BACKED_UP:timemachine / cloud:* / git-remote -> a real copy exists. Silent.
    # (worktree-footprint-signal.py owns the cloud-sync-churn nag, so no overlap.)
    return _emit(None)


if __name__ == "__main__":
    # UTF-8 console guard (the ai-brain-starter#313 cp1252 crash class). This hook
    # prints emoji in its loudest message, so on a Windows console in a cp1252
    # codepage the write itself raises and the backup warning is LOST — the one
    # message that must never be the thing that goes quiet. This file previously
    # sat in scripts/utf8-stdout-baseline.txt as a legacy exemption; the row is
    # removed rather than re-pinned, per that baseline's own instruction.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({"continue": True, "suppressOutput": True}))
        sys.exit(0)
