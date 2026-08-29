#!/usr/bin/env python3
"""
demote-stale-procedural.py - Decay path for procedural memory.

Closed-loop learning has a promotion side (episodic -> procedural via
promote-episodic-to-procedural.py) and a decay side. Without a decay side
the procedural surface only grows, and stale rules eventually outnumber
fresh ones. This script handles the decay side without ever auto-deleting
anything: it surfaces decay candidates to a human reviewer who either
re-verifies or acknowledges decay.

Walks <vault-root>/Meta/Workflows/, <vault-root>/Meta/Exceptions/, and
<vault-root>/Meta/Facts/. For each procedural entry, computes
(today - last_verified). When that value is past
multiplier x freshness_days AND the entry shows signs of never having been
confirmed working (empty outcome, no pattern field set), the script writes a
demotion candidate to:

    <vault-root>/Meta/Demotion-Candidates/<sha8>.md

The candidate frontmatter records:
  - type: matches the source type (workflow / exception / fact)
  - memory_class: procedural
  - status: demotion-candidate
  - source_procedural_file: relative path to the source under Meta/
  - reason: stale-no-outcome OR stale-no-pattern
  - days_since_verified: integer count
  - freshness_days: source freshness window
  - multiplier: the multiplier used to compute decay

Operator workflow:
  1. Read the candidate. Decide whether the rule is still alive.
  2. If alive: open the source file, set last_verified to today, optionally
     fill in outcome / pattern. Delete the candidate.
  3. If decayed: change status to archived (or move the source file to an
     archive folder). Delete the candidate.

The script never auto-deletes the source file. Human review is the only
demotion gate.

CLI:
    python3 demote-stale-procedural.py \\
        --vault-root /path/to/vault \\
        --multiplier 2 \\
        --dry-run

Exit codes:
    0  no demotion candidates found, or all already drafted
    0  candidates drafted (this is a normal result, not an error)
    1  hard error (missing Meta folder, etc.)

Stdlib + PyYAML only.
"""
# exit-contract: NOT-A-CHECKER -- writes demotion-candidate files for human
#   review


from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir as _find_meta_dir_helper  # noqa: E402


TYPED_FOLDERS = ("Workflows", "Exceptions", "Facts")
TYPE_BY_FOLDER = {
    "Workflows": "workflow",
    "Exceptions": "exception",
    "Facts": "fact",
}


def find_meta_dir(vault_root: Path) -> Path | None:
    return _find_meta_dir_helper(
        vault_root,
        prefer_subfolders=("Workflows", "Exceptions", "Facts", "Decisions"),
    )


def parse_frontmatter(text: str) -> dict[str, Any] | None:
    if not text.startswith("---"):
        return None
    m = re.match(r"^---\n(.*?)\n---\s*", text, re.DOTALL)
    if not m:
        return None
    if yaml is None:
        return parse_simple_yaml(m.group(1))
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return parse_simple_yaml(m.group(1))
    if not isinstance(data, dict):
        return None
    return _stringify_dates(data)


def parse_simple_yaml(raw: str) -> dict:
    fm: dict = {}
    for line in raw.splitlines():
        if ":" not in line or line.startswith(" ") or line.startswith("-"):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        fm[key] = value
    return fm


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


