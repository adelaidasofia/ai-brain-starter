#!/usr/bin/env python3
"""Make Claude Code's per-project memory physically live inside the vault.

THE PROBLEM THIS FIXES
----------------------
Claude Code writes its persistent memory (``MEMORY.md`` + topic files) to
``~/.claude/projects/<encoded-vault>/memory/`` — a per-account, per-machine,
tool-specific directory that is invisible inside Obsidian. For a product whose
entire premise is "your second brain lives in your vault", memory stranded in
``~/.claude/`` is a broken promise: it does not show up in the vault, it is not
in the vault's git history, and it does not survive switching machines or tools.

The substrate already assumed a symlink from that dir into the vault
(``instinct_lib.py`` looks for it, ``relocate-vault.sh`` re-homes it, tests
check it) — but nothing in the install ever CREATED it. This script is that
missing step.

WHAT IT DOES (idempotent, loss-free, never deletes)
---------------------------------------------------
1. Ensures ``<vault>/⚙️ Meta/Agent Memory/`` exists (the vault-side home).
2. Resolves the Claude Code project-memory path for this vault.
3. If that path is a real directory with content, MIGRATES every file into the
   vault dir without loss (same-name-but-different files are kept as
   ``<name>.from-tooldir``), then backs the directory up to
   ``memory.pre-link-backup[-N]`` — never deletes — and replaces it with a
   symlink.
4. If it is missing, creates the symlink directly.
5. If it is already the correct symlink, no-ops.
6. Verifies the end state and FAILS LOUD if the symlink does not resolve to the
   vault dir — a wrong-key silent no-op is the worst outcome.

After this runs, everything Claude Code "remembers" is a file in the user's
vault, visible in Obsidian and tracked by the vault's git.

Usage:
    python3 scripts/link-agent-memory.py --vault "/path/to/vault"
    python3 scripts/link-agent-memory.py --vault "/path/to/vault" --dry-run
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _project_key import project_dir_for  # noqa: E402

AGENT_MEMORY_RELPATH = ("⚙️ Meta", "Agent Memory")


def _say(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg)


def _is_link(p: Path) -> bool:
    """True for a symlink OR a Windows directory junction.

    Path.is_symlink() is False for a junction, so every is_symlink() check here
    would misread a correctly-junctioned Windows install as an ordinary
    directory — re-running would then "migrate" the vault's own files into
    themselves and rename the link out of the way. Junctions are reparse
    points, so ask the filesystem attribute directly.
    """
    try:
        if p.is_symlink():
            return True
        if os.name != "nt":
            return False
        import stat as _stat
        attrs = getattr(os.lstat(p), "st_file_attributes", 0)
        return bool(attrs & _stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, ValueError, AttributeError):
        return False


def _same_target(link: Path, target: Path) -> bool:
    try:
        return _is_link(link) and Path(os.path.realpath(link)) == Path(os.path.realpath(target))
    except OSError:
        return False


def _link_dir(link: Path, target: Path) -> str:
    """Point `link` at directory `target`. Returns the mechanism used.

    Windows refuses os.symlink() with WinError 1314 ("a required privilege is
    not held by the client") unless the process is elevated or Developer Mode
    is on — neither is true for a normal user double-clicking an installer. The
    memory link is the step that makes "your brain lives in your vault" true,
    so failing there strands every future memory in ~/.claude, invisible in
    Obsidian. Reported from a Windows 11 install.

    A directory JUNCTION needs no privilege, is resolved by os.path.realpath(),
    and reads/writes through transparently — everything this link is for. It is
    local-volume and directory-only, which is exactly this use case. Symlink is
    still preferred (it works on POSIX and on a Developer Mode box, and it
    survives a target that does not exist yet); the junction is the fallback.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return "symlink"
    except OSError:
        if os.name != "nt":
            raise
        import subprocess
        # cmd.exe writes localized output in the OEM code page; we only care
        # about the exit status, and _same_target verifies the real outcome.
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0 or not _is_link(link):
            raise SystemExit(
                f"link-agent-memory: could not link {link} -> {target}.\n"
                f"  os.symlink needs admin or Developer Mode on Windows, and the\n"
                f"  junction fallback (mklink /J) also failed"
                + (f": {proc.stdout.strip() or proc.stderr.strip()}" if (proc.stdout or proc.stderr) else "")
                + "\n  Without this link your Claude Code memory stays in ~/.claude "
                  "instead of your vault.\n"
                  "  Fix: enable Developer Mode (Settings > System > For developers), "
                  "or run\n"
                  f'    cmd /c mklink /J "{link}" "{target}"\n'
                  "  from an Administrator prompt, then re-run this script."
            )
        return "junction"


def _migrate_contents(src_dir: Path, dst_dir: Path, *, dry_run: bool, quiet: bool) -> int:
    """Copy every file from src into dst without loss. Returns files migrated."""
    moved = 0
    for item in sorted(src_dir.rglob("*")):
        # Only migrate real files. Skip symlinks so a stray link in the source
        # never copies its target's content into the vault (defense-in-depth —
        # the source is Claude Code's own memory, but be strict anyway).
        if item.is_symlink() or not item.is_file():
            continue
        rel = item.relative_to(src_dir)
        dest = dst_dir / rel
        if dest.exists():
            if dest.read_bytes() == item.read_bytes():
                continue  # identical — nothing to do
            dest = dest.with_name(dest.name + ".from-tooldir")  # conflict — keep both
        if dry_run:
            _say(f"  would migrate: {rel} -> {dest.relative_to(dst_dir.parent)}", quiet)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
        moved += 1
    return moved


def _backup_path(mem: Path) -> Path:
    base = mem.with_name("memory.pre-link-backup")
    if not base.exists():
        return base
    n = 2
    while base.with_name(f"memory.pre-link-backup-{n}").exists():
        n += 1
    return base.with_name(f"memory.pre-link-backup-{n}")


def link_agent_memory(vault: str, *, dry_run: bool = False, quiet: bool = False) -> Path:
    vault_path = Path(os.path.abspath(os.path.expanduser(vault)))
    if not vault_path.is_dir():
        raise SystemExit(f"link-agent-memory: vault is not a directory: {vault_path}")
    if not (vault_path / ".obsidian").exists():
        _say(f"  note: {vault_path} has no .obsidian/ — linking anyway", quiet)

    agent_mem = vault_path.joinpath(*AGENT_MEMORY_RELPATH)
    if dry_run:
        _say(f"  would ensure vault memory home: {agent_mem}", quiet)
    else:
        agent_mem.mkdir(parents=True, exist_ok=True)

    proj_dir = project_dir_for(vault_path)
    mem = proj_dir / "memory"

    # Already correctly linked -> nothing to do.
    if _same_target(mem, agent_mem):
        _say(f"✓ already linked: {mem} -> {agent_mem}", quiet)
        return mem

    # A link pointing somewhere ELSE -> do not clobber; fail loud. Covers a
    # Windows junction too, which is not a symlink as far as Python is concerned.
    if _is_link(mem):
        current = os.path.realpath(mem)
        raise SystemExit(
            f"link-agent-memory: {mem} is already a link to {current}, not the vault's "
            f"Agent Memory ({agent_mem}). Refusing to clobber. Resolve by hand."
        )

    # A real directory -> migrate contents into the vault, back it up, replace.
    if mem.exists():
        moved = _migrate_contents(mem, agent_mem, dry_run=dry_run, quiet=quiet)
        backup = _backup_path(mem)
        if dry_run:
            _say(f"  would migrate {moved} file(s), back up {mem} -> {backup}, then symlink", quiet)
            _say(f"  would link: {mem} -> {agent_mem}", quiet)
            return mem
        mem.rename(backup)
        _say(f"  migrated {moved} file(s) into the vault; backed up old dir -> {backup.name}", quiet)
    elif dry_run:
        _say(f"  would link: {mem} -> {agent_mem}", quiet)
        return mem

    proj_dir.mkdir(parents=True, exist_ok=True)
    how = _link_dir(mem, agent_mem)

    # Verify — a wrong-key / failed link is a silent brain-loss bug. Fail loud.
    if not _same_target(mem, agent_mem):
        raise SystemExit(
            f"link-agent-memory: VERIFY FAILED — {mem} does not resolve to {agent_mem}. "
            f"Memory would NOT reach the vault. Aborting."
        )
    _say(f"✓ linked ({how}): {mem} -> {agent_mem}", quiet)
    _say("  Claude Code's memory now lives in your vault (visible in Obsidian, tracked by git).", quiet)
    return mem


def main() -> int:
    ap = argparse.ArgumentParser(description="Symlink Claude Code project memory into the vault.")
    ap.add_argument("--vault", required=True, help="Absolute path to the Obsidian vault.")
    ap.add_argument("--dry-run", action="store_true", help="Show what would happen; change nothing.")
    ap.add_argument("--quiet", action="store_true", help="Only print on error.")
    args = ap.parse_args()
    link_agent_memory(args.vault, dry_run=args.dry_run, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
