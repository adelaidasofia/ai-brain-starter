#!/usr/bin/env python3
"""Rotate graphify backups and clean stale .bak files.

1. Finds all graph.json.backup_* files in graphify-out/
2. Keeps the N most recent (by mtime), deletes the rest
3. Removes .bak files in the Meta folder older than N days

Usage:
  python3 rotate_graphify_backups.py --vault-root /path/to/vault
  python3 rotate_graphify_backups.py --vault-root /path/to/vault --keep 5 --bak-max-age 14
"""
# exit-contract: NOT-A-CHECKER -- deletes old graphify backups; rotation work


import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir as _find_meta_dir_helper  # noqa: E402

DEFAULT_KEEP = 3
DEFAULT_BAK_MAX_AGE_DAYS = 7


def find_meta_dir(vault_root: Path) -> Path:
    return _find_meta_dir_helper(vault_root, prefer_subfolders=("graphify-out", "Decisions")) \
        or (vault_root / "Meta")


def rotate_graphify(vault_root: Path, keep: int) -> list[str]:
    """Delete old graphify backups, keep the newest `keep`. Returns deleted filenames."""
    out_dir = vault_root / "graphify-out"
    if not out_dir.exists():
        print("graphify-out/ directory not found, skipping.")
        return []

    backups = list(out_dir.glob("graph.json.backup_*"))
    if not backups:
        print("No graphify backups found.")
        return []

    backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    to_keep = backups[:keep]
    to_delete = backups[keep:]

    print(f"Found {len(backups)} graphify backups.")
    print(f"Keeping {len(to_keep)} most recent:")
    for p in to_keep:
        print(f"  [keep] {p.name}")

    deleted = []
    for p in to_delete:
        print(f"  [delete] {p.name}")
        p.unlink()
        deleted.append(p.name)

    if not deleted:
        print("Nothing to delete, already at target count.")

    return deleted


def clean_meta_bak(vault_root: Path, max_age_days: int) -> list[str]:
    """Remove .bak files in Meta folder older than max_age_days."""
    meta_dir = find_meta_dir(vault_root)
    if not meta_dir.exists():
        print("Meta directory not found, skipping .bak cleanup.")
        return []

    cutoff = time.time() - max_age_days * 86400
    deleted = []

    for pattern in ["*.bak", "*.bak-*", "*backup_*"]:
        for f in meta_dir.rglob(pattern):
            if f.is_file() and f.stat().st_mtime < cutoff:
                age_days = int((time.time() - f.stat().st_mtime) / 86400)
                print(f"  [delete] {f.relative_to(vault_root)} ({age_days} days old)")
                f.unlink()
                deleted.append(str(f.relative_to(vault_root)))

    if not deleted:
        print(f"No stale .bak files in Meta folder (threshold: {max_age_days} days).")

    return deleted


def main():
    parser = argparse.ArgumentParser(
        description="Rotate graphify backups and clean stale .bak files"
    )
    parser.add_argument("--vault-root", type=Path, required=True,
                        help="Path to the Obsidian vault root")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help=f"Number of graphify backups to keep (default: {DEFAULT_KEEP})")
    parser.add_argument("--bak-max-age", type=int, default=DEFAULT_BAK_MAX_AGE_DAYS,
                        help=f"Delete .bak files older than N days (default: {DEFAULT_BAK_MAX_AGE_DAYS})")
    args = parser.parse_args()

    vault_root = args.vault_root.resolve()
    if not vault_root.is_dir():
        print(f"Error: vault root not found at {vault_root}")
        raise SystemExit(1)

    print(f"Vault root: {vault_root}\n")

    print("=== Graphify backup rotation ===")
    graphify_deleted = rotate_graphify(vault_root, args.keep)

    print(f"\n=== Meta .bak cleanup (>{args.bak_max_age} days) ===")
    bak_deleted = clean_meta_bak(vault_root, args.bak_max_age)

    print(f"\nDone. Deleted {len(graphify_deleted)} graphify backup(s) "
          f"and {len(bak_deleted)} stale .bak file(s).")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
