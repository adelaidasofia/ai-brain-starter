#!/usr/bin/env python3
"""
aggregate-sessions.py — rebuild Last Session.md from {VAULT}/⚙️ Meta/Sessions/*.md

Problem: multiple Claude Code worktrees running session-end cascades
concurrently raced on Last Session.md writes. Last write wins. Entries from
earlier sessions were silently clobbered.

Fix: each session writes its content to its own unique file in
{VAULT}/⚙️ Meta/Sessions/ (filename: YYYY-MM-DDTHH-MM-{worktree}.md). This
script rebuilds Last Session.md by concatenating the N most recent session
files in reverse chronological order. Because the output is a deterministic
function of the sorted input, concurrent runs of this script produce
identical output — safe against races.

Usage:
  python3 aggregate-sessions.py                 # vault auto-detected from script location
  python3 aggregate-sessions.py --keep 5
  python3 aggregate-sessions.py --dry-run
  python3 aggregate-sessions.py --no-legacy
  VAULT_ROOT_FORCE=1 VAULT_ROOT=/other/vault python3 aggregate-sessions.py  # deliberate cross-vault

Environment variables:
  VAULT_ROOT        — absolute path to the vault root. Optional: by default the
                      vault is auto-detected from this script's OWN location
                      (⚙️ Meta/scripts/ → 2 levels up). If VAULT_ROOT is set but
                      points at a DIFFERENT vault than the one this copy lives
                      in, it is IGNORED (with a stderr warning) and the script's
                      own vault is used — so a globally-exported VAULT_ROOT can't
                      silently redirect a ported copy at the wrong vault.
  VAULT_ROOT_FORCE  — set to 1 to honor VAULT_ROOT even when it differs from the
                      script's own vault (deliberate cross-vault runs).

File format (Sessions/ entries):
  Each file must start with YAML frontmatter containing:
    - creationDate (ISO 8601)
    - type: session
    - worktree: {name}   (or "main" for non-worktree sessions)
    - session_date: YYYY-MM-DD
    - session_label: "..."  (use "update pending" for stubs to filter)
  Body starts with a "# Session — ..." top-level heading.

Stub filtering: files with `session_label: "update pending"` are excluded
from the aggregated view so orphaned placeholders don't pollute
Last Session.md. A stub becomes a real entry once Claude fills in its body
and changes the session_label.

Determinism rules:
  - Files sorted by filename descending (reverse ISO timestamp order)
  - Top N concatenated with "---" separators
  - Deterministic preamble + aggregator region markers
  - Same input → same output, always → concurrent runs are safe

Safety:
  - If Sessions/ is empty, this script DOES NOT touch Last Session.md.
    This prevents wiping a historical file on first run before migration.
  - Pre-split historical content is preserved below the aggregator region
    under a "## Legacy (pre-split) historical entries" header.
  - Pass --no-legacy to disable legacy preservation (destructive — review
    before using on a vault with existing content).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

# --- VAULT_ROOT resolution ------------------------------------------------
# Ground truth is THIS script's own location: ⚙️ Meta/scripts/ → 2 levels up
# is the vault this physical copy belongs to. A VAULT_ROOT env var is honored
# only when it points at that same vault, or when the caller explicitly sets
# VAULT_ROOT_FORCE=1.
#
# Why: a globally-exported VAULT_ROOT (e.g. a shell-profile export) otherwise
# silently redirects EVERY copy of this script — including copies ported into
# other vaults — at that one vault, causing wrong-vault reads and destructive
# wrong-vault writes with NO error. Preferring the script's own location on
# mismatch is fail-safe: a copy can only ever touch the vault it lives in.
def _resolve_vault_root() -> tuple[Path, str]:
    auto_root = Path(__file__).resolve().parent.parent.parent
    env_raw = os.environ.get("VAULT_ROOT")
    if not env_raw:
        return auto_root, "auto-detect (script location)"
    env_root = Path(os.path.expanduser(env_raw)).resolve()
    if env_root == auto_root:
        return env_root, "env VAULT_ROOT (matches script location)"
    if os.environ.get("VAULT_ROOT_FORCE", "").strip().lower() in ("1", "true", "yes"):
        return env_root, f"env VAULT_ROOT (FORCED, differs from script vault {auto_root})"
    print(
        f"WARNING: VAULT_ROOT env points at {env_root}, but this script lives in "
        f"{auto_root}. Operating on the script's own vault ({auto_root}); this "
        f"copy will NOT touch {env_root}. Set VAULT_ROOT_FORCE=1 to override.",
        file=sys.stderr,
    )
    return auto_root, "auto-detect (env VAULT_ROOT ignored: vault mismatch)"


VAULT_ROOT, _VAULT_ROOT_SOURCE = _resolve_vault_root()
META_DIR = VAULT_ROOT / "⚙️ Meta"
SESSIONS_DIR = META_DIR / "Sessions"
LAST_SESSION = META_DIR / "Last Session.md"

AGGREGATOR_BEGIN = "<!-- aggregate-sessions:BEGIN -->"
AGGREGATOR_END = "<!-- aggregate-sessions:END -->"
LEGACY_HEADER = "## Legacy (pre-split) historical entries"


def _backup_before_write(path: Path, keep: int = 3) -> Path | None:
    """Write a timestamped backup of `path` before it is overwritten, so a
    bad aggregation — or a wrong-vault run that somehow slips past the
    vault-root guard — can never silently destroy the prior good file. Keeps
    the most recent `keep` backups (older ones pruned) so a file rebuilt on
    every session-close never fills the vault with .bak files. Returns the
    backup path, or None when there was nothing to back up."""
    if not path.exists():
        return None
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.bak-{stamp}{path.suffix}")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    for old in sorted(
        path.parent.glob(f"{path.stem}.bak-*{path.suffix}"), reverse=True
    )[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
    return backup


def preamble() -> str:
    return (
        "---\n"
        f"creationDate: {dt.date.today().isoformat()}\n"
        "type: meta\n"
        "---\n"
        "\n"
        "*Auto-generated by `⚙️ Meta/scripts/aggregate-sessions.py` from "
        "`⚙️ Meta/Sessions/`. Do not edit this file directly — edit the "
        "individual session files instead and re-run the aggregator (or "
        "wait for the next session-end hook to run it). Historical entries "
        "that were written before the per-worktree split live below the "
        "aggregator region as legacy content.*\n"
        "\n"
    )


def is_stub(content: str) -> bool:
    """A stub is a session file written by the session-end hook but not
    yet filled in by Claude. Detect via the `session_label: "update
    pending"` line in the frontmatter OR the placeholder marker in the
    body. Stubs are excluded from the aggregator view."""
    if 'session_label: "update pending"' in content:
        return True
    if "stub written by session-end-hook.sh" in content:
        return True
    return False


def gather_session_files(include_stubs: bool = False) -> list[Path]:
    """Return session files sorted by filename descending (newest first).

    By default, stub files are excluded. Pass include_stubs=True to
    include them."""
    if not SESSIONS_DIR.exists():
        return []
    all_files = [
        p for p in SESSIONS_DIR.glob("*.md")
        if p.is_file() and not p.name.startswith("legacy")
    ]
    if not include_stubs:
        all_files = [
            p for p in all_files if not is_stub(p.read_text(encoding="utf-8"))
        ]
    return sorted(all_files, key=lambda p: p.name, reverse=True)


def strip_frontmatter(content: str) -> str:
    """Remove the YAML frontmatter block at the top of a session file."""
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            return content[end + 5 :].lstrip("\n")
    return content


def truncate_body(body: str, max_lines: int, source_file: Path) -> str:
    """Truncate a session body to max_lines, adding a pointer to the full file."""
    lines = body.split("\n")
    if max_lines <= 0 or len(lines) <= max_lines:
        return body
    truncated = "\n".join(lines[:max_lines])
    truncated += (
        f"\n\n> *[truncated — {len(lines)} lines total. "
        f"Full session: `⚙️ Meta/Sessions/{source_file.name}`]*"
    )
    return truncated


def build_aggregate(files: list[Path], keep: int, max_lines: int = 0) -> str:
    """Build the aggregator region content from the top N session files."""
    top = files[:keep]
    blocks = []
    for f in top:
        body = strip_frontmatter(f.read_text(encoding="utf-8")).rstrip()
        if max_lines > 0:
            body = truncate_body(body, max_lines, f)
        blocks.append(body)

    header = (
        f"{AGGREGATOR_BEGIN}\n"
        f"*Last aggregated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} — "
        f"showing top {len(top)} of {len(files)} session(s) from "
        f"`⚙️ Meta/Sessions/`.*\n"
        "\n"
        "> **Rotated sessions:** older entries beyond the top N still live "
        "in `⚙️ Meta/Sessions/` — read those when you need historical "
        "context.\n"
        "\n"
        "---\n\n"
    )
    body = "\n\n---\n\n".join(blocks)
    footer = f"\n\n{AGGREGATOR_END}\n"
    return header + body + footer


def extract_legacy(existing: str) -> str:
    """Return the legacy (pre-split) historical content from an existing
    Last Session.md, for preservation across aggregator runs.

    Steady-state: legacy header present → return content below it.
    First run: no legacy header → treat everything below the frontmatter
    as legacy."""
    legacy_idx = existing.find(LEGACY_HEADER)
    if legacy_idx != -1:
        # Strip the header itself — main() re-adds it.
        after_header = existing[legacy_idx + len(LEGACY_HEADER) :].lstrip("\n")
        # Strip any aggregator regions that got snowballed into legacy
        # (bug fix: steady-state path was not stripping these, causing
        # exponential growth on each run)
        pattern = re.compile(
            re.escape(AGGREGATOR_BEGIN)
            + r".*?"
            + re.escape(AGGREGATOR_END),
            re.DOTALL,
        )
        after_header = pattern.sub("", after_header)
        # Strip duplicate legacy headers that got nested
        after_header = after_header.replace(LEGACY_HEADER, "")
        # Clean up excessive blank lines left by stripping
        after_header = re.sub(r"\n{4,}", "\n\n---\n\n", after_header)
        return after_header.rstrip()

    # First-run migration: no legacy header yet.
    if existing.startswith("---\n"):
        fm_end = existing.find("\n---\n", 4)
        if fm_end != -1:
            below_fm = existing[fm_end + 5 :].lstrip("\n")
            # Strip any leftover aggregator region from a buggy earlier run
            if AGGREGATOR_BEGIN in below_fm:
                pattern = re.compile(
                    re.escape(AGGREGATOR_BEGIN)
                    + r".*?"
                    + re.escape(AGGREGATOR_END),
                    re.DOTALL,
                )
                below_fm = pattern.sub("", below_fm)
            # Strip an "*Auto-generated...*" preamble line if present
            below_fm = re.sub(
                r"^\*Auto-generated[^\n]*\*\s*\n+",
                "",
                below_fm,
                count=1,
                flags=re.MULTILINE,
            )
            return below_fm.strip()
    return existing.rstrip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild Last Session.md from ⚙️ Meta/Sessions/*.md"
    )
    parser.add_argument(
        "--keep", type=int, default=2, help="Number of sessions to show (default: 2)"
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=60,
        help="Max lines per session entry before truncation (default: 60)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without writing"
    )
    parser.add_argument(
        "--no-legacy",
        action="store_true",
        help="Don't preserve historical content below the aggregator region",
    )
    args = parser.parse_args()

    print(f"VAULT_ROOT: {VAULT_ROOT}  [{_VAULT_ROOT_SOURCE}]")

    if not META_DIR.exists():
        print(
            f"ERROR: {META_DIR} does not exist. Set VAULT_ROOT env var "
            f"to your vault path.",
            file=sys.stderr,
        )
        return 1

    files = gather_session_files()

    if not files:
        print(
            f"WARNING: {SESSIONS_DIR} is empty. Not touching {LAST_SESSION.name}.",
            file=sys.stderr,
        )
        return 0

    aggregate_block = build_aggregate(files, args.keep, args.max_lines)

    if LAST_SESSION.exists() and not args.no_legacy:
        existing = LAST_SESSION.read_text(encoding="utf-8")
        legacy = extract_legacy(existing)
    else:
        legacy = ""

    new_content = preamble() + aggregate_block
    if legacy:
        new_content += f"\n\n---\n\n{LEGACY_HEADER}\n\n"
        new_content += legacy.rstrip() + "\n"

    # Safety cap: never produce a file larger than 15KB (~4k tokens).
    # If the output exceeds this, the legacy section has grown too large
    # or something else snowballed. Truncate legacy with a pointer.
    MAX_BYTES = 15_000
    if len(new_content.encode("utf-8")) > MAX_BYTES:
        print(
            f"WARNING: output would be {len(new_content.encode('utf-8')):,} bytes "
            f"(cap: {MAX_BYTES:,}). Truncating legacy to fit.",
            file=sys.stderr,
        )
        # Rebuild without legacy, add a pointer instead
        new_content = preamble() + aggregate_block
        new_content += (
            f"\n\n---\n\n{LEGACY_HEADER}\n\n"
            "*Legacy content exceeded size cap and was moved to "
            "`⚙️ Meta/Sessions/legacy-pre-split.md`. Read that file "
            "when you need historical context.*\n"
        )

    if args.dry_run:
        print(f"--- DRY RUN ---")
        print(f"Would write {len(new_content):,} bytes to {LAST_SESSION.name}")
        print(f"Top {min(args.keep, len(files))} of {len(files)} session(s):")
        for f in files[: args.keep]:
            print(f"  - {f.name}")
        return 0

    backup = _backup_before_write(LAST_SESSION)
    LAST_SESSION.write_text(new_content, encoding="utf-8")
    print(
        f"Aggregated {min(args.keep, len(files))} of {len(files)} session(s) "
        f"into {LAST_SESSION.name} ({len(new_content):,} bytes)"
        + (f" [backup: {backup.name}]" if backup else "")
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
