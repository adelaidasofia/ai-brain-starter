#!/usr/bin/env python3
"""
aggregate-decisions.py — rebuild Decision Log.md from {VAULT}/⚙️ Meta/Decisions/*.md

Sister script to aggregate-sessions.py. Same rationale: concurrent
worktrees racing on Decision Log.md writes silently clobbered entries.
Fix: each decision lives in its own file in Decisions/, this script
rebuilds Decision Log.md by concatenating all decision files in reverse
chronological order.

Rotation (added 2026-05-03 after Decision Log hit 351 KB / +4600 lines/month):
  Inline = (decisions within last N months) ∪ (any decision with outcome:
  pending), regardless of age. Pending decisions never rotate out — they
  stay live as accountability surfaces (Brené veto). Closed-outcome
  decisions older than the window move to Decision Log Archive.md.
  Source files in Decisions/ are immutable; rotation is at output time.
  Default window: 6 months. Override via --inline-window-months.

Usage:
  python3 aggregate-decisions.py                # vault auto-detected from script location
  python3 aggregate-decisions.py --dry-run
  python3 aggregate-decisions.py --no-legacy
  python3 aggregate-decisions.py --inline-window-months 12
  VAULT_ROOT_FORCE=1 VAULT_ROOT=/other/vault python3 aggregate-decisions.py  # deliberate cross-vault

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

File format (Decisions/ entries):
  Each file must start with YAML frontmatter containing:
    - creationDate (ISO 8601)
    - type: decision
    - worktree: {name}  (or "main")
    - decision_date: YYYY-MM-DD
    - floor: {emotional floor at decision time}
    - stakes: Low | Medium | High
    - speed: Instant | Hours | Days | Weeks
    - next_step: REQUIRED. One concrete action (who + what + by when).
    - outcome: pending | {fill-in-later}
    - pattern: pending | {fill-in-later}
  Body is the decision entry (What / Why / Floor / Stakes / Speed / Next Step / ...).

Determinism rules identical to aggregate-sessions.py: sorted filename
descending → deterministic output → concurrent runs safe.
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
DECISIONS_DIR = META_DIR / "Decisions"
DECISION_LOG = META_DIR / "Decision Log.md"
DECISION_LOG_ARCHIVE = META_DIR / "Decision Log Archive.md"

AGGREGATOR_BEGIN = "<!-- aggregate-decisions:BEGIN -->"
AGGREGATOR_END = "<!-- aggregate-decisions:END -->"
LEGACY_HEADER = "## Legacy (pre-split) historical decisions"

INLINE_WINDOW_MONTHS_DEFAULT = 6


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
        f"last_updated: {dt.date.today().isoformat()}\n"
        "---\n"
        "\n"
        "# Decision Log\n"
        "\n"
        "*Tracking decisions to learn patterns over time. What I chose, why, "
        "what I was feeling, and what happened.*\n"
        "\n"
        "**Auto-generated** by `⚙️ Meta/scripts/aggregate-decisions.py` from "
        "`⚙️ Meta/Decisions/`. Do not edit this file directly — create or "
        "edit individual decision files in `⚙️ Meta/Decisions/` and re-run "
        "the aggregator (or let the session-end hook run it). Pre-split "
        "historical decisions are preserved as legacy content below the "
        "aggregator region.*\n"
        "\n"
        "## What to Track Per Decision\n"
        "\n"
        "- **What:** The decision in one sentence\n"
        "- **Why:** The reasoning — what tipped it\n"
        "- **Floor:** What emotional floor I was on when I decided\n"
        "- **Stakes:** Low / Medium / High\n"
        "- **Speed:** Instant / Hours / Days / Weeks\n"
        "- **Outcome:** (fill in later) What actually happened\n"
        "- **Pattern:** (fill in later) What this reveals about how I decide\n"
        "\n"
        "---\n"
        "\n"
    )


def gather_decision_files() -> list[Path]:
    """Return decision files sorted by filename descending (newest first)."""
    if not DECISIONS_DIR.exists():
        return []
    return sorted(
        [p for p in DECISIONS_DIR.glob("*.md") if p.is_file()],
        key=lambda p: p.name,
        reverse=True,
    )


def strip_frontmatter(content: str) -> str:
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            return content[end + 5 :].lstrip("\n")
    return content


def parse_frontmatter(content: str) -> dict:
    """Minimal flat-key YAML frontmatter parser. Ignores nested structures.

    Sufficient for the decision frontmatter schema (decision_date, outcome,
    stakes, etc. are all single-line key: value pairs).
    """
    if not content.startswith("---\n"):
        return {}
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}
    fm_text = content[4:end]
    out: dict[str, str] = {}
    for line in fm_text.split("\n"):
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip().strip("\"'")
    return out


def extract_first_heading(body: str) -> str:
    """Return the first '# ...' or '## ...' heading from the body, or ''."""
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
        if s.startswith("## "):
            return s[3:].strip()
    return ""


def is_pending(outcome: str) -> bool:
    """Treat empty / pending / fill-in-later as pending (always inline)."""
    o = (outcome or "").lower().strip().strip("{}")
    return o in ("", "pending", "fill-in-later", "tbd", "n/a")


def split_inline_vs_archive(
    files: list[Path], inline_window_months: int
) -> tuple[list[Path], list[Path]]:
    """Partition decision files into (inline, archive).

    Inline = within window OR pending-outcome OR undated.
    Archive = closed-outcome AND older than window.
    """
    cutoff = dt.date.today() - dt.timedelta(days=inline_window_months * 30)
    inline: list[Path] = []
    archive: list[Path] = []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            inline.append(f)  # fail safe: keep readable
            continue
        fm = parse_frontmatter(content)
        outcome = fm.get("outcome", "pending")
        date_str = fm.get("decision_date", "")
        try:
            d = dt.date.fromisoformat(date_str)
        except (ValueError, TypeError):
            d = None
        if is_pending(outcome):
            inline.append(f)  # pending → always inline regardless of age
        elif d is None or d >= cutoff:
            inline.append(f)
        else:
            archive.append(f)
    return inline, archive


def build_toc(files: list[Path], inline_window_months: int) -> str:
    """Auto-TOC for the inline portion: date — title — [PENDING] marker.

    No anchor links; Obsidian's outline panel handles in-file navigation.
    The TOC's job is letting a reader scan all titles without scrolling
    through a 350 KB file.
    """
    if not files:
        return ""
    lines = [
        "## Index",
        "",
        f"*{len(files)} decision(s) inline. Window: last {inline_window_months} months. "
        "Pending-outcome decisions stay inline regardless of age.*",
        "",
    ]
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            lines.append(f"- `????-??-??` — {f.stem} (read failed)")
            continue
        fm = parse_frontmatter(content)
        body = strip_frontmatter(content)
        date = fm.get("decision_date") or "????-??-??"
        outcome = fm.get("outcome", "pending")
        stakes = fm.get("stakes", "")
        title = extract_first_heading(body) or f.stem
        markers = []
        if is_pending(outcome):
            markers.append("PENDING")
        if stakes.lower() == "high":
            markers.append("HIGH")
        marker_str = f" `[{' / '.join(markers)}]`" if markers else ""
        lines.append(f"- `{date}` — {title}{marker_str}")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def build_archive_file(files: list[Path], inline_window_months: int) -> str:
    """Full content for Decision Log Archive.md."""
    today = dt.date.today().isoformat()
    preamble = (
        "---\n"
        f"creationDate: {today}\n"
        "type: meta\n"
        f"last_updated: {today}\n"
        "---\n"
        "\n# Decision Log Archive\n\n"
        "*Closed-outcome decisions older than the inline window "
        f"({inline_window_months} months). "
        f"Auto-generated by `⚙️ Meta/scripts/aggregate-decisions.py` from "
        "`⚙️ Meta/Decisions/`. Read [[Decision Log]] first; come here for "
        "older pattern detection. Source files in Decisions/ are immutable — "
        "rotation happens at output time only.*\n\n"
        "---\n\n"
    )
    aggregate = build_aggregate(files)
    return preamble + aggregate


def build_aggregate(files: list[Path]) -> str:
    blocks = []
    for f in files:
        body = strip_frontmatter(f.read_text(encoding="utf-8")).rstrip()
        blocks.append(body)

    header = (
        f"{AGGREGATOR_BEGIN}\n"
        f"*Last aggregated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} — "
        f"{len(files)} decision(s) from `⚙️ Meta/Decisions/`, newest first.*\n"
        "\n"
    )
    body = "\n\n---\n\n".join(blocks)
    footer = f"\n\n{AGGREGATOR_END}\n"
    return header + body + footer


def extract_legacy(existing: str) -> str:
    """Preserve pre-split historical decisions across aggregator runs.

    Steady-state: legacy header present → return content below it.
    First run: no legacy header → find the first '## YYYY-' heading
    (hand-written section markers like '## 2026-04') and preserve from
    there down."""
    legacy_idx = existing.find(LEGACY_HEADER)
    if legacy_idx != -1:
        after_header = existing[legacy_idx + len(LEGACY_HEADER) :].lstrip("\n")
        return after_header.rstrip()

    match = re.search(r"^## \d{4}-", existing, re.MULTILINE)
    if match:
        return existing[match.start() :].rstrip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild Decision Log.md from ⚙️ Meta/Decisions/*.md"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without writing"
    )
    parser.add_argument(
        "--no-legacy",
        action="store_true",
        help="Don't preserve historical content below the aggregator region",
    )
    parser.add_argument(
        "--inline-window-months",
        type=int,
        default=INLINE_WINDOW_MONTHS_DEFAULT,
        help=(
            "How many months of decisions stay inline in Decision Log.md. "
            "Older closed-outcome decisions move to Decision Log Archive.md. "
            f"Pending-outcome decisions stay inline regardless of age. Default: {INLINE_WINDOW_MONTHS_DEFAULT}."
        ),
    )
    parser.add_argument(
        "--no-toc",
        action="store_true",
        help="Skip the auto-generated Index/TOC at the top of Decision Log.md",
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

    files = gather_decision_files()

    if not files:
        print(
            f"WARNING: {DECISIONS_DIR} is empty. Not touching {DECISION_LOG.name}.",
            file=sys.stderr,
        )
        return 0

    inline_files, archive_files = split_inline_vs_archive(
        files, args.inline_window_months
    )

    inline_block = build_aggregate(inline_files)
    toc = "" if args.no_toc else build_toc(inline_files, args.inline_window_months)

    archive_pointer = ""
    if archive_files:
        archive_pointer = (
            "\n\n---\n\n## Archived decisions\n\n"
            f"*{len(archive_files)} closed-outcome decision(s) older than "
            f"{args.inline_window_months} months moved to "
            "[[Decision Log Archive]]. Search there for older pattern detection.*\n"
        )

    if DECISION_LOG.exists() and not args.no_legacy:
        existing = DECISION_LOG.read_text(encoding="utf-8")
        legacy = extract_legacy(existing)
    else:
        legacy = ""

    new_content = preamble() + toc + inline_block + archive_pointer
    if legacy:
        new_content += f"\n\n---\n\n{LEGACY_HEADER}\n\n"
        new_content += legacy.rstrip() + "\n"

    archive_content = (
        build_archive_file(archive_files, args.inline_window_months)
        if archive_files
        else None
    )

    if args.dry_run:
        print("--- DRY RUN ---")
        print(
            f"Would write {len(new_content):,} bytes to {DECISION_LOG.name} "
            f"({len(inline_files)} inline decision(s))"
        )
        if archive_content is not None:
            print(
                f"Would write {len(archive_content):,} bytes to "
                f"{DECISION_LOG_ARCHIVE.name} "
                f"({len(archive_files)} archived decision(s))"
            )
        else:
            print("No archive file needed (no decisions older than window).")
        print(f"\nInline ({len(inline_files)}):")
        for f in inline_files[:20]:
            print(f"  - {f.name}")
        if len(inline_files) > 20:
            print(f"  ... +{len(inline_files) - 20} more")
        print(f"\nArchive ({len(archive_files)}):")
        for f in archive_files[:20]:
            print(f"  - {f.name}")
        if len(archive_files) > 20:
            print(f"  ... +{len(archive_files) - 20} more")
        return 0

    log_backup = _backup_before_write(DECISION_LOG)
    DECISION_LOG.write_text(new_content, encoding="utf-8")
    print(
        f"Aggregated {len(inline_files)} inline decision(s) into "
        f"{DECISION_LOG.name} ({len(new_content):,} bytes)"
        + (f" [backup: {log_backup.name}]" if log_backup else "")
    )
    if archive_content is not None:
        archive_backup = _backup_before_write(DECISION_LOG_ARCHIVE)
        DECISION_LOG_ARCHIVE.write_text(archive_content, encoding="utf-8")
        print(
            f"Archived {len(archive_files)} closed decision(s) to "
            f"{DECISION_LOG_ARCHIVE.name} ({len(archive_content):,} bytes)"
            + (f" [backup: {archive_backup.name}]" if archive_backup else "")
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
