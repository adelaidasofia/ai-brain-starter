#!/usr/bin/env python3
"""
graphify_wikilink_gaps.py — find graph entities worth wikilink-ing.

Reads graph.json, filters to genuine wikilink candidates (people, concepts,
organizations, tools — not sentences or long titles), checks against existing
[[wikilinks]] in the vault, and reports ranked gaps.

Then run graphify_apply_wikilinks.py to interactively approve and apply them.

Usage:
    python3 graphify_wikilink_gaps.py [options]

Options:
    --vault-root PATH       Vault root (default: current directory)
    --graph PATH            Override graph.json path
    --top N                 Max candidates to report (default: 30)
    --min-degree N          Minimum connections to qualify (default: 3)
    --output PATH           Write markdown report here (default: <graph_dir>/WIKILINK_GAPS.md)
    --exclude LABEL [...]   Labels to skip entirely (e.g. your own name)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

SKIP_PARTS = {
    "⚙️ Meta", "Archive", "🗄 Archive", "_review_alternate_drafts",
    ".claude", ".git", ".obsidian", ".trash", "node_modules", "worktrees",
}

# Node types that are file nodes — already map to real vault files, skip them
FILE_TYPES = {"document", "code", "image", "paper", "rationale"}

# Words that typically open sentences (not named concepts)
SENTENCE_STARTERS = {
    "the", "a", "an", "this", "that", "these", "those", "how", "when", "why",
    "what", "if", "because", "since", "there", "it", "they", "we", "you", "i",
    "my", "our", "your", "his", "her", "its", "their", "he", "she", "but",
    "and", "or", "so", "yet", "for", "nor",
}

# Separators that indicate formatting artifacts, not concept names
NOISE_SEPARATORS = {" - ", " → ", " > ", " :: "}

WIKILINK_RE = re.compile(r'\[\[([^\]|#\n]+?)(?:\|([^\]\n]+?))?\]\]')

# Labels to always exclude — set via --exclude or populated at runtime from args
EXCLUDED_LABELS: set[str] = set()


def is_wikilink_candidate(label: str, ntype: str) -> bool:
    """Return True if this graph node looks like a genuine wikilink candidate."""
    # User-supplied exclusions (e.g. vault author's own name)
    if label in EXCLUDED_LABELS or label.lower() in {e.lower() for e in EXCLUDED_LABELS}:
        return False

    # Skip file-type nodes
    if ntype.lower() in FILE_TYPES:
        return False

    # Too long to be a wikilink
    if len(label) > 55:
        return False

    # Path-form labels ([[folder/Name]]) are never valid — they break Obsidian
    # alias resolution. If the graph produced one, skip it.
    if "/" in label:
        return False

    words = label.split()

    # More than 3 words = almost certainly a title, sentence, or LLM-invented phrase
    # Real wikilinks: "angel investing", "Tai Lopez", "Founder Exhaustion Loop" — all ≤3 words
    if len(words) > 3:
        return False

    # First word is a gerund (-ing) = extracted phrase, not a named concept
    # "Designing Peace Through", "Choosing Authenticity Over", "Reframing X as Y"
    if len(words) > 1 and words[0].endswith("ing"):
        return False

    # Sentences end with terminal punctuation
    if label and label[-1] in ".?!,;":
        return False

    # Quoted speech or dialogue
    if '"' in label or "\u201c" in label or "\u201d" in label:
        return False

    # Parenthetical disambiguation added by LLM: "Onde (startup)", "Onde (Company)"
    # These are graph artifacts — the real wikilink target is just "Onde"
    if "(" in label:
        return False

    # Date/timestamp patterns — note titles, never inline mentions
    # "2025-12-19 00-33", "2024-09 Monthly Summary"
    if re.search(r'\b\d{4}-\d{2}\b', label):
        return False

    # Formatting artifacts: "A - B", "A → B"
    for sep in NOISE_SEPARATORS:
        if sep in label:
            return False

    # Sentence-opener words at start of 3+ word phrases, case-insensitive.
    # "When Climbing Becomes Rising", "The People in Your Elevator",
    # "Rest as the Path to Capacity" — all get filtered.
    # Named multi-word concepts the user already uses ("The Framework Series",
    # "Your Project Name") are already wikilinked so they won't appear in
    # this gap report anyway.
    if len(words) >= 3 and words[0].lower() in SENTENCE_STARTERS:
        return False

    # All-lowercase 3-word phrases = extracted prose, not a named concept
    # Exception: 2-word lowercase phrases like "angel investing" are valid
    if len(words) == 3 and label == label.lower():
        return False

    return True


def looks_like_first_name(label: str, ntype: str) -> bool:
    """True if label is a single capitalized word AND the graph typed it as a person."""
    words = label.split()
    return (
        ntype.lower() == "person"
        and len(words) == 1
        and label[0].isupper()
        and not label.isupper()  # not an acronym
        and len(label) >= 3
        and label.isalpha()
    )


def find_graph(vault: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    for candidate in [
        vault / "⚙️ Meta/graphify-out/graph.json",
        vault / "graphify-out/graph.json",
    ]:
        if candidate.exists():
            return candidate
    # Multi-corpus layout: graph inside corpus subfolder
    for child in sorted(vault.iterdir()):
        if child.is_dir() and child.name not in SKIP_PARTS:
            candidate = child / "⚙️ Meta/graphify-out/graph.json"
            if candidate.exists():
                return candidate
    sys.exit("ERROR: graph.json not found. Use --graph <path> to specify location.")


def scan_wikilinks(vault: Path) -> dict[str, int]:
    """
    Return lowercased term -> occurrence count for all wikilink targets AND aliases.
    [[George Trimis|George]] counts both "george trimis" and "george" as linked.
    """
    counts: dict[str, int] = defaultdict(int)
    for md in vault.rglob("*.md"):
        if any(part in SKIP_PARTS for part in md.parts):
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
            for m in WIKILINK_RE.finditer(text):
                counts[m.group(1).strip().lower()] += 1
                if m.group(2):  # alias display text e.g. [[Full Name|Display Text]]
                    counts[m.group(2).strip().lower()] += 1
        except OSError:
            continue
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--vault-root", default=".", metavar="PATH")
    parser.add_argument("--graph", default=None, metavar="PATH")
    parser.add_argument("--top", type=int, default=30, metavar="N")
    parser.add_argument("--min-degree", type=int, default=3, metavar="N")
    parser.add_argument("--output", default=None, metavar="PATH")
    parser.add_argument(
        "--exclude", nargs="+", default=[], metavar="LABEL",
        help="Labels to exclude from candidates (e.g. your own name: --exclude 'Jane Doe' 'Jane')"
    )
    args = parser.parse_args()
    EXCLUDED_LABELS.update(args.exclude)

    vault = Path(args.vault_root).resolve()
    graph_path = find_graph(vault, args.graph)

    print(f"Graph:  {graph_path}")
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes = graph.get("nodes", [])
    edges = graph.get("links", graph.get("edges", []))  # NetworkX stores edges as "links"

    # Compute undirected degree per node id
    degree: dict[str, int] = defaultdict(int)
    for e in edges:
        degree[e.get("source", "")] += 1
        degree[e.get("target", "")] += 1

    # Collect candidates: pass quality filter + min-degree threshold
    candidates: list[dict] = []
    filtered_out = 0
    for n in nodes:
        ntype = n.get("type", "")
        label = n.get("label", "").strip()
        if not label:
            continue
        if not is_wikilink_candidate(label, ntype):
            filtered_out += 1
            continue
        deg = degree.get(n.get("id", ""), 0)
        if deg >= args.min_degree:
            candidates.append({
                "label": label,
                "type": ntype,
                "degree": deg,
                "needs_disambiguation": looks_like_first_name(label, ntype),
            })

    print(f"Vault:  {vault}")
    print(f"Nodes after quality filter: {len(candidates)} kept, {filtered_out} filtered (sentences/titles/noise)")
    print("Scanning vault for existing wikilinks...")

    wikilinks = scan_wikilinks(vault)
    print(f"Unique wikilink targets found: {len(wikilinks)}")

    # Gaps: candidates with 0 existing wikilinks
    all_gaps = [c for c in candidates if wikilinks.get(c["label"].lower(), 0) == 0]
    all_gaps.sort(key=lambda x: -x["degree"])
    total_gaps = len(all_gaps)
    display_gaps = all_gaps[: args.top]

    # Console output
    needs_disambig = [g for g in display_gaps if g["needs_disambiguation"]]
    print(f"\nWikilink Gap Report — top {len(display_gaps)} of {total_gaps} gaps")
    if needs_disambig:
        print(f"⚠ {len(needs_disambig)} look like first names only — will need full name when applying")
    print()
    print(f"{'#':<4} {'Entity':<35} {'Type':<15} {'Connections':<12} {'Note'}")
    print("-" * 75)
    for i, g in enumerate(display_gaps, 1):
        note = "⚠ first name?" if g["needs_disambiguation"] else ""
        print(f"{i:<4} {g['label']:<35} {g['type']:<15} {g['degree']:<12} {note}")

    # Write markdown report (used as input for graphify_apply_wikilinks.py)
    out_path = (
        Path(args.output).resolve()
        if args.output
        else graph_path.parent / "WIKILINK_GAPS.md"
    )
    lines = [
        "---",
        "type: report",
        f"generated: {date.today()}",
        "---",
        "",
        "# Wikilink Gap Report",
        "",
        "High-connection graph entities with no existing `[[wikilinks]]`.",
        f"Showing top {len(display_gaps)} of {total_gaps} gaps (min degree: {args.min_degree}).",
        "Delete rows you don't want, then run `graphify_apply_wikilinks.py` to apply.",
        "",
        "| # | Entity | Type | Connections | Note |",
        "|---|--------|------|-------------|------|",
    ]
    for i, g in enumerate(display_gaps, 1):
        note = "first name?" if g["needs_disambiguation"] else ""
        lines.append(f"| {i} | {g['label']} | {g['type']} | {g['degree']} | {note} |")
    lines += [
        "",
        "*Edit this file to remove terms you don't want, then run `graphify_apply_wikilinks.py --report <this file>`.*",
    ]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport saved: {out_path}")
    print(f"Next: review the report, delete unwanted rows, then run:")
    print(f"  python3 graphify_apply_wikilinks.py --vault-root . --report '{out_path}'")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
