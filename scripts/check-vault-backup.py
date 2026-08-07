#!/usr/bin/env python3
"""Guard: detect whether a vault has ANY off-machine backup at all.

The failure this prevents: a brain in active daily use whose ONLY copy is the
local disk it runs on. One disk failure = everything gone. It is silent — the
vault works perfectly right up until the drive dies — which is exactly why it
needs a loud, repeated signal rather than a doc nobody reads.

A vault counts as backed up if ANY of these holds (cheapest checks first):

  1. vault-backup    — `scripts/vault-backup.sh` is configured for this vault
                       AND a real archive exists in a reachable destination.
  2. Time Machine    — a destination is configured (macOS `tmutil`). The
                       OS-default off-machine path; counts even if a disk is
                       currently unplugged (the strategy exists).
  3. cloud copy      — the vault path resolves inside a consumer cloud-sync
                       root (iCloud / OneDrive / Dropbox / Google Drive / Box).
                       A churny, sub-optimal backup (see docs/CLOUD_SYNC.md and
                       worktree-footprint-signal.py, which owns nagging about
                       that combo) — but it IS an off-machine copy, so it
                       suppresses the no-backup alarm here.
  4. git remote      — the vault is a git repo with a remote AND the current
                       HEAD has been pushed (exists on a remote branch).

This is the SINGLE source of truth for "is there a backup" — the SessionStart
hook (surface-backup-status.py), diagnose.sh/.ps1, and the phase-01 onboarding
gate all route through it so the surfaces never drift.

Usage:
  check-vault-backup.py <vault-path>          # human-readable verdict + remedy
  check-vault-backup.py --porcelain <path>    # one machine-readable line
  check-vault-backup.py --ignore-cloud <path> # don't count a cloud-sync copy as
                                              # a backup (for callers about to
                                              # REMOVE it, e.g. relocate-vault.sh)

Exit codes:
  0  BACKED UP  — at least one off-machine copy/strategy detected
  1  NO BACKUP  — none detected (the one-disk-failure class)
  2  USAGE      — bad arguments

Porcelain first token (parse by splitting on ':'):
  BACKED_UP:vault-backup:<age_days>   — our archive present, <age_days> old
  BACKED_UP:timemachine               — Time Machine destination configured
  BACKED_UP:cloud:<service>           — vault inside a cloud-sync root
  BACKED_UP:git-remote                — vault git HEAD pushed to a remote
  NO_BACKUP                           — nothing detected
  NO_BACKUP:configured-not-run        — vault-backup configured, but no archive
                                        in the destination yet (or dest gone)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# detect_cloud_sync lives in the shared worktree-safety lib. Resolve it whether
# this runs from the repo (../hooks/_lib) or the deployed skill dir. Mirror of
# check-cloud-sync.py's resolution so the two guards stay byte-compatible.
_HERE = Path(__file__).resolve().parent
for _cand in (_HERE.parent / "hooks", _HERE.parent):
    if (_cand / "_lib" / "worktree_safety.py").is_file():
        sys.path.insert(0, str(_cand))
        break
else:
    _dep = Path.home() / ".claude" / "skills" / "ai-brain-starter" / "hooks"
    if (_dep / "_lib" / "worktree_safety.py").is_file():
        sys.path.insert(0, str(_dep))

try:
    from _lib.worktree_safety import detect_cloud_sync
except Exception:  # pragma: no cover - import-path failure shouldn't crash the guard
    def detect_cloud_sync(_p: Path) -> str | None:  # type: ignore[misc]
        return None

# Canonical config written by vault-backup.sh / vault-backup.ps1. Keyed by the
# resolved vault path so one config can track several vaults. Overridable for
# tests via VAULT_BACKUP_CONF.
CONF_PATH = Path(os.environ.get(
    "VAULT_BACKUP_CONF", str(Path.home() / ".claude" / ".vault-backup.conf")
))

# Archive filename stems vault-backup.sh writes (single rotating file per vault).
ARCHIVE_SUFFIXES = (".tar.gz", ".tar.gz.gpg", ".tar.gz.enc", ".zip", ".zip.gpg")


def _now() -> float:
    import time
    return time.time()


def _read_conf() -> dict:
    try:
        # utf-8-sig: tolerate a UTF-8 BOM. Windows PowerShell 5.1's
        # Set-Content -Encoding UTF8 (used by vault-backup.ps1) prepends one;
        # plain read_text() would raise "Unexpected UTF-8 BOM" (a ValueError),
        # silently zeroing the config and mis-reporting a real backup as absent.
        return json.loads(CONF_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _archive_age_days(dest: Path, stem: str) -> float | None:
    """Newest matching archive's age in days, or None if dest has no archive."""
    if not dest.is_dir():
        return None
    newest: float | None = None
    try:
        for child in dest.iterdir():
            if not child.is_file():
                continue
            name = child.name
            if not name.startswith(stem):
                continue
            if not any(name.endswith(sfx) for sfx in ARCHIVE_SUFFIXES):
                continue
            try:
                mt = child.stat().st_mtime
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
    except OSError:
        return None
    if newest is None:
        return None
    return max(0.0, (_now() - newest) / 86400.0)


