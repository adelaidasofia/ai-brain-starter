#!/usr/bin/env python3
"""
resolver-conflict-report.py is a standalone JSON conflict report.

Reuses the conflict-detection logic in resolver-build.py without writing
RESOLVER.md. Cron-friendly: runs read-only, prints a single JSON document
to stdout, exits 0 when clean, exits 2 when conflicts surface (so a
scheduled task can alert).

JSON shape:
  {
    "vault_root": "...",
    "checked_at": "YYYY-MM-DD",
    "rule_count": <int>,
    "conflict_count": <int>,
    "branch_merge_count": <int>,
    "conflicts": [
      {
        "pattern": "...",
        "members": ["rule_id_a", "rule_id_b", ...],
        "winner_rule_id": "...",
        "winner_source": "...",
        "winner_last_verified": "...",
        "superseded": [
          {"rule_id": "...", "source_path": "...", "last_verified": "..."},
          ...
        ]
      },
      ...
    ],
    "branch_merges": [
      {
        "pattern": "...",
        "branch_a": {"rule_id", "source_path", "last_verified",
                     "outcome", "type"},
        "branch_b": {...},
        "delta_days": <int>
      },
      ...
    ]
  }

Usage:
  python3 scripts/resolver-conflict-report.py --vault-root PATH

Exit codes:
  0 = clean (no conflicts and no branch merges)
  1 = error (e.g. Meta folder not found)
  2 = conflicts or branch-merge candidates surfaced

Stdlib + PyYAML only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

# Reuse helpers from the aggregator. The hyphenated module name forces
# us to import via importlib.
import importlib.util


def _load_aggregator():
    here = Path(__file__).resolve().parent
    src = here / "resolver-build.py"
    if not src.is_file():
        print(f"ERROR: resolver-build.py not found at {src}", file=sys.stderr)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("resolver_build", src)
    if spec is None or spec.loader is None:
        print("ERROR: could not load resolver-build.py", file=sys.stderr)
        sys.exit(1)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _strip_unserializable(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop fields the helper carries that cannot be JSON-encoded."""
    cleaned = []
    for r in rules:
        copy = dict(r)
        copy.pop("subject_tokens", None)
        copy.pop("last_verified_date", None)
        copy.pop("abs_path", None)
        cleaned.append(copy)
    return cleaned


def _strip_branch_merge_internal(branches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the abs_path side-channel from branch-merge records before JSON."""
    out: list[dict[str, Any]] = []
    for bm in branches:
        copy = dict(bm)
        for side in ("branch_a", "branch_b"):
            if isinstance(copy.get(side), dict):
                inner = dict(copy[side])
                inner.pop("abs_path", None)
                copy[side] = inner
        out.append(copy)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detect rule conflicts (same pattern + overlapping subject) "
            "across Meta/Decisions/, Meta/Workflows/, Meta/Exceptions/, "
            "Meta/Facts/. Print a JSON report. Read-only."
        )
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=Path(os.environ.get("VAULT_ROOT", Path.cwd())),
        help="Vault root containing the Meta folder.",
    )
    parser.add_argument(
        "--include-rules",
        action="store_true",
        help="Include the full rule list in the JSON output.",
    )
    args = parser.parse_args()

    vault_root = args.vault_root.resolve()
    aggregator = _load_aggregator()
    meta_dir = aggregator.find_meta_dir(vault_root)
    if meta_dir is None:
        print(
            json.dumps({
                "error": f"no Meta folder under {vault_root}",
                "vault_root": str(vault_root),
            }),
        )
        return 1

    rules = aggregator.collect_rules(meta_dir)
    conflicts = aggregator.detect_conflicts(rules)
    branch_merges = aggregator.detect_branch_merges(rules)

    report: dict[str, Any] = {
        "vault_root": str(vault_root),
        "checked_at": dt.date.today().isoformat(),
        "rule_count": len(rules),
        "conflict_count": len(conflicts),
        "branch_merge_count": len(branch_merges),
        "conflicts": conflicts,
        "branch_merges": _strip_branch_merge_internal(branch_merges),
    }
    if args.include_rules:
        report["rules"] = _strip_unserializable(rules)

    print(json.dumps(report, indent=2, default=str))

    return 2 if (conflicts or branch_merges) else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
