#!/usr/bin/env python3
"""
decision-retrospective.py — surface decisions older than N days with empty Outcome.

Decisions/ files have an `outcome:` frontmatter field that's typically empty
when the decision is logged and filled in later as the situation resolves.
Without a forcing function, Outcome fields stay empty forever and the
quarterly retrospective never happens.

This script scans every Decisions/*.md file, reports those with empty
outcome that are older than the threshold, and produces a review prompt.

Usage:
  python3 scripts/decision-retrospective.py [--vault-root PATH] [--days 90]
                                            [--json] [--quiet]
                                            [--apply-prompt]

`--apply-prompt` mode: appends a "Retrospective candidates" section to
<vault>/⚙️ Meta/Decision Retrospective.md with one entry per stale decision,
ready for the user to fill in during /sunday-review or /monthly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir as _find_meta_dir_helper  # noqa: E402


def find_meta_dir(vault: Path) -> Path:
    return _find_meta_dir_helper(vault) or (vault / "Meta")


def parse_frontmatter(text: str) -> dict | None:
    if not text.startswith("---"):
        return None
    m = re.match(r"^---\n(.*?)\n---\s*", text, re.DOTALL)
    if not m:
        return None
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None


def is_outcome_empty(fm: dict) -> bool:
    val = fm.get("outcome")
    if val is None:
        return True
    if isinstance(val, str) and val.strip().lower() in {"", "(pending)", "tbd", "—", "-", "pending"}:
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault-root", default=os.environ.get("VAULT_ROOT", os.getcwd()))
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--apply-prompt", action="store_true",
                    help="append a Retrospective Candidates section to Meta/Decision Retrospective.md")
    args = ap.parse_args()

    vault = Path(args.vault_root).resolve()
    meta = find_meta_dir(vault)
    decisions_dir = meta / "Decisions"
    if not decisions_dir.is_dir():
        print(f"No Decisions/ folder at {decisions_dir}", file=sys.stderr)
        return 1

    cutoff_ts = datetime.now(timezone.utc).timestamp() - args.days * 86400
    candidates = []
    for path in sorted(decisions_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = parse_frontmatter(text)
        if not fm:
            continue
        if not is_outcome_empty(fm):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff_ts:
            continue
        # Extract first-line summary
        body = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)
        first_heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = first_heading.group(1).strip() if first_heading else path.stem
        age_days = int((datetime.now(timezone.utc).timestamp() - mtime) / 86400)
        candidates.append({
            "path": str(path),
            "title": title,
            "age_days": age_days,
            "stakes": fm.get("stakes"),
            "decision_date": fm.get("decision_date"),
        })

    candidates.sort(key=lambda c: c["age_days"], reverse=True)

    if args.json:
        print(json.dumps({"days": args.days, "candidates": candidates}, ensure_ascii=False, indent=2))
        return 0

    if not args.quiet:
        if not candidates:
            print(f"No decisions older than {args.days} days have empty Outcome. Clean.")
        else:
            print(f"Decisions ≥{args.days} days old with empty Outcome ({len(candidates)} total):\n")
            for c in candidates:
                stakes = f" [{c['stakes']}]" if c.get("stakes") else ""
                print(f"  - [{c['age_days']}d]{stakes} {c['title']}")
                print(f"    {c['path']}")

    if args.apply_prompt and candidates and meta.is_dir():
        retro_path = meta / "Decision Retrospective.md"
        section = [
            "",
            f"## Retrospective candidates — generated {datetime.now().strftime('%Y-%m-%d')}",
            "",
            f"Decisions older than {args.days} days with empty Outcome. Fill in what actually happened.",
            "",
        ]
        for c in candidates:
            section.append(f"### {c['title']} ({c['age_days']}d old)")
            section.append("")
            section.append(f"- File: `{c['path']}`")
            section.append(f"- Stakes: {c.get('stakes', 'not set')}")
            section.append(f"- Decision date: {c.get('decision_date', 'not set')}")
            section.append("- **Outcome:** [fill in: did this go well, badly, mixed? what did you learn?]")
            section.append("- **Pattern tag:** [fill in: which recurring pattern does this exemplify?]")
            section.append("")
        existing = retro_path.read_text(encoding="utf-8") if retro_path.is_file() else ""
        retro_path.write_text(existing + "\n".join(section), encoding="utf-8")
        if not args.quiet:
            print(f"\nAppended {len(candidates)} entries to {retro_path}")

    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
