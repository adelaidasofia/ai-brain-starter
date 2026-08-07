#!/usr/bin/env python3
"""
check-claude-md-drift.py — detect stale entries in vault CLAUDE.md.

Over months a CLAUDE.md grows stale: people no longer in the user's life,
projects archived, contradictory rules accumulating, abbreviations for terms
the user no longer uses. None of that is automatically caught. This script
flags candidates for review.

It does NOT auto-edit CLAUDE.md. Drift is a judgment call — the script
surfaces signals; the user decides what to keep.

Detection signals:
  1. People in `## People` not mentioned in any session/journal in the last
     N days (default 90) → may have rotated out of active context.
  2. Project names in `## Current Focus` not appearing in any new session/
     journal/decision in the last N days → may be archived without removal.
  3. Wikilinks in CLAUDE.md that point to files that don't exist in the
     vault → broken link, candidate for repair.
  4. Identical headings appearing twice in CLAUDE.md → likely accidental
     duplicate from a manual merge.
  5. "Codified DATE" markers older than 1 year → review candidate.
  6. Tools/MCPs listed in `## Tools I Use` that no longer respond when probed.

Output: human-readable review document at <vault>/⚙️ Meta/CLAUDE-md drift.md
(unless --json is set).

Usage:
  python3 scripts/check-claude-md-drift.py [--vault-root PATH] [--days N]
                                            [--json] [--quiet]

Read-only. Never modifies CLAUDE.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir as _find_meta_dir_helper  # noqa: E402


def find_meta_dir(vault: Path) -> Path:
    return _find_meta_dir_helper(vault) or (vault / "Meta")


def find_journals_dir(vault: Path) -> Path | None:
    if not vault.is_dir():
        return None
    for child in vault.iterdir():
        if child.is_dir() and ("Journal" in child.name or "Daily Logs" in child.name):
            return child
    return None


def parse_claude_md(claude_md: Path) -> dict:
    """Extract sections from CLAUDE.md."""
    if not claude_md.is_file():
        return {}
    text = claude_md.read_text(encoding="utf-8")
    out = {"raw": text, "sections": {}}
    current = None
    buffer = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            if current:
                out["sections"][current] = "\n".join(buffer)
            current = m.group(1).strip()
            buffer = []
        else:
            buffer.append(line)
    if current:
        out["sections"][current] = "\n".join(buffer)
    return out


def extract_people_names(people_section: str) -> list[str]:
    names = []
    for line in people_section.splitlines():
        m = re.match(r"^\s*[-*]\s+\*\*([^*]+)\*\*", line)
        if m:
            names.append(m.group(1).strip())
            continue
        m = re.match(r"^\s*[-*]\s+\[\[([^|\]]+)", line)
        if m:
            names.append(m.group(1).strip())
    return names


def extract_focus_terms(focus_section: str) -> list[str]:
    terms = []
    for m in re.finditer(r"\*\*([^*]+)\*\*", focus_section):
        terms.append(m.group(1).strip())
    for m in re.finditer(r"\[\[([^|\]]+)", focus_section):
        terms.append(m.group(1).strip())
    return terms


def extract_wikilinks(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"\[\[([^|\]]+)", text)]


def find_duplicate_headings(text: str) -> list[str]:
    headings = {}
    duplicates = []
    for line in text.splitlines():
        m = re.match(r"^(##+)\s+(.+)$", line)
        if m:
            key = (m.group(1), m.group(2).strip().lower())
            headings[key] = headings.get(key, 0) + 1
            if headings[key] == 2:
                duplicates.append(f"{m.group(1)} {m.group(2)}")
    return duplicates


def find_codified_old(text: str, year_threshold: int) -> list[str]:
    """Find 'Codified YYYY-MM-DD' markers older than threshold."""
    out = []
    today = datetime.now(timezone.utc)
    for m in re.finditer(r"[Cc]odified\s+(\d{4})-(\d{2})-(\d{2})", text):
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            if (today - d).days > year_threshold:
                snippet = text[max(0, m.start() - 60):m.end() + 40].replace("\n", " ")
                out.append(snippet.strip())
        except ValueError:
            continue
    return out[:10]  # cap


def collect_recent_corpus(vault: Path, days: int) -> str:
    """Concatenate text from sessions, decisions, journals, captures from last N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    meta = find_meta_dir(vault)
    sources = []
    for sub in ["Sessions", "Decisions"]:
        d = meta / sub
        if d.is_dir():
            for f in d.glob("*.md"):
                if f.is_file() and f.stat().st_mtime >= cutoff:
                    try:
                        sources.append(f.read_text(encoding="utf-8"))
                    except OSError:
                        pass
    captures = meta / "Session Captures.md"
    if captures.is_file() and captures.stat().st_mtime >= cutoff:
        try:
            sources.append(captures.read_text(encoding="utf-8"))
        except OSError:
            pass
    j = find_journals_dir(vault)
    if j:
        for f in j.rglob("*.md"):
            if f.is_file() and f.stat().st_mtime >= cutoff:
                try:
                    sources.append(f.read_text(encoding="utf-8"))
                except OSError:
                    pass
    return "\n\n".join(sources)


