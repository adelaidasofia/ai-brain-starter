"""Human-review report for graphify's typed-edges output (Part A.5).

Part A.5 (``wire_typed_relationships.py``) computes real typed edges from
frontmatter + wikilinks, but graphify's build pipeline never reads that
output — it's only ever used as an LLM skip-list (see SKILL.md). This script
does NOT auto-merge the data into graph.json. Two things make a blind merge
unsafe on a real vault:

1. Node IDs in graph.json are LLM-improvised per entity (Part B), not a
   deterministic function of the file path — resolving a wikilink's raw text
   to the right node needs a lookup, and some files produce more than one
   entity node, so a file-stem match can be ambiguous.
2. The existing wikilink-derived edge types (``mentions``, ``journaled_about``,
   ``attended``, ``investor_for``) already appear in graph.json via Part B,
   with a consistent direction: entity --relation--> document (the mentioned
   thing points at what mentions it). Part A.5 naturally reads the other way
   (document -> wikilink target). Merging without reversing would contradict
   the existing convention instead of extending it.

So this script resolves what it safely can, flags what it can't, and writes
a Markdown report (``graphify-out/TYPED_EDGES_REVIEW.md``, same review-then-apply
shape as graphify's own WIKILINK_GAPS.md) for a human to read before anything
touches the live graph. Nothing here writes to graph.json.

Usage:
    python3 review_typed_edges.py [--graph graphify-out/graph.json]
                                   [--edges graphify-out/.graphify_typed_edges.jsonl]
                                   [--output graphify-out/TYPED_EDGES_REVIEW.md]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# The four wikilink-derived types below are exactly the ones
# wire_typed_relationships.py emits from wikilinks, and Part B already writes
# them into graph.json with an entity->document direction. Part A.5 reads them
# the other way (document -> wikilink target), so they get flipped here.
# The frontmatter-derived types (works_at, floor_at, governs, created_on) have
# no existing precedent in graph.json to contradict, so their direction is left
# as Part A.5 emitted it (src->dst).
_REVERSE_FOR_CONVENTION = {"mentions", "journaled_about", "attended", "investor_for"}

_CONFIDENCE_MAP = {"high": ("EXTRACTED", 1.0), "medium": ("INFERRED", 0.6), "low": ("AMBIGUOUS", 0.3)}


def _stem_index(graph: dict) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = defaultdict(list)
    for node in graph.get("nodes", []):
        sf = node.get("source_file")
        if not sf:
            continue
        stem = Path(sf).stem.lower()
        index[stem].append(node)
    return index


def _existing_pairs(graph: dict) -> set[tuple[str, str, str]]:
    pairs: set[tuple[str, str, str]] = set()
    for link in graph.get("links", []):
        s, t, r = link.get("source"), link.get("target"), link.get("relation")
        if s and t and r:
            pairs.add((s, t, r))
    return pairs


def _load_jsonl(path: Path) -> list[dict]:
    edges = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                edges.append(json.loads(line))
    return edges


def _classify(edges: list[dict], index: dict[str, list[dict]], existing: set) -> dict:
    buckets: dict[str, list[dict]] = {
        "ready": [], "ambiguous": [], "dst_unresolved": [], "src_unresolved": [], "duplicate": [],
    }
    for e in edges:
        src_candidates = index.get(e["src"].lower(), [])
        dst_candidates = index.get(e["dst"].lower(), [])

        if not src_candidates:
            buckets["src_unresolved"].append(e)
            continue
        if not dst_candidates:
            buckets["dst_unresolved"].append(e)
            continue
        if len(src_candidates) > 1 or len(dst_candidates) > 1:
            buckets["ambiguous"].append({**e, "_src_candidates": src_candidates, "_dst_candidates": dst_candidates})
            continue

        src_id = src_candidates[0]["id"]
        dst_id = dst_candidates[0]["id"]
        reverse = e["edge_type"] in _REVERSE_FOR_CONVENTION
        final_source, final_target = (dst_id, src_id) if reverse else (src_id, dst_id)

        if (final_source, final_target, e["edge_type"]) in existing:
            buckets["duplicate"].append(e)
            continue

        conf_label, conf_score = _CONFIDENCE_MAP.get(e["confidence"], ("AMBIGUOUS", 0.3))
        buckets["ready"].append({
            **e,
            "_final_source": final_source, "_final_target": final_target,
            "_reversed": reverse, "_confidence_label": conf_label, "_confidence_score": conf_score,
        })
    return buckets


def _write_report(buckets: dict, output: Path, total: int) -> None:
    lines = []
    lines.append("---")
    lines.append("type: report")
    lines.append("---")
    lines.append("")
    lines.append("# Typed Edges Review")
    lines.append("")
    lines.append(f"Part A.5 computed {total} typed edges. Nothing here has touched graph.json —")
    lines.append("this is a review report only, matching graphify's own WIKILINK_GAPS.md pattern.")
    lines.append("")
    lines.append("| Bucket | Count | Meaning |")
    lines.append("|---|---|---|")
    lines.append(f"| Ready to merge | {len(buckets['ready'])} | Both endpoints resolve to exactly one node, not already in graph.json |")
    lines.append(f"| Ambiguous | {len(buckets['ambiguous'])} | src or dst matches more than one node (same-titled files) — needs a human pick |")
    lines.append(f"| Already covered | {len(buckets['duplicate'])} | Same edge (after direction fix) already exists via Part B |")
    lines.append(f"| dst not found | {len(buckets['dst_unresolved'])} | Wikilink/frontmatter target has no matching note in the graph |")
    lines.append(f"| src not found | {len(buckets['src_unresolved'])} | Source file has no node in the graph (renamed/moved/excluded since Part A.5 ran) |")
    lines.append("")

    by_type = Counter(e["edge_type"] for e in buckets["ready"])
    if by_type:
        lines.append("## Ready to merge, by type")
        lines.append("")
        for et, count in by_type.most_common():
            reversed_note = " (direction flipped to match existing convention)" if et in _REVERSE_FOR_CONVENTION else ""
            lines.append(f"- **{et}**: {count}{reversed_note}")
        lines.append("")

    lines.append("## Ready to merge — sample")
    lines.append("")
    lines.append("| src | dst | type | confidence | final edge (source -> target) |")
    lines.append("|---|---|---|---|---|")
    for e in buckets["ready"][:200]:
        arrow = f"{e['_final_source']} -> {e['_final_target']}"
        lines.append(f"| {e['src']} | {e['dst']} | {e['edge_type']} | {e['_confidence_label']} | {arrow} |")
    if len(buckets["ready"]) > 200:
        lines.append(f"| ... | ... | ... | ... | ({len(buckets['ready']) - 200} more, truncated) |")
    lines.append("")

    if buckets["ambiguous"]:
        lines.append("## Ambiguous — needs a human pick")
        lines.append("")
        lines.append("| src | dst | type | src candidates | dst candidates |")
        lines.append("|---|---|---|---|---|")
        for e in buckets["ambiguous"][:100]:
            src_ids = ", ".join(n["id"] for n in e["_src_candidates"])
            dst_ids = ", ".join(n["id"] for n in e["_dst_candidates"])
            lines.append(f"| {e['src']} | {e['dst']} | {e['edge_type']} | {src_ids} | {dst_ids} |")
        if len(buckets["ambiguous"]) > 100:
            lines.append(f"| ... | ... | ... | ... | ({len(buckets['ambiguous']) - 100} more, truncated) |")
        lines.append("")

    dst_gap = Counter(e["dst"] for e in buckets["dst_unresolved"])
    if dst_gap:
        lines.append("## dst not found — most frequent missing targets")
        lines.append("")
        lines.append("| target | times referenced |")
        lines.append("|---|---|")
        for target, count in dst_gap.most_common(40):
            lines.append(f"| {target} | {count} |")
        lines.append("")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", default="graphify-out/graph.json")
    parser.add_argument("--edges", default="graphify-out/.graphify_typed_edges.jsonl")
    parser.add_argument("--output", default="graphify-out/TYPED_EDGES_REVIEW.md")
    args = parser.parse_args(argv)

    graph_path = Path(args.graph).expanduser()
    edges_path = Path(args.edges).expanduser()
    output_path = Path(args.output).expanduser()

    if not graph_path.is_file():
        print(f"ERROR: graph not found: {graph_path}", file=sys.stderr)
        return 2
    if not edges_path.is_file():
        print(f"ERROR: typed edges not found: {edges_path}", file=sys.stderr)
        return 2

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    edges = _load_jsonl(edges_path)
    index = _stem_index(graph)
    existing = _existing_pairs(graph)
    buckets = _classify(edges, index, existing)

    _write_report(buckets, output_path, len(edges))

    print(f"{len(edges)} typed edges reviewed:")
    for name in ("ready", "ambiguous", "duplicate", "dst_unresolved", "src_unresolved"):
        print(f"  {name}: {len(buckets[name])}")
    print(f"report: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
