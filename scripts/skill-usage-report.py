#!/usr/bin/env python3
"""
skill-usage-report.py
Reads the skill usage JSONL log and generates a Markdown report
with usage statistics, written to the vault as an Obsidian note.

Usage:
  python3 skill-usage-report.py
  python3 skill-usage-report.py --vault-root /path/to/vault
  python3 skill-usage-report.py --log-file /path/to/log.jsonl --report-file /path/to/report.md
"""

import sys
import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def detect_vault_root() -> Path:
    """Detect vault root from $VAULT_ROOT env var or script location."""
    env_root = os.environ.get("VAULT_ROOT")
    if env_root:
        return Path(env_root)
    # Fall back to script location (expects <vault>/⚙️ Meta/scripts/)
    script_dir = Path(__file__).resolve().parent
    candidate = script_dir.parent.parent
    if (candidate / "⚙️ Meta").is_dir():
        return candidate
    # Last resort: current directory
    return Path.cwd()


def find_default_log_file(vault_root: Path) -> Path:
    """Pick the right log path. Vault-side preferred, ~/.claude fallback.

    Order:
      1. <vault>/⚙️ Meta/skill-usage-log.jsonl (vault-aware install)
      2. <vault>/Meta/skill-usage-log.jsonl (no-emoji install)
      3. ~/.claude/logs/skill-usage.jsonl (vault-less install or stranger)
    """
    candidates = [
        vault_root / "⚙️ Meta" / "skill-usage-log.jsonl",
        vault_root / "Meta" / "skill-usage-log.jsonl",
        Path.home() / ".claude" / "logs" / "skill-usage.jsonl",
    ]
    for c in candidates:
        if c.is_file():
            return c
    # Default to the first vault-side candidate (will be created on first write)
    return candidates[0]


def parse_args():
    vault_root = detect_vault_root()
    parser = argparse.ArgumentParser(
        description="Generate a skill usage report from JSONL log."
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=vault_root,
        help="Path to vault root (default: auto-detected)",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Path to skill-usage-log.jsonl (default: <vault>/⚙️ Meta/skill-usage-log.jsonl)",
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        default=None,
        help="Path to output report (default: <vault>/⚙️ Meta/Skill Usage Report.md)",
    )
    args = parser.parse_args()
    if args.log_file is None:
        args.log_file = find_default_log_file(args.vault_root)
    if args.report_file is None:
        args.report_file = args.vault_root / "⚙️ Meta" / "Skill Usage Report.md"
    return args


def load_entries(log_file: Path):
    """Load all entries from the JSONL log file."""
    entries = []
    if not log_file.exists():
        return entries
    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entry["_dt"] = datetime.fromisoformat(entry["timestamp"])
                entries.append(entry)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return entries


def time_bucket(hour):
    """Classify an hour into a time-of-day bucket."""
    if 6 <= hour < 12:
        return "Morning (6am-12pm)"
    elif 12 <= hour < 17:
        return "Afternoon (12pm-5pm)"
    elif 17 <= hour < 21:
        return "Evening (5pm-9pm)"
    else:
        return "Night (9pm-6am)"


DAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday"
]


def build_report(entries):
    """Build the Markdown report string."""
    now = datetime.now(timezone.utc)
    today = now.date()

    # --- Total invocations per skill ---
    skill_counts = Counter(e["skill"] for e in entries)
    total = sum(skill_counts.values())

    # --- Last 7 days ---
    seven_days_ago = today - timedelta(days=6)
    daily_counts = defaultdict(int)
    for e in entries:
        d = e["_dt"].date()
        if d >= seven_days_ago:
            daily_counts[d] += 1

    # --- Last 4 weeks ---
    four_weeks_ago = today - timedelta(weeks=4)
    weekly_counts = defaultdict(int)
    for e in entries:
        d = e["_dt"].date()
        if d >= four_weeks_ago:
            iso_year, iso_week, _ = d.isocalendar()
            weekly_counts[f"{iso_year}-W{iso_week:02d}"] += 1

    # --- Most active day of week ---
    dow_counts = Counter()
    for e in entries:
        dow_counts[e["_dt"].weekday()] += 1
    if dow_counts:
        best_dow = dow_counts.most_common(1)[0]
        best_dow_name = DAY_NAMES[best_dow[0]]
        best_dow_count = best_dow[1]
    else:
        best_dow_name = "N/A"
        best_dow_count = 0

    # --- Most active time of day ---
    tod_counts = Counter()
    for e in entries:
        tod_counts[time_bucket(e["_dt"].hour)] += 1
    if tod_counts:
        best_tod = tod_counts.most_common(1)[0]
        best_tod_name = best_tod[0]
        best_tod_count = best_tod[1]
    else:
        best_tod_name = "N/A"
        best_tod_count = 0

    # --- Assemble ---
    lines = []
    lines.append("---")
    lines.append("type: report")
    lines.append("category: skill-usage")
    lines.append(f"generated: {now.strftime('%Y-%m-%dT%H:%M:%S')}")
    lines.append(f"total_invocations: {total}")
    lines.append(f"unique_skills: {len(skill_counts)}")
    lines.append(f"most_used_skill: {skill_counts.most_common(1)[0][0] if skill_counts else 'N/A'}")
    lines.append("---")
    lines.append("")
    lines.append("# Skill Usage Report")
    lines.append("")
    lines.append(f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")

    # Total per skill
    lines.append("## Invocations per Skill")
    lines.append("")
    lines.append("| Skill | Count |")
    lines.append("|-------|-------|")
    for skill, count in skill_counts.most_common():
        lines.append(f"| {skill} | {count} |")
    if not skill_counts:
        lines.append("| (no data) | 0 |")
    lines.append("")
    lines.append(f"**Total:** {total}")
    lines.append("")

    # Daily (last 7 days)
    lines.append("## Daily Invocations (Last 7 Days)")
    lines.append("")
    lines.append("| Date | Day | Count |")
    lines.append("|------|-----|-------|")
    for i in range(7):
        d = seven_days_ago + timedelta(days=i)
        day_name = DAY_NAMES[d.weekday()]
        count = daily_counts.get(d, 0)
        lines.append(f"| {d.isoformat()} | {day_name} | {count} |")
    lines.append("")

    # Weekly (last 4 weeks)
    lines.append("## Weekly Invocations (Last 4 Weeks)")
    lines.append("")
    lines.append("| Week | Count |")
    lines.append("|------|-------|")
    for week_key in sorted(weekly_counts.keys()):
        lines.append(f"| {week_key} | {weekly_counts[week_key]} |")
    if not weekly_counts:
        lines.append("| (no data) | 0 |")
    lines.append("")

    # Patterns
    lines.append("## Usage Patterns")
    lines.append("")
    lines.append(f"- **Most active day of week:** {best_dow_name} ({best_dow_count} invocations)")
    lines.append(f"- **Most active time of day:** {best_tod_name} ({best_tod_count} invocations)")
    lines.append("")

    # Time of day breakdown
    lines.append("### Time of Day Breakdown")
    lines.append("")
    lines.append("| Period | Count |")
    lines.append("|--------|-------|")
    for period in ["Morning (6am-12pm)", "Afternoon (12pm-5pm)", "Evening (5pm-9pm)", "Night (9pm-6am)"]:
        lines.append(f"| {period} | {tod_counts.get(period, 0)} |")
    lines.append("")

    return "\n".join(lines)


def main():
    args = parse_args()
    entries = load_entries(args.log_file)
    report = build_report(entries)
    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    with open(args.report_file, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Report written to: {args.report_file}")
    print(f"Total entries processed: {len(entries)}")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
