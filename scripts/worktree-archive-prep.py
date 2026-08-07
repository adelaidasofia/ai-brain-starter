#!/usr/bin/env python3
"""
worktree-archive-prep.py — silence the worktree-archive false-alarm prompt.

Problem: Claude Code's worktree archive prompt scans `git status` from inside
the worktree and flags every `??` (untracked) file as "permanent loss" if you
archive without committing. This is true for files that exist ONLY in the
worktree filesystem. It is FALSE for files that have a byte-identical copy
already committed on main — the worktree copy is a redundant filesystem
duplicate, the master commit holds the real content, the archive prompt is
crying wolf.

Recurring failure mode: the false-alarm trains the operator to distrust the
system. Permanent-fix-pattern hooks (PostToolUse auto-commit, session-close
sweep) successfully commit the work to master but do NOT clean up the
worktree filesystem duplicate, so the prompt keeps firing.

Fix: before archive, walk the worktree's `git status --porcelain`. For each
untracked file:
  - If a byte-identical copy exists at the main-vault path → remove the
    worktree duplicate (redundant).
  - If the master copy differs OR doesn't exist → leave alone, surface to
    operator as REAL pending work that needs a decision.
After this runs, the archive prompt sees only real pending files (or
nothing). The wolf-crying stops.

Usage:
  # From inside a worktree, with VAULT_ROOT set to the main vault
  VAULT_ROOT=/path/to/vault python3 worktree-archive-prep.py

  # Explicit
  python3 worktree-archive-prep.py --worktree /path/to/worktree --main /path/to/main

  # Dry-run (show what would be removed, don't actually delete)
  python3 worktree-archive-prep.py --dry-run

Exit codes:
  0 — clean, archive is safe to proceed
  1 — REAL pending files surfaced, operator decision needed
  2 — error (not in a worktree, git failed, etc.)

Wire into session-close as a pre-archive phase. Or run manually before
clicking the archive prompt.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MAIN_VAULT = Path(os.environ.get("VAULT_ROOT", str(_SCRIPT_DIR.parent)))


def sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def find_worktree_root() -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
        return Path(out) if out else None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def is_worktree(path: Path) -> bool:
    return ".claude/worktrees/" in str(path.resolve())


def list_untracked(worktree: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain"],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        print(f"git status failed: {exc.stderr}", file=sys.stderr)
        return []
    untracked: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("?? "):
            rel = line[3:]
            if rel.startswith('"') and rel.endswith('"'):
                rel = rel[1:-1].encode().decode("unicode_escape")
            untracked.append(rel)
    return untracked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1] if __doc__ else "")
    ap.add_argument("--worktree", type=Path, help="worktree path (default: cwd's git toplevel)")
    ap.add_argument("--main", type=Path, default=DEFAULT_MAIN_VAULT, help="main vault path (default: VAULT_ROOT env or script's parent)")
    ap.add_argument("--dry-run", action="store_true", help="report what would be removed, do not delete")
    ap.add_argument("--quiet", action="store_true", help="only print errors and the final exit code")
    args = ap.parse_args()

    worktree = args.worktree or find_worktree_root()
    if not worktree:
        print("not in a git working tree", file=sys.stderr)
        return 2
    if not is_worktree(worktree):
        if not args.quiet:
            print(f"not a worktree (no /.claude/worktrees/ in path): {worktree}")
        return 0
    if not args.main.exists():
        print(f"main vault path does not exist: {args.main}", file=sys.stderr)
        return 2

    untracked = list_untracked(worktree)
    if not untracked:
        if not args.quiet:
            print(f"✓ {worktree.name}: 0 untracked files. Archive is clean.")
        return 0

    duplicates: list[str] = []
    real_pending: list[tuple[str, str]] = []

    for rel in untracked:
        wt_path = worktree / rel
        main_path = args.main / rel
        if not wt_path.is_file():
            real_pending.append((rel, "worktree path is not a file (dir or symlink)"))
            continue
        if not main_path.exists():
            real_pending.append((rel, "no master copy"))
            continue
        wt_hash = sha256(wt_path)
        main_hash = sha256(main_path)
        if wt_hash is None or main_hash is None:
            real_pending.append((rel, "hash failed"))
            continue
        if wt_hash == main_hash:
            duplicates.append(rel)
        else:
            real_pending.append((rel, f"content differs from master ({wt_hash[:8]} vs {main_hash[:8]})"))

    if duplicates:
        verb = "would remove" if args.dry_run else "removing"
        if not args.quiet:
            print(f"{verb} {len(duplicates)} duplicate worktree file(s) (byte-identical to master):")
        for rel in duplicates:
            if not args.quiet:
                print(f"  - {rel}")
            if not args.dry_run:
                try:
                    (worktree / rel).unlink()
                except OSError as e:
                    print(f"  failed to remove {rel}: {e}", file=sys.stderr)
                    real_pending.append((rel, f"removal failed: {e}"))

    if real_pending:
        print()
        print(f"✗ {len(real_pending)} REAL pending file(s) — these are NOT duplicates, archive will lose them:")
        for rel, reason in real_pending:
            print(f"  - {rel}  [{reason}]")
        print()
        print("Decide before archiving:")
        print("  - Commit them with your safe-commit wrapper if they should survive")
        print("  - Delete them manually if they're disposable")
        print("  - Leave the worktree open and finish the work")
        return 1

    if not args.quiet:
        print(f"✓ {worktree.name}: {len(duplicates)} duplicate(s) cleared. Archive is safe.")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
