#!/usr/bin/env python3
"""
graphify_prune_stale_cache.py

Deletes cache entries whose SHA256 key doesn't match any current file in the
corpus folder. These accumulate when files are edited, moved, or renamed (the
cache key combines content + resolved_path, so both changes invalidate).

Run this periodically (monthly) or after any vault restructuring. Counterpart
to the new mtime-manifest short-circuit in graphify_stage_select.py (Lesson
#93): the manifest makes live runs fast, prune keeps the cache directory from
growing indefinitely.

Usage:
    python3 graphify_prune_stale_cache.py <corpus_folder> --vault-root <path> [--dry-run]

Ship date: 2026-04-15
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus_folder", help="e.g. 'Team' or 'Journals'")
    ap.add_argument("--vault-root", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    vault = Path(args.vault_root)
    folder = vault / args.corpus_folder
    if not folder.exists():
        print(f"folder not found: {folder}", file=sys.stderr)
        sys.exit(1)

    # Auto-detect layout (matches graphify_stage_select.py Lesson #87)
    team_cache = vault / "graphify-out" / "cache"
    personal_cache = vault / "⚙️ Meta" / "graphify-out" / "cache"
    if team_cache.exists():
        cache_dir = team_cache
        layout = "team-vault"
    elif personal_cache.exists():
        cache_dir = personal_cache
        layout = "personal"
    else:
        print(f"no cache dir found under {vault}", file=sys.stderr)
        sys.exit(1)

    print(f"Layout: {layout}")
    print(f"Cache dir: {cache_dir}")

    # Also honor the SKIP_PARTS used by select so we don't count Meta/Archive
    # files as "current" (they're filtered from extraction anyway).
    SKIP = {"_review_alternate_drafts", "⚙️ Meta", "Archive", "🗄 Archive"}

    # Lesson #94: graphify.cache hashes content + \x00 + relative_to(root)
    # when file is inside root, falling back to absolute path when not. Compute
    # BOTH variants for every file so we don't falsely prune cache entries that
    # were written with the lib's relative-path scheme.
    current_hashes = set()
    n_files = 0
    vault_resolved = vault.resolve()
    for f in folder.rglob("*.md"):
        if any(p in SKIP for p in f.parts):
            continue
        if f.stem.endswith(" 2") or f.stem.endswith(" 3"):
            continue
        try:
            content = f.read_bytes()
            abs_p = str(f.resolve()).encode()
            # Absolute-path variant (our own scripts)
            current_hashes.add(hashlib.sha256(content + b"\x00" + abs_p).hexdigest())
            # Relative-to-vault variant (graphify library)
            try:
                rel = str(f.resolve().relative_to(vault_resolved)).encode()
                current_hashes.add(hashlib.sha256(content + b"\x00" + rel).hexdigest())
            except ValueError:
                pass
            n_files += 1
        except Exception:
            pass
    print(f"Current files indexed: {n_files} (each contributes 1-2 hash variants)")

    all_cache = list(cache_dir.glob("*.json"))
    stale = [c for c in all_cache if c.stem not in current_hashes]
    print(f"Total cache entries: {len(all_cache)}")
    print(f"Stale: {len(stale)}  ({100 * len(stale) / max(1, len(all_cache)):.0f}%)")

    if args.dry_run:
        print("DRY RUN: no files deleted.")
        return

    deleted = 0
    for c in stale:
        try:
            c.unlink()
            deleted += 1
        except Exception as e:
            print(f"  failed to delete {c.name}: {e}", file=sys.stderr)
    print(f"Deleted {deleted} stale cache entries.")
    print(f"Remaining: {len(all_cache) - deleted}")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