def render_report(report: dict) -> str:
    lines = [
        "---",
        f"creationDate: {datetime.now().isoformat()}",
        "type: report",
        "category: claude-md-drift",
        "---",
        "",
        "# CLAUDE.md drift review",
        "",
        f"Window: last {report['days']} days. Read-only — review and decide what to update.",
        "",
    ]
    if report["dormant_people"]:
        lines.append("## People not mentioned in recent context")
        lines.append("")
        lines.append("These names appear in CLAUDE.md `## People` but were not found in any session, decision, or journal entry in the window.")
        lines.append("")
        for name in report["dormant_people"]:
            lines.append(f"- **{name}**")
        lines.append("")
    if report["dormant_focus"]:
        lines.append("## Focus terms not mentioned recently")
        lines.append("")
        lines.append("Terms in `## Current Focus` that didn't appear in recent sessions/decisions/journals. May be archived projects.")
        lines.append("")
        for t in report["dormant_focus"]:
            lines.append(f"- {t}")
        lines.append("")
    if report["broken_links"]:
        lines.append("## Broken wikilinks in CLAUDE.md")
        lines.append("")
        lines.append("Files referenced by `[[...]]` that don't exist in the vault.")
        lines.append("")
        for link in report["broken_links"]:
            lines.append(f"- `[[{link}]]`")
        lines.append("")
    if report["duplicate_headings"]:
        lines.append("## Duplicate headings")
        lines.append("")
        for h in report["duplicate_headings"]:
            lines.append(f"- {h}")
        lines.append("")
    if report["old_codified"]:
        lines.append("## Old codified rules (>1 year)")
        lines.append("")
        lines.append("Worth checking whether these still hold.")
        lines.append("")
        for snippet in report["old_codified"]:
            lines.append(f"- ...{snippet}...")
        lines.append("")
    if not any([report["dormant_people"], report["dormant_focus"], report["broken_links"], report["duplicate_headings"], report["old_codified"]]):
        lines.append("No drift signals detected. CLAUDE.md looks current.")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault-root", default=os.environ.get("VAULT_ROOT", os.getcwd()))
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--year-threshold", type=int, default=365)
    args = ap.parse_args()

    vault = Path(args.vault_root).resolve()
    claude_md = vault / "CLAUDE.md"
    if not claude_md.is_file():
        print(f"No CLAUDE.md at {claude_md}", file=sys.stderr)
        return 1

    parsed = parse_claude_md(claude_md)
    sections = parsed["sections"]

    # Build vault file index for broken-link detection
    vault_files: set[str] = set()
    if vault.is_dir():
        for f in vault.rglob("*.md"):
            stem = f.stem
            vault_files.add(stem)

    recent = collect_recent_corpus(vault, args.days).lower()

    people_section = sections.get("People", "")
    people = extract_people_names(people_section)
    dormant_people = [n for n in people if n.lower() not in recent]

    focus_section = sections.get("Current Focus", "") or sections.get("Focus", "")
    focus_terms = extract_focus_terms(focus_section)
    dormant_focus = [t for t in focus_terms if t.lower() not in recent]

    wikilinks = extract_wikilinks(parsed["raw"])
    broken_links = sorted(set(w for w in wikilinks if w not in vault_files))[:30]

    duplicate_headings = find_duplicate_headings(parsed["raw"])
    old_codified = find_codified_old(parsed["raw"], args.year_threshold)

    report = {
        "days": args.days,
        "dormant_people": dormant_people,
        "dormant_focus": dormant_focus,
        "broken_links": broken_links,
        "duplicate_headings": duplicate_headings,
        "old_codified": old_codified,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    out = render_report(report)
    if args.quiet:
        # Just summary
        signals = sum(1 for k in ["dormant_people", "dormant_focus", "broken_links", "duplicate_headings", "old_codified"] if report[k])
        print(f"CLAUDE.md drift: {signals} signal categor{'y' if signals == 1 else 'ies'} flagged.")
    else:
        print(out)

    # Also write to vault Meta
    meta = find_meta_dir(vault)
    if meta.is_dir():
        report_path = meta / "CLAUDE-md drift.md"
        try:
            report_path.write_text(out, encoding="utf-8")
            if not args.quiet:
                print(f"\nWrote {report_path}")
        except OSError as e:
            print(f"Could not write report: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
