#!/usr/bin/env python3
"""
rotate-last-session.py — keep Last Session.md lean by archiving old sessions

Problem: Last Session.md is read first on every UserPromptSubmit (session
protocol hook). Every old session in it pays a token tax on every prompt.
Fix: keep only the last N sessions in Last Session.md and move the rest
to monthly archive files.

Usage:
  python3 rotate-last-session.py                    # keep last 3 (default)
  python3 rotate-last-session.py --keep 1           # keep only the newest
  python3 rotate-last-session.py --dry-run          # show what would change
  python3 rotate-last-session.py --vault-root /path/to/vault

Archive layout:
  Meta/Session Archive/2026-04.md          # one file per YYYY-MM
  Meta/Session Archive/2026-03.md

Each archive file accumulates sessions chronologically. Running the
rotation script monthly is idempotent — it only moves sessions out of
Last Session.md if they exceed the keep count.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir as _find_meta_dir_helper  # noqa: E402


def detect_vault_root() -> Path:
    """Detect vault root from $VAULT_ROOT env var or script location."""
    env_root = os.environ.get("VAULT_ROOT")
    if env_root:
        return Path(env_root)
    script_dir = Path(__file__).resolve().parent
    candidate = script_dir.parent.parent
    if (candidate / "⚙️ Meta").is_dir():
        return candidate
    return Path.cwd()


def find_meta_dir(vault_root: Path) -> Path:
    return _find_meta_dir_helper(vault_root) or (vault_root / "Meta")


# Sessions start with "# Session —" at column 0.
SESSION_HEADER_RE = re.compile(r"^# Session —", re.MULTILINE)
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def parse_sessions(content: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Split the file into (preamble, list[(start, end, text)])."""
    matches = list(SESSION_HEADER_RE.finditer(content))
    if not matches:
        return content, []

    preamble = content[: matches[0].start()]
    sessions = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sessions.append((start, end, content[start:end]))
    return preamble, sessions


def session_month(session_text: str) -> str:
    """Return YYYY-MM for archive filename. First date wins."""
    match = DATE_RE.search(session_text)
    if not match:
        return dt.datetime.now().strftime("%Y-%m")
    return f"{match.group(1)}-{match.group(2)}"


def strip_update_pending_stubs(text: str) -> str:
    """Remove noisy session-end stubs from pre-archive content."""
    cleaned = re.sub(
        r"(?m)^(?:---\s*\n)?\*Session ended:[^\n]*update pending\*\s*\n",
        "",
        text,
    )
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def append_to_archive(archive_dir: Path, month: str, session_text: str, dry_run: bool) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{month}.md"
    header = (
        f"---\ncreationDate: {month}-01\ntype: meta\naliases: [session archive "
        f"{month}]\n---\n\n# Session Archive -- {month}\n\n"
        "*Older sessions rotated out of `Last Session.md` by "
        "`rotate-last-session.py`. Chronological order. Read this when you "
        "need historical context; the always-loaded `Last Session.md` only "
        "keeps the most recent sessions to minimize UserPromptSubmit token "
        "cost.*\n\n---\n\n"
    )
    if not path.exists():
        if not dry_run:
            path.write_text(header, encoding="utf-8")
        print(f"  created {path.name}")
    if not dry_run:
        with path.open("a", encoding="utf-8") as f:
            f.write(strip_update_pending_stubs(session_text).rstrip() + "\n\n---\n\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Rotate old sessions out of Last Session.md")
    parser.add_argument("--keep", type=int, default=3, help="Number of most recent sessions to keep (default: 3)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--vault-root", type=Path, default=None,
                        help="Path to vault root (default: auto-detected)")
    args = parser.parse_args()

    vault_root = (args.vault_root or detect_vault_root()).resolve()
    meta_dir = find_meta_dir(vault_root)
    last_session = meta_dir / "Last Session.md"
    archive_dir = meta_dir / "Session Archive"

    if not last_session.exists():
        print(f"ERROR: {last_session} not found", file=sys.stderr)
        return 1

    content = last_session.read_text(encoding="utf-8")
    preamble, sessions = parse_sessions(content)

    if len(sessions) <= args.keep:
        print(f"Nothing to rotate: {len(sessions)} session(s) in file, keep={args.keep}")
        return 0

    to_archive = sessions[: len(sessions) - args.keep]
    to_keep = sessions[len(sessions) - args.keep :]

    print(f"Found {len(sessions)} session(s). Archiving {len(to_archive)}, keeping {len(to_keep)}.")

    archived_files = set()
    for start, end, text in to_archive:
        month = session_month(text)
        path = append_to_archive(archive_dir, month, text, args.dry_run)
        archived_files.add(path.name)
        first_line = text.splitlines()[0][:80]
        print(f"  -> {path.name}: {first_line}")

    new_content = preamble.rstrip() + "\n\n"
    if archived_files:
        pointer = (
            "> **Rotated sessions:** older entries live in "
            + ", ".join(sorted(f"[[Session Archive/{f[:-3]}]]" for f in archived_files))
            + " -- read those when you need historical context.\n\n---\n\n"
        )
        new_content += pointer

    for _, _, text in to_keep:
        new_content += strip_update_pending_stubs(text).rstrip() + "\n\n"

    if args.dry_run:
        print(f"\n--- DRY RUN --- would write {len(new_content)} bytes to {last_session.name}")
        return 0

    backup = last_session.with_name(
        f"{last_session.stem}.bak-{dt.datetime.now().strftime('%Y-%m-%d-%H%M')}{last_session.suffix}"
    )
    backup.write_text(content, encoding="utf-8")
    print(f"  backup: {backup.name}")

    last_session.write_text(new_content, encoding="utf-8")
    old_size = len(content)
    new_size = len(new_content)
    saved = old_size - new_size
    print(
        f"Rotated. {last_session.name}: {old_size:,} -> {new_size:,} bytes "
        f"(-{saved:,} bytes / -{saved * 100 // old_size}%)"
    )
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