def check_vault_backup(conf_entry: dict | None) -> float | None:
    """Age (days) of the newest vault-backup archive, or None if no real backup.

    conf_entry is the per-vault dict from the config (dest / stem). A configured
    entry whose destination is gone or empty returns None (treated as
    configured-not-run by the caller, NOT as backed up).
    """
    if not conf_entry:
        return None
    dest = conf_entry.get("dest")
    if not dest:
        return None
    stem = conf_entry.get("archive_stem") or "vault-backup"
    return _archive_age_days(Path(dest).expanduser(), stem)


def check_time_machine() -> bool:
    """True if a Time Machine destination is configured (macOS only)."""
    # Test/override hook: lets the negative-control suite assert NO_BACKUP
    # deterministically on a dev Mac that genuinely has Time Machine set up
    # (TM is machine-wide, so without this the bare-vault case would flake).
    if os.environ.get("VAULT_BACKUP_SKIP_TIMEMACHINE") == "1":
        return False
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.run(
            ["tmutil", "destinationinfo"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if out.returncode != 0:
        return False
    text = out.stdout or ""
    if "No destinations configured" in text:
        return False
    # A configured destination prints Name / ID / Kind / Mount Point lines.
    return any(
        line.strip().split(":", 1)[0].strip() in {"Name", "ID", "Kind", "Mount Point"}
        for line in text.splitlines()
    )


def check_git_remote_pushed(vault: Path) -> bool:
    """True if the vault is a git repo with a remote AND HEAD is pushed."""
    if not (vault / ".git").exists():
        return False
    try:
        remotes = subprocess.run(
            ["git", "-C", str(vault), "remote"],
            capture_output=True, text=True, timeout=10,
        )
        if remotes.returncode != 0 or not remotes.stdout.strip():
            return False
        # HEAD reachable from any remote-tracking branch == it has been pushed.
        contains = subprocess.run(
            ["git", "-C", str(vault), "branch", "-r", "--contains", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        return contains.returncode == 0 and bool(contains.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def detect(vault: Path, conf_keys: list[str] | None = None,
           ignore_cloud: bool = False) -> tuple[str, str]:
    """Return (porcelain_token, human_remedy). Cheapest checks first.

    conf_keys: candidate path strings to look up in the config (the as-given and
    the resolved form). Matching on both makes the lookup robust when the vault
    path contains a symlink (e.g. macOS /var -> /private/var), so the detector
    and vault-backup.sh agree on the key regardless of which form was stored.

    ignore_cloud: when True, a vault that lives inside a cloud-sync root does NOT
    count as backed up. Used by callers that are about to REMOVE the cloud copy
    (relocate-vault.sh moves the vault out and leaves a symlink — the sync daemon
    then follows a few-byte symlink, not the tree, so the cloud copy is gone post
    -move). For those callers the only backups that count are the ones that
    survive the move (a vault-backup archive, Time Machine, or a pushed git
    remote). Default False so the SessionStart/diagnose/onboarding consumers,
    which are NOT removing the cloud copy, still treat it as a real off-machine
    copy.
    """
    conf = _read_conf()
    vaults = conf.get("vaults") or {}
    keys = list(conf_keys or [str(vault)])
    entry = next((vaults[k] for k in keys if k in vaults), None)

    age = check_vault_backup(entry)
    if age is not None:
        return (f"BACKED_UP:vault-backup:{age:.1f}",
                f"vault-backup archive present (~{age:.1f} days old).")

    if check_time_machine():
        return ("BACKED_UP:timemachine",
                "Time Machine destination configured.")

    if not ignore_cloud:
        service = detect_cloud_sync(vault)
        if service:
            return (f"BACKED_UP:cloud:{service}",
                    f"vault is inside {service} (an off-machine cloud copy — churny "
                    f"but real; see docs/CLOUD_SYNC.md for the safer single-file pattern).")

    if check_git_remote_pushed(vault):
        return ("BACKED_UP:git-remote",
                "vault git HEAD is pushed to a remote.")

    if entry:
        # Configured but no archive landed yet (or destination unreachable).
        return ("NO_BACKUP:configured-not-run",
                "backup is configured but no snapshot exists yet (or the "
                "destination is unreachable). Run `vault-backup.sh run`.")

    return ("NO_BACKUP", "no off-machine backup of any kind detected.")


def main(argv: list[str]) -> int:
    porcelain = "--porcelain" in argv
    ignore_cloud = "--ignore-cloud" in argv
    args = [a for a in argv if a not in ("--porcelain", "--ignore-cloud")]
    if len(args) != 1:
        print("usage: check-vault-backup.py [--porcelain] [--ignore-cloud] <vault-path>",
              file=sys.stderr)
        return 2

    raw = Path(args[0]).expanduser()
    try:
        vault = raw.resolve()
    except OSError:
        vault = raw

    # Look up the conf by both the as-given and resolved path (symlink-robust).
    conf_keys = list(dict.fromkeys([str(vault), str(raw)]))
    token, remedy = detect(vault, conf_keys, ignore_cloud=ignore_cloud)
    backed_up = token.startswith("BACKED_UP")

    if porcelain:
        print(token)
        return 0 if backed_up else 1

    if backed_up:
        print(f"OK    {vault} has an off-machine backup: {remedy}")
        return 0

    print(f"FAIL  {vault} has NO off-machine backup.")
    print(f"      {remedy}")
    print(f"      One disk failure = everything gone. Set one up (one command):")
    print(f"      bash ~/.claude/skills/ai-brain-starter/scripts/vault-backup.sh setup")
    return 1


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
