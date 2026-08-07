#!/usr/bin/env python3
"""
decision-outcome-check.py — surface stale decisions awaiting an outcome

Problem: the Decision Log fills up with `Outcome:` and `Pattern:` fields
left blank "to be filled in later." Later never comes. A log of decisions
without outcomes can't teach you anything about your own patterns.

Fix: weekly cron walks the Decision Log, finds every decision older than
N days (default 30) with a blank Outcome field, and writes a "Decisions
awaiting outcome" section at the top of Current Priorities.md. One
natural prompt per week to fill them in.

Usage:
  python3 decision-outcome-check.py              # default 30 days
  python3 decision-outcome-check.py --days 14    # more aggressive
  python3 decision-outcome-check.py --dry-run    # preview
  python3 decision-outcome-check.py --vault-root /path/to/vault
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir  # noqa: E402


def _resolve_vault_root(cli_override: Path | None) -> tuple[Path, str]:
    """Resolve the vault root, defeating the inherited-VAULT_ROOT footgun.

    Precedence: an explicit --vault-root is honored; otherwise the vault is
    auto-detected from THIS script's own location (⚙️ Meta/scripts/ → 2 levels
    up). An ambient VAULT_ROOT env var is honored ONLY when it matches the
    script's own vault, or when VAULT_ROOT_FORCE=1 (deliberate cross-vault).
    Otherwise it is ignored (with a stderr warning) and the script's own vault
    is used — so a globally-exported VAULT_ROOT can't silently redirect a
    ported copy at the wrong vault, causing wrong-vault reads and destructive
    wrong-vault writes with NO error."""
    if cli_override is not None:
        return cli_override.resolve(), "--vault-root (explicit)"
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


# Decision entries use this shape:
#   ### <Title>
#   - **Date:** 2026-04-11
#   - **What:** ...
#   - **Why:** ...
#   - **Floor:** [[...]]
#   - **Stakes:** ...
#   - **Speed:** ...
#   - **Outcome:**
#   - **Pattern:**
DECISION_HEADER_RE = re.compile(r"^### (.+)$", re.MULTILINE)
# Two date formats in this log:
#   1) "### 2026-04-11 — Some title"  (date embedded in header)
#   2) "### Some title"  then on a later line  "- **Date:** 2026-04-11"
HEADER_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*[—-]")
DATE_FIELD_RE = re.compile(r"\*\*Date:\*\*\s*(\d{4}-\d{2}-\d{2})")
OUTCOME_FIELD_RE = re.compile(r"\*\*Outcome:\*\*\s*(.*)")

# Italicized parentheticals like "*(fill in 30 days from now...)*" count as
# unfilled — they're placeholder notes, not real outcomes.
PLACEHOLDER_RE = re.compile(r"^\s*\*?\(fill in|\*\(verify|\*\(measure|\*\(check", re.IGNORECASE)

# The auto-generated section header in Current Priorities — we use it
# as an anchor so we can replace it cleanly each run instead of
# accumulating duplicates.
PRIORITIES_MARKER_START = "<!-- decision-outcome-check: START -->"
PRIORITIES_MARKER_END = "<!-- decision-outcome-check: END -->"


def _backup_before_write(path: Path, keep: int = 3) -> Path | None:
    """Write a timestamped backup of `path` before it is overwritten, so a
    bad rewrite — or a wrong-vault run that somehow slips past the vault-root
    guard — can never silently destroy the prior good file. Keeps the most
    recent `keep` backups (older ones pruned) so repeated runs never fill the
    vault with .bak files. Returns the backup path, or None when there was
    nothing to back up."""
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


def parse_decisions(content: str) -> list[dict]:
    """Return [{title, date, outcome_blank, start, end, raw}, ...]."""
    matches = list(DECISION_HEADER_RE.finditer(content))
    decisions = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        block = content[start:end]

        rule_idx = block.find("\n---\n")
        if rule_idx != -1:
            block = block[:rule_idx]

        header_date_match = HEADER_DATE_RE.match(title)
        date_match = DATE_FIELD_RE.search(block)
        date_str = None
        if header_date_match:
            date_str = header_date_match.group(1)
            title = title[header_date_match.end():].lstrip(" —-").strip()
        elif date_match:
            date_str = date_match.group(1)
        if not date_str:
            continue
        try:
            date = dt.date.fromisoformat(date_str)
        except ValueError:
            continue

        outcome_match = OUTCOME_FIELD_RE.search(block)
        outcome_text = outcome_match.group(1).strip() if outcome_match else ""
        outcome_blank = (
            not outcome_text
            or PLACEHOLDER_RE.match(outcome_text) is not None
        )

        decisions.append(
            {
                "title": title,
                "date": date,
                "outcome_blank": outcome_blank,
                "raw": block.strip(),
            }
        )
    return decisions


def build_section(stale: list[dict]) -> str:
    if not stale:
        return (
            f"\n{PRIORITIES_MARKER_START}\n"
            f"<!-- generated by decision-outcome-check.py — do not edit by hand -->\n"
            f"{PRIORITIES_MARKER_END}\n"
        )

    lines = [
        PRIORITIES_MARKER_START,
        "<!-- generated by decision-outcome-check.py — do not edit by hand -->",
        "",
        "## Decisions awaiting outcome",
        "",
        f"*{len(stale)} decision{'s' if len(stale) != 1 else ''} older than the threshold "
        "still have a blank `Outcome:` field. Fill them in so the log can start "
        "teaching you about your own patterns.*",
        "",
    ]
    for d in sorted(stale, key=lambda x: x["date"]):
        age = (dt.date.today() - d["date"]).days
        lines.append(f"- **[{d['date']}]** {d['title']} — {age}d ago")
    lines.append("")
    lines.append(PRIORITIES_MARKER_END)
    return "\n".join(lines) + "\n"


def splice_section(content: str, new_section: str) -> str:
    if PRIORITIES_MARKER_START in content:
        return re.sub(
            rf"{re.escape(PRIORITIES_MARKER_START)}.*?{re.escape(PRIORITIES_MARKER_END)}\s*",
            new_section.strip() + "\n",
            content,
            flags=re.DOTALL,
        )
    header_re = re.compile(r"^(# Current Priorities\s*\n(?:\n\*[^*\n]+\*\s*\n)?)", re.MULTILINE)
    match = header_re.search(content)
    if not match:
        return content + "\n" + new_section
    insert_at = match.end()
    return content[:insert_at] + "\n" + new_section + content[insert_at:]


def main() -> int:
    parser = argparse.ArgumentParser(description="Surface stale Decision Log entries awaiting outcomes")
    parser.add_argument("--days", type=int, default=30, help="Decisions older than this are stale (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--vault-root", type=Path, default=None,
                        help="Path to vault root (default: auto-detected)")
    args = parser.parse_args()

    vault_root, _source = _resolve_vault_root(args.vault_root)
    print(f"VAULT_ROOT: {vault_root}  [{_source}]")
    meta_dir = find_meta_dir(vault_root) or (vault_root / "Meta")

    decision_log = meta_dir / "Decision Log.md"
    priorities = meta_dir / "Current Priorities.md"

    if not decision_log.exists():
        print(f"ERROR: {decision_log} not found", file=sys.stderr)
        return 1
    if not priorities.exists():
        print(f"ERROR: {priorities} not found", file=sys.stderr)
        return 1

    decisions = parse_decisions(decision_log.read_text(encoding="utf-8"))
    today = dt.date.today()
    threshold = today - dt.timedelta(days=args.days)

    stale = [
        d for d in decisions
        if d["outcome_blank"] and d["date"] <= threshold
    ]

    print(f"Decision Log: {len(decisions)} entries parsed.")
    print(f"Threshold: older than {args.days} days (on/before {threshold}).")
    print(f"Stale (blank outcome + past threshold): {len(stale)}.")

    for d in sorted(stale, key=lambda x: x["date"]):
        age = (today - d["date"]).days
        print(f"  [{d['date']}] {d['title']} ({age}d ago)")

    new_section = build_section(stale)

    if args.dry_run:
        print("\n--- DRY RUN --- would write:\n")
        print(new_section)
        return 0

    priorities_content = priorities.read_text(encoding="utf-8")
    updated = splice_section(priorities_content, new_section)

    if updated == priorities_content:
        print("No changes to Current Priorities.md.")
        return 0

    backup = _backup_before_write(priorities)
    priorities.write_text(updated, encoding="utf-8")
    print(
        f"Updated {priorities.name}"
        + (f" (backup: {backup.name})" if backup else "")
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
