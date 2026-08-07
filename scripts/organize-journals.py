#!/usr/bin/env python3
"""
organize-journals.py
--------------------
Organizes a flat Journals folder into month subfolders ("January 2026", etc.)
based on the `creationDate` field in each file's YAML frontmatter.

Also moves any existing Monthly Summaries and Weekly Insights subfolders
into their matching month folders, then removes the now-empty subfolders.

Usage:
    python3 organize-journals.py --vault-root "/path/to/your/vault"

The Journals folder is expected at: <vault-root>/Journals/
Customize JOURNALS_DIR if your vault uses a different name (e.g. "📓 Journals").
"""

import sys
import argparse
import os
import re
import shutil
from pathlib import Path
from datetime import datetime

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]


def get_creation_date(filepath):
    """Extract creationDate from YAML frontmatter (reads only first 500 chars)."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read(500)
        if not content.startswith("---"):
            return None
        end = content.find("---", 3)
        if end == -1:
            return None
        frontmatter = content[3:end]
        match = re.search(r"creationDate:\s*(\d{4}-\d{2}-\d{2})", frontmatter)
        if match:
            return match.group(1)
    except Exception as e:
        print(f"  ERROR reading {filepath.name}: {e}")
    return None


def month_folder_name(year, month):
    return f"{MONTH_NAMES[month]} {year}"


def parse_monthly_summary(filename):
    """'YYYY-MM Monthly Summary.md' → (year, month)"""
    match = re.match(r"(\d{4})-(\d{2}) Monthly Summary", filename)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def parse_weekly_insight(filename):
    """'Apr. 7-13, 2026 Weekly.md' → (year, month) using the end date's month."""
    match = re.search(r"(\w+)\.?\s+\d+-\d+,\s+(\d{4})", filename)
    if match:
        month_abbr = match.group(1)
        year = int(match.group(2))
        try:
            month = datetime.strptime(month_abbr, "%b").month
            return year, month
        except Exception:
            pass
    return None, None


def move_file(src, dst_dir, label=""):
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        print(f"  SKIP (exists): {src.name}")
        return False
    shutil.move(str(src), str(dst))
    if label:
        print(f"  {src.name} → {label}/")
    return True


def main():
    parser = argparse.ArgumentParser(description="Organize journal entries into month folders.")
    parser.add_argument("--vault-root", required=True, help="Path to your Obsidian vault root")
    parser.add_argument(
        "--journals-folder",
        default=None,
        help="Name of the journals folder inside vault root (auto-detected if omitted)",
    )
    args = parser.parse_args()

    vault = Path(args.vault_root).expanduser().resolve()

    # Auto-detect journals folder (handles emoji prefix variants)
    if args.journals_folder:
        journals_dir = vault / args.journals_folder
    else:
        candidates = [d for d in vault.iterdir() if d.is_dir() and "journal" in d.name.lower()]
        if not candidates:
            print("ERROR: Could not find a Journals folder. Pass --journals-folder explicitly.")
            return
        journals_dir = candidates[0]
        print(f"Using journals folder: {journals_dir.name}")

    if not journals_dir.exists():
        print(f"ERROR: Journals folder not found at {journals_dir}")
        return

    # Step 1: Organize journal entries by creationDate
    print("\n=== Organizing journal entries ===")
    moved = 0
    no_date = []

    for filepath in journals_dir.iterdir():
        if not filepath.is_file() or filepath.suffix != ".md":
            continue

        date_str = get_creation_date(filepath)
        if not date_str:
            no_date.append(filepath.name)
            continue

        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            folder_name = month_folder_name(dt.year, dt.month)
            if move_file(filepath, journals_dir / folder_name):
                moved += 1
        except Exception as e:
            print(f"  ERROR moving {filepath.name}: {e}")

    print(f"Moved: {moved} entries")
    if no_date:
        print(f"Skipped (no creationDate): {len(no_date)} files")
        for name in no_date[:5]:
            print(f"  {name}")
        if len(no_date) > 5:
            print(f"  ... and {len(no_date) - 5} more")

    # Step 2: Move Monthly Summaries subfolder contents
    monthly_dir = journals_dir / "Monthly Summaries"
    if monthly_dir.exists():
        print("\n=== Moving Monthly Summaries ===")
        for filepath in monthly_dir.iterdir():
            if not filepath.is_file():
                continue
            year, month = parse_monthly_summary(filepath.name)
            if year and month:
                folder_name = month_folder_name(year, month)
                move_file(filepath, journals_dir / folder_name, folder_name)
            else:
                print(f"  Could not parse: {filepath.name}")
        try:
            monthly_dir.rmdir()
            print("  Removed empty 'Monthly Summaries' folder")
        except OSError:
            print("  'Monthly Summaries' not empty — left in place")

    # Step 3: Move Weekly Insights subfolder contents
    weekly_dir = journals_dir / "Weekly Insights"
    if weekly_dir.exists():
        print("\n=== Moving Weekly Insights ===")
        for filepath in weekly_dir.iterdir():
            if not filepath.is_file():
                continue
            year, month = parse_weekly_insight(filepath.name)
            if year and month:
                folder_name = month_folder_name(year, month)
                move_file(filepath, journals_dir / folder_name, folder_name)
            else:
                print(f"  Could not parse: {filepath.name}")
        try:
            weekly_dir.rmdir()
            print("  Removed empty 'Weekly Insights' folder")
        except OSError:
            print("  'Weekly Insights' not empty — left in place")

    # Summary
    print("\n=== Done ===")
    folders = sorted([d.name for d in journals_dir.iterdir() if d.is_dir()])
    print(f"Month folders created: {len(folders)}")
    for folder in folders[:5]:
        count = len(list((journals_dir / folder).iterdir()))
        print(f"  {folder}/ ({count} files)")
    if len(folders) > 5:
        print(f"  ... and {len(folders) - 5} more")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