def render_frontmatter(frontmatter: dict) -> str:
    if yaml is not None:
        return yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    lines = []
    for key, value in frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {json.dumps(item, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def stable_sha8(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def is_field_unset(fm: dict, key: str) -> bool:
    """Field counts as unset when missing, None, empty string, empty list, or empty dict."""
    if key not in fm:
        return True
    value = fm[key]
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def determine_decay_reason(fm: dict) -> str | None:
    """Return decay reason or None if entry is not a decay candidate."""
    outcome_unset = is_field_unset(fm, "outcome")
    pattern_unset = is_field_unset(fm, "pattern")

    if outcome_unset:
        return "stale-no-outcome"
    if pattern_unset:
        return "stale-no-pattern"
    return None


def build_demotion_candidate(
    source_path: Path,
    rel_path: str,
    fm: dict,
    source_type: str,
    days_since_verified: int,
    freshness_days: int,
    multiplier: float,
    reason: str,
) -> tuple[str, dict, str]:
    """Return (filename_stem, frontmatter, body)."""
    seed = f"{rel_path}|{reason}|{days_since_verified}"
    sha8 = stable_sha8(seed)

    captured_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    candidate_fm = {
        "type": source_type,
        "memory_class": "procedural",
        "status": "demotion-candidate",
        "source_procedural_file": rel_path,
        "reason": reason,
        "days_since_verified": days_since_verified,
        "freshness_days": freshness_days,
        "multiplier": multiplier,
        "captured_at": captured_at,
        "provenance": [
            {
                "source_type": "claude-session",
                "source_id": "demote-stale-procedural",
                "captured_at": captured_at,
            }
        ],
    }

    summary_field = (
        fm.get("name")
        or fm.get("claim")
        or fm.get("exception_summary")
        or "(no summary)"
    )

    body_parts = [
        "## Decay signal",
        "",
        f"Source: `{rel_path}`",
        f"Type: `{source_type}`",
        f"Summary: {summary_field}",
        "",
        f"Days since `last_verified`: {days_since_verified}",
        f"`freshness_days` on entry: {freshness_days}",
        f"Multiplier applied: {multiplier}",
        f"Decay reason: `{reason}`",
        "",
        "## Reviewer notes",
        "",
        "This entry is past the configured decay horizon and shows signs of never "
        "having been confirmed working. Two paths:",
        "",
        "1. **Re-verify.** Open the source file, confirm the rule still applies, set "
        "   `last_verified` to today, fill in `outcome` or `pattern` if you have it. "
        "   Delete this candidate.",
        "2. **Acknowledge decay.** Set `status: archived` on the source file (or move "
        "   it to an archive folder). Delete this candidate.",
        "",
        "The script does not auto-delete the source. Human review is the only "
        "demotion gate.",
        "",
    ]

    return sha8, candidate_fm, "\n".join(body_parts)


def scan(
    meta_dir: Path,
    multiplier: float,
    today: dt.date,
) -> list[dict]:
    """Return list of demotion candidate descriptors."""
    candidates: list[dict] = []

    for folder_name in TYPED_FOLDERS:
        folder = meta_dir / folder_name
        if not folder.is_dir():
            continue
        source_type = TYPE_BY_FOLDER[folder_name]
        for path in sorted(folder.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = parse_frontmatter(text)
            if fm is None:
                continue

            last_verified = parse_iso_date(fm.get("last_verified"))
            if last_verified is None:
                continue

            freshness = fm.get("freshness_days")
            if not isinstance(freshness, int) or freshness < 0:
                continue

            age_days = (today - last_verified).days
            decay_horizon = freshness * multiplier
            if age_days <= decay_horizon:
                continue

            reason = determine_decay_reason(fm)
            if reason is None:
                continue

            try:
                rel_path = str(path.relative_to(meta_dir.parent))
            except ValueError:
                rel_path = str(path)

            candidates.append(
                {
                    "path": path,
                    "rel_path": rel_path,
                    "fm": fm,
                    "source_type": source_type,
                    "days_since_verified": age_days,
                    "freshness_days": freshness,
                    "reason": reason,
                }
            )

    return candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Surface stale procedural rules as human-review demotion candidates."
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        required=True,
        help="Path to the vault root (contains a Meta/ folder).",
    )
    parser.add_argument(
        "--multiplier",
        type=float,
        default=2.0,
        help="Decay horizon = multiplier x freshness_days (default 2).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be drafted without writing any files.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress output unless candidates were drafted.",
    )
    args = parser.parse_args(argv)

    vault_root = args.vault_root.expanduser().resolve()
    meta_dir = find_meta_dir(vault_root)
    if meta_dir is None:
        print(f"ERROR: no Meta folder under {vault_root}.", file=sys.stderr)
        return 1

    candidates_dir = vault_root / "Meta" / "Demotion-Candidates"
    today = dt.date.today()

    candidates = scan(meta_dir, args.multiplier, today)
    if not candidates:
        if not args.quiet:
            print("No procedural entries past decay horizon.")
        return 0

    drafted = 0
    skipped = 0
    drafted_messages: list[str] = []

    for c in candidates:
        sha8, fm, body = build_demotion_candidate(
            c["path"],
            c["rel_path"],
            c["fm"],
            c["source_type"],
            c["days_since_verified"],
            c["freshness_days"],
            args.multiplier,
            c["reason"],
        )
        target = candidates_dir / f"{sha8}.md"
        content = "---\n" + render_frontmatter(fm).rstrip() + "\n---\n\n" + body
        if args.dry_run:
            drafted_messages.append(
                f"[dry-run] would write {target} (source={c['rel_path']}, "
                f"reason={c['reason']}, age={c['days_since_verified']}d)"
            )
            drafted += 1
            continue
        if target.exists():
            skipped += 1
            continue
        try:
            candidates_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            drafted_messages.append(
                f"Wrote {target} (source={c['rel_path']}, "
                f"reason={c['reason']}, age={c['days_since_verified']}d)"
            )
            drafted += 1
        except OSError as e:
            print(f"Failed to write {target}: {e}", file=sys.stderr)

    if args.quiet and drafted == 0:
        return 0

    for msg in drafted_messages:
        print(msg)
    print(f"Drafted {drafted} demotion candidate(s). Skipped {skipped} pre-existing.")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
