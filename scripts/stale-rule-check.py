#!/usr/bin/env python3
"""
stale-rule-check.py flags typed-memory entries past their freshness window.

A rule is stale when (today - last_verified) > freshness_days. The check
walks the four typed-memory folders under Meta/ (Decisions, Workflows,
Exceptions, Facts), parses YAML frontmatter on every .md file, and reports
entries whose validity-time clock has rolled past the configured horizon.

Exit codes:
  0  no stale rules
  1  hard error (missing Meta folder, etc.)
  2  one or more stale rules surfaced

Usage:
  python3 scripts/stale-rule-check.py --vault-root PATH
  python3 scripts/stale-rule-check.py --vault-root PATH --threshold-days 30
  python3 scripts/stale-rule-check.py --vault-root PATH --json

The --threshold-days flag overrides every rule's per-entry freshness_days
field. Use this for a uniform sweep (e.g., a quarterly audit that flags
anything older than 90 days regardless of declared freshness).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir  # noqa: E402


TYPED_FOLDERS = ("Decisions", "Workflows", "Exceptions", "Facts")
TYPE_BY_FOLDER = {
    "Decisions": "decision",
    "Workflows": "workflow",
    "Exceptions": "exception",
    "Facts": "fact",
}


def parse_frontmatter(text: str) -> dict[str, Any] | None:
    if not text.startswith("---"):
        return None
    m = re.match(r"^---\n(.*?)\n---\s*", text, re.DOTALL)
    if not m:
        return None
    try:
        import yaml  # PyYAML
    except ImportError:
        print("ERROR: PyYAML required (pip install pyyaml)", file=sys.stderr)
        sys.exit(1)
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return _stringify_dates(data)


def _stringify_dates(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _stringify_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify_dates(v) for v in obj]
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, dt.date):
        return obj.isoformat()
    return obj


def parse_iso_date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if len(s) >= 10:
        try:
            return dt.date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def scan(meta_dir: Path, override: int | None) -> tuple[list[dict], list[dict]]:
    """Return (stale_entries, skipped_entries)."""
    today = dt.date.today()
    stale: list[dict] = []
    skipped: list[dict] = []

    for folder_name in TYPED_FOLDERS:
        folder = meta_dir / folder_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = parse_frontmatter(text)
            if fm is None:
                skipped.append({
                    "path": str(path),
                    "reason": "missing or unparseable frontmatter",
                })
                continue

            last_verified = parse_iso_date(fm.get("last_verified"))
            if last_verified is None:
                skipped.append({
                    "path": str(path),
                    "reason": "no last_verified date",
                })
                continue

            if override is not None:
                threshold = override
            else:
                fd = fm.get("freshness_days")
                if not isinstance(fd, int) or fd < 0:
                    skipped.append({
                        "path": str(path),
                        "reason": "no freshness_days set",
                    })
                    continue
                threshold = fd

            age = (today - last_verified).days
            if age > threshold:
                stale.append({
                    "path": str(path),
                    "type": TYPE_BY_FOLDER[folder_name],
                    "last_verified": last_verified.isoformat(),
                    "freshness_days": threshold,
                    "age_days": age,
                    "overdue_days": age - threshold,
                })

    return stale, skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flag typed-memory rules past their freshness horizon."
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=Path(os.environ.get("VAULT_ROOT", Path.cwd())),
    )
    parser.add_argument(
        "--threshold-days",
        type=int,
        default=None,
        help="Override the per-entry freshness_days field with a uniform value.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON report on stdout.",
    )
    args = parser.parse_args()

    meta_dir = find_meta_dir(args.vault_root.resolve())
    if meta_dir is None:
        print(
            f"ERROR: no Meta folder under {args.vault_root}.",
            file=sys.stderr,
        )
        return 1

    stale, skipped = scan(meta_dir, args.threshold_days)

    if args.json:
        print(json.dumps({
            "vault_root": str(args.vault_root.resolve()),
            "today": dt.date.today().isoformat(),
            "threshold_override": args.threshold_days,
            "stale_count": len(stale),
            "skipped_count": len(skipped),
            "stale": stale,
            "skipped": skipped,
        }, indent=2))
    else:
        if stale:
            print(f"STALE rules: {len(stale)}")
            for entry in stale:
                print(
                    f"  [{entry['type']}] {entry['path']} "
                    f"(last_verified={entry['last_verified']}, "
                    f"age={entry['age_days']}d, "
                    f"overdue={entry['overdue_days']}d)"
                )
        else:
            print("No stale rules.")
        if skipped:
            print(f"Skipped: {len(skipped)} entry/entries")

    return 2 if stale else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
