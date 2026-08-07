#!/usr/bin/env python3
"""
curate-skills-surface.py — rank skills by usage and update README + SKILL.md.

Reads ~/.claude/logs/skill-usage.jsonl (or vault Meta log), ranks skills by
invocation count over a configurable window, and emits a markdown block listing
the top N. Optional `--apply` mode patches a managed region in README.md
between BEGIN/END markers so the surface re-ranks as usage data accumulates.

Surfaces:
  - README.md "Most-used skills" badge region (between
    `<!-- top-skills:BEGIN -->` and `<!-- top-skills:END -->`)
  - Optional SKILL.md routing nudge

Read-only by default. Pass `--apply` to write into README.md.

Usage:
  python3 scripts/curate-skills-surface.py
  python3 scripts/curate-skills-surface.py --apply --readme PATH
  python3 scripts/curate-skills-surface.py --top 5 --days 30
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir  # noqa: E402


def load_records(log_paths: list[Path], since_epoch: int) -> list[dict]:
    """Load JSONL records from any of the given paths. Tolerant of multiple schemas."""
    out = []
    for path in log_paths:
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Normalize timestamp: support both `ts` (epoch int) and `timestamp` (ISO)
                    ts = rec.get("ts")
                    if ts is None and rec.get("timestamp"):
                        try:
                            from datetime import datetime
                            ts = int(datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00")).timestamp())
                        except Exception:
                            ts = 0
                    if ts is None:
                        continue
                    if ts >= since_epoch:
                        rec["_ts"] = ts
                        out.append(rec)
        except OSError:
            continue
    return out


def render_badge(top: list[tuple[str, int]]) -> str:
    if not top:
        return "_No usage data yet. Try `/journal`, `/weekly`, or `/deconstruct`._"
    lines = []
    for rank, (name, count) in enumerate(top, 1):
        lines.append(f"{rank}. **`/{name}`** — {count} invocations")
    return "\n".join(lines)


def patch_readme(readme: Path, badge: str) -> bool:
    if not readme.is_file():
        return False
    text = readme.read_text(encoding="utf-8")
    begin_marker = "<!-- top-skills:BEGIN -->"
    end_marker = "<!-- top-skills:END -->"
    new_block = f"{begin_marker}\n{badge}\n{end_marker}"
    if begin_marker in text and end_marker in text:
        # Replace existing region
        new_text = re.sub(
            re.escape(begin_marker) + r".*?" + re.escape(end_marker),
            new_block,
            text,
            count=1,
            flags=re.DOTALL,
        )
    else:
        # Append a new section near the top
        new_text = re.sub(
            r"(^# AI Brain Starter\s*\n)",
            r"\1\n## Most-used skills (auto-updated)\n\n" + new_block + "\n\n",
            text,
            count=1,
        )
    if new_text == text:
        return False
    readme.write_text(new_text, encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--apply", action="store_true",
                    help="patch README.md between top-skills markers")
    ap.add_argument("--readme",
                    default=str(Path(__file__).resolve().parent.parent / "README.md"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    home = Path.home()
    log_paths = [home / ".claude" / "logs" / "skill-usage.jsonl"]
    # Also check vault Meta. Walks UP from cwd looking for a vault root with a
    # Meta folder that contains skill-usage-log.jsonl. Prefers '⚙️ Meta' over
    # plain 'Meta' when both exist (the log lives in the human-rules variant).
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents)[:5]:
        meta = find_meta_dir(parent, prefer_subfolders=("skill-usage-log.jsonl", "Decisions"))
        if meta is None:
            continue
        vault_log = meta / "skill-usage-log.jsonl"
        if vault_log.is_file():
            log_paths.append(vault_log)
            break

    since = int(time.time()) - args.days * 86400
    records = load_records(log_paths, since)
    if not records:
        msg = f"No skill usage data found in the last {args.days} days. " \
              f"Telemetry is opt-in: enable with `cascadeTelemetry: true` in CLAUDE.md."
        if args.json:
            print(json.dumps({"top": [], "message": msg}))
        else:
            print(msg)
        return 0

    counts = Counter(r["skill"] for r in records if r.get("skill"))
    top = counts.most_common(args.top)
    badge = render_badge(top)

    if args.json:
        print(json.dumps({"top": top, "badge": badge}, ensure_ascii=False, indent=2))
        return 0

    print("Top skills (last", args.days, "days):")
    print(badge)

    if args.apply:
        readme = Path(args.readme)
        if patch_readme(readme, badge):
            print(f"\nPatched {readme}")
        else:
            print(f"\nNo changes to {readme} (markers missing or content unchanged).")

    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
