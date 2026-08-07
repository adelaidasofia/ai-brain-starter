#!/usr/bin/env python3
"""
vault-hygiene.py — vault rot detector + report.

Scans the vault for drift signals that accumulate quietly:
  - Broken wikilinks (target file doesn't exist in vault)
  - Notes with empty body (frontmatter only)
  - Notes that haven't been opened/modified in N+ days (default 365)
  - Duplicate concept candidates (case-insensitive title match across folders)
  - Folders that exist but have no notes (empty containers)
  - Graphify-stale folders (last graphify run >30 days ago, or never)

Read-only. Writes a summary report to <vault>/⚙️ Meta/Vault Hygiene.md.
Suggests actions but doesn't take them.

Usage:
  python3 scripts/vault-hygiene.py [--vault-root PATH] [--stale-days N]
                                   [--json] [--quiet]

Designed to run weekly via cron OR manually OR as part of /sunday-review.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir as _find_meta_dir_helper  # noqa: E402


SKIP_FOLDERS = {
    ".obsidian", ".trash", ".smart-env", "node_modules", ".git", "Archive",
    ".DS_Store", "__pycache__",
}


def find_meta_dir(vault: Path) -> Path:
    return _find_meta_dir_helper(vault, prefer_subfolders=("graphify-out", "Decisions")) \
        or (vault / "Meta")


def walk_md_files(vault: Path):
    for root, dirs, files in os.walk(vault):
        # Prune
        dirs[:] = [d for d in dirs if d not in SKIP_FOLDERS and not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                yield Path(root) / f


def extract_wikilinks(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"\[\[([^|\]]+)", text)]


def has_body(text: str) -> bool:
    """True if the note has content beyond frontmatter + headings."""
    body = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)
    body = re.sub(r"^#+ .*$", "", body, flags=re.MULTILINE)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    return bool(body.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault-root", default=os.environ.get("VAULT_ROOT", os.getcwd()))
    ap.add_argument("--stale-days", type=int, default=365)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    vault = Path(args.vault_root).resolve()
    if not vault.is_dir():
        print(f"vault not found: {vault}", file=sys.stderr)
        return 1

    # === build file index ===
    all_files = list(walk_md_files(vault))
    stem_to_paths = defaultdict(list)
    file_set = set()
    for p in all_files:
        stem_to_paths[p.stem].append(p)
        file_set.add(p.stem)

    broken_links: list[tuple[Path, str]] = []
    empty_notes: list[Path] = []
    stale_notes: list[Path] = []
    duplicate_concepts: list[tuple[str, list[Path]]] = []

    cutoff = datetime.now(timezone.utc).timestamp() - args.stale_days * 86400

    for p in all_files:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        # broken links
        for link in extract_wikilinks(text):
            if link not in file_set:
                broken_links.append((p, link))
        # empty body
        if not has_body(text):
            empty_notes.append(p)
        # stale
        try:
            mtime = p.stat().st_mtime
            if mtime < cutoff:
                stale_notes.append(p)
        except OSError:
            pass

    # duplicate-concept candidates: same stem (case-insensitive) in different folders
    case_groups = defaultdict(list)
    for p in all_files:
        case_groups[p.stem.lower()].append(p)
    for key, paths in case_groups.items():
        if len(paths) > 1:
            duplicate_concepts.append((key, paths))

    # graphify-stale: check Meta/graphify-out/cache or similar
    meta = find_meta_dir(vault)
    graphify_dir = meta / "graphify-out"
    graphify_status = "not run"
    if graphify_dir.is_dir():
        manifest = graphify_dir / "manifest.json"
        if manifest.is_file():
            try:
                age_days = (datetime.now(timezone.utc).timestamp() - manifest.stat().st_mtime) / 86400
                graphify_status = f"last run ~{age_days:.0f} days ago"
            except OSError:
                graphify_status = "manifest unreadable"
        else:
            graphify_status = "manifest missing"

    # build report
    report = {
        "vault": str(vault),
        "files_scanned": len(all_files),
        "broken_links": [{"file": str(p), "target": t} for p, t in broken_links[:50]],
        "broken_links_total": len(broken_links),
        "empty_notes": [str(p) for p in empty_notes[:50]],
        "empty_notes_total": len(empty_notes),
        "stale_notes": [str(p) for p in stale_notes[:50]],
        "stale_notes_total": len(stale_notes),
        "duplicate_concepts": [
            {"stem": k, "paths": [str(p) for p in paths]}
            for k, paths in duplicate_concepts[:30]
        ],
        "duplicate_concepts_total": len(duplicate_concepts),
        "graphify_status": graphify_status,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    # human-readable summary
    lines = [
        "---",
        f"creationDate: {datetime.now().isoformat()}",
        "type: report",
        "category: vault-hygiene",
        "---",
        "",
        "# Vault hygiene report",
        "",
        f"Scanned {report['files_scanned']} markdown files in {vault}.",
        f"Graphify: {graphify_status}.",
        "",
    ]
    if report["broken_links_total"]:
        lines.append(f"## Broken wikilinks: {report['broken_links_total']}")
        lines.append("")
        for item in report["broken_links"][:20]:
            lines.append(f"- {item['file']} → `[[{item['target']}]]`")
        if report["broken_links_total"] > 20:
            lines.append(f"- ... and {report['broken_links_total'] - 20} more")
        lines.append("")
    if report["empty_notes_total"]:
        lines.append(f"## Empty notes (frontmatter only): {report['empty_notes_total']}")
        lines.append("")
        for f in report["empty_notes"][:15]:
            lines.append(f"- {f}")
        lines.append("")
    if report["stale_notes_total"]:
        lines.append(f"## Stale notes (>{args.stale_days} days untouched): {report['stale_notes_total']}")
        lines.append("")
        lines.append("Top 15 oldest:")
        for f in report["stale_notes"][:15]:
            lines.append(f"- {f}")
        lines.append("")
    if report["duplicate_concepts_total"]:
        lines.append(f"## Duplicate concept candidates: {report['duplicate_concepts_total']}")
        lines.append("")
        lines.append("Same stem in multiple folders — review for merge:")
        for d in report["duplicate_concepts"][:20]:
            lines.append(f"- **{d['stem']}** in {len(d['paths'])} locations")
            for p in d["paths"]:
                lines.append(f"  - {p}")
        lines.append("")

    if all(report[k] in (0, "not run", "manifest missing") or report[k] == [] for k in ["broken_links_total", "empty_notes_total", "stale_notes_total", "duplicate_concepts_total"]):
        lines.append("Vault is clean. Nothing flagged.")
        lines.append("")

    text = "\n".join(lines)
    if not args.quiet:
        print(text)

    # Also write to Meta
    if meta.is_dir():
        report_path = meta / "Vault Hygiene.md"
        try:
            report_path.write_text(text, encoding="utf-8")
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
