#!/usr/bin/env python3
"""
Negative-control tests for graphify's typed-edges review report (Step 4b).

Guards the bug class BLIND-MERGE-CORRUPTS-A-LIVE-GRAPH: Part A.5
(wire_typed_relationships.py) computes real typed edges from frontmatter and
wikilinks, but its output is only ever an LLM skip-list -- nothing merges it
into graph.json. Two things make an automatic merge unsafe:

1. Node IDs in graph.json are LLM-improvised per entity (Part B), not a
   deterministic function of the file path. A wikilink's raw text can resolve
   to zero, one, or several nodes.
2. The four wikilink-derived types (mentions, journaled_about, attended,
   investor_for) already exist in graph.json via Part B with an
   entity -> document direction. Part A.5 reads them the other way
   (document -> wikilink target), so merging as-emitted would contradict the
   existing convention rather than extend it.

review_typed_edges.py resolves what it safely can and buckets the rest for a
human. These tests pin the two properties that make it safe to run on a vault:
graph.json is never written, and only unambiguous, non-duplicate, correctly
oriented edges reach the "ready to merge" bucket.

Run: python3 scripts/test_review_typed_edges.py
"""

from __future__ import annotations  # PEP 604 annotations; gate pins Python 3.9

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "graphify" / "scripts"
REVIEW = SCRIPTS / "review_typed_edges.py"

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


GRAPH = {
    "nodes": [
        {"id": "ada_lovelace", "label": "Ada Lovelace", "source_file": "people/Ada Lovelace.md"},
        {"id": "acme_corp", "label": "Acme Corp", "source_file": "companies/Acme Corp.md"},
        {"id": "journal_entry", "label": "A Tuesday", "source_file": "journal/2026-01-05.md"},
        {"id": "dup_one", "label": "Dup One", "source_file": "a/Duplicate.md"},
        {"id": "dup_two", "label": "Dup Two", "source_file": "b/Duplicate.md"},
        {"id": "meeting_note", "label": "Board Meeting", "source_file": "meetings/Board Meeting.md"},
    ],
    # Part B already wrote this one, in the entity -> document direction.
    "links": [
        {"source": "ada_lovelace", "target": "journal_entry", "relation": "journaled_about"},
    ],
}

EDGES = [
    # Already covered: Part A.5 emits document -> entity; after the flip this is
    # exactly the link Part B already wrote.
    {"src": "2026-01-05", "dst": "Ada Lovelace", "edge_type": "journaled_about", "confidence": "medium"},
    # Ready, and must be flipped: wikilink-derived.
    {"src": "Board Meeting", "dst": "Ada Lovelace", "edge_type": "attended", "confidence": "medium"},
    # Ready, and must NOT be flipped: frontmatter-derived.
    {"src": "Ada Lovelace", "dst": "Acme Corp", "edge_type": "works_at", "confidence": "high"},
    # Ambiguous: two notes share the stem "Duplicate".
    {"src": "Board Meeting", "dst": "Duplicate", "edge_type": "mentions", "confidence": "low"},
    # dst has no node in the graph.
    {"src": "Board Meeting", "dst": "Ghost Note", "edge_type": "mentions", "confidence": "low"},
    # src has no node in the graph.
    {"src": "Missing File", "dst": "Acme Corp", "edge_type": "mentions", "confidence": "low"},
]


def run_review(tmp: Path):
    out = tmp / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    graph_path = out / "graph.json"
    edges_path = out / ".graphify_typed_edges.jsonl"
    report_path = out / "TYPED_EDGES_REVIEW.md"

    graph_path.write_text(json.dumps(GRAPH), encoding="utf-8")
    edges_path.write_text("\n".join(json.dumps(e) for e in EDGES) + "\n", encoding="utf-8")
    before = graph_path.read_bytes()

    proc = subprocess.run(
        [sys.executable, str(REVIEW), "--graph", str(graph_path),
         "--edges", str(edges_path), "--output", str(report_path)],
        capture_output=True, text=True,
    )
    return proc, graph_path, report_path, before


def main() -> int:
    print("review_typed_edges.py — negative controls")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc, graph_path, report_path, before = run_review(tmp)

        check("exits 0", proc.returncode == 0, proc.stderr.strip())
        check("writes the report", report_path.is_file())

        # The invariant the whole design rests on.
        check("graph.json is byte-identical afterwards",
              graph_path.read_bytes() == before,
              "the review step wrote to the live graph")

        report = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
        stdout = proc.stdout

        check("counts every bucket", all(
            f"{name}: {n}" in stdout
            for name, n in (("ready", 2), ("ambiguous", 1), ("duplicate", 1),
                            ("dst_unresolved", 1), ("src_unresolved", 1))
        ), stdout.strip())

        # Direction: wikilink-derived flips, frontmatter-derived does not.
        check("wikilink-derived edge is flipped to entity -> document",
              "ada_lovelace -> meeting_note" in report,
              "attended should resolve as entity -> document")
        check("frontmatter-derived edge keeps its direction",
              "ada_lovelace -> acme_corp" in report,
              "works_at should stay src -> dst")
        check("flip is disclosed in the report",
              "direction flipped to match existing convention" in report)

        # Nothing unsafe reaches "ready".
        ready_section = report.split("## Ready to merge — sample")[-1].split("## Ambiguous")[0]
        check("ambiguous target stays out of ready", "Duplicate" not in ready_section)
        check("unresolved dst stays out of ready", "Ghost Note" not in ready_section)
        check("unresolved src stays out of ready", "Missing File" not in ready_section)
        check("edge already written by Part B is not re-listed as ready",
              "journaled_about" not in ready_section,
              "duplicate detection must run AFTER the direction flip")

        # The ambiguous bucket has to show the human both candidates to pick from.
        check("ambiguous row lists both candidate nodes",
              "dup_one" in report and "dup_two" in report)

        # Missing inputs fail loudly instead of writing a half-empty report.
        missing = subprocess.run(
            [sys.executable, str(REVIEW), "--graph", str(tmp / "nope.json"),
             "--edges", str(tmp / "graphify-out" / ".graphify_typed_edges.jsonl"),
             "--output", str(tmp / "out.md")],
            capture_output=True, text=True,
        )
        check("missing graph exits non-zero", missing.returncode != 0)
        check("missing graph explains itself", "ERROR" in missing.stderr)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
