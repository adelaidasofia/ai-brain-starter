#!/usr/bin/env python3
"""
graphify_dedupe_by_adjacency.py — second-pass canonicalization that catches
extraction duplicates `graphify_canonicalize.py` misses.

Why this exists:
    `graphify_canonicalize.py` merges nodes by NORMALIZED LABEL. It cannot
    catch the case where the same conceptual file appears under two different
    labels — e.g. a brain-dump file titled by its first sentence + a separately
    written canonical doc that summarizes it. Different labels, different
    source files, identical adjacency. Canonicalize had no way to merge them.
    On 2026-04-11 we found 6 such pairs in a real vault — every `c_<sentence>`
    had a `file_<canonical>` with adjacency Jaccard 1.0. Manual cleanup was
    expensive; this script automates it.

What this script does:
    1. Loads graph.json
    2. For every pair of nodes (n1, n2) where both have ≥ MIN_DEGREE edges,
       computes adjacency Jaccard
    3. If jaccard >= JACCARD_THRESHOLD AND the labels share ≥ MIN_LABEL_OVERLAP
       stemmed words, treats them as duplicates (the label-overlap guard
       filters out coincidental adjacency where two unrelated concepts
       happen to live in the same small set of files)
    4. Picks a canonical winner: file_* over c_*, then non-truncated over
       truncated, then shorter labels
    5. Merges: rewrites edges, dedupes (src, tgt, relation), drops self-loops,
       promotes the loser's label as an alias on the canonical, preserves the
       loser's source_file as merged_source_files
    6. Writes graph.json (with timestamped backup) and prints a merge report

Wiring:
    Designed to run as a post-canonicalize Step 3.5 inside your graphify
    pipeline finishing script. After canonicalize merges by label, this pass
    catches the residue. Add it to your version of graphify_stage_finish.py
    between the merge step and the report regen step.

Usage:
    python3 graphify_dedupe_by_adjacency.py <graph.json> [--dry-run] [--threshold 0.95]

The default threshold (0.95) is conservative — only catches near-perfect
duplicates. Lower it (e.g. 0.85) to catch more aggressive merges (review
the dry-run output first before lowering).

Tested on a real vault with 100% precision and recall on a known 6-duplicate
test set. Filtered out a known false-positive case (two unrelated concepts
sharing 5 neighbors at jaccard 1.0) via the label overlap guard.
"""

import sys
import argparse
import json
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

JACCARD_THRESHOLD = 0.95
MIN_DEGREE = 8  # general floor — don't merge low-degree nodes (too noisy a signal)
MIN_DEGREE_FILE_CANONICAL = 5  # relaxed floor when one node is `file_*` (strong canonical signal — file_* IDs come from properly-named files)
MIN_LABEL_OVERLAP = 0.15  # labels must share at least 15% of stemmed words (catches "co-founder" ↔ "cofounder" cases via stemming, while still filtering coincidental adjacency overlap)

# Stop words filtered out before comparing labels
STOP = set(
    "a an the of for and or but to in on at by with from is are was were be been "
    "this that these those i you they we he she it as how what why when where which "
    "do does did so not no yes if then than".split()
)


def label_words(label: str):
    """Lowercase, stemmed word tokens minus stop words. Joins hyphenated words
    so 'co-founder' and 'cofounder' tokenize identically, then drops trailing
    's' for crude plural handling."""
    import re

    label = (label or "").lower()
    # Collapse hyphens within words: "co-founder" -> "cofounder"
    label = re.sub(r"(\w)-(\w)", r"\1\2", label)
    tokens = re.findall(r"[A-Za-z0-9]+", label)
    out = set()
    for t in tokens:
        if not t or t in STOP or len(t) <= 1:
            continue
        # Crude stem: drop trailing 's' on words >3 chars
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.add(t)
    return out


def labels_compatible(a_label: str, b_label: str, min_overlap: float) -> bool:
    """True if the two labels share enough non-stop words to plausibly be the same concept."""
    a_words = label_words(a_label)
    b_words = label_words(b_label)
    if not a_words or not b_words:
        return False
    overlap = len(a_words & b_words) / max(1, min(len(a_words), len(b_words)))
    return overlap >= min_overlap


def adjacency_map(edges, edge_field="links"):
    """Returns dict[node_id, set[neighbor_id]] from edge list."""
    adj = defaultdict(set)
    for e in edges:
        s = e.get("source", e.get("from"))
        t = e.get("target", e.get("to"))
        if s and t:
            adj[s].add(t)
            adj[t].add(s)
    return adj


def pick_canonical(a, b):
    """Return (canonical_id, duplicate_id). Rules in priority order."""
    aid, alabel = a["id"], a.get("label", "")
    bid, blabel = b["id"], b.get("label", "")

    # Rule 1: file_* beats c_* (file_* IDs come from properly-named files,
    #         c_* IDs come from extracted concepts which often inherit messy titles)
    if aid.startswith("file_") and not bid.startswith("file_"):
        return aid, bid
    if bid.startswith("file_") and not aid.startswith("file_"):
        return bid, aid

    # Rule 2: non-truncated label beats truncated
    a_trunc = alabel.endswith("…") or alabel.endswith("...")
    b_trunc = blabel.endswith("…") or blabel.endswith("...")
    if a_trunc and not b_trunc:
        return bid, aid
    if b_trunc and not a_trunc:
        return aid, bid

    # Rule 3: shorter label wins (canonical concepts are usually noun phrases)
    if len(alabel) != len(blabel):
        return (aid, bid) if len(alabel) < len(blabel) else (bid, aid)

    # Rule 4: stable tie-break by id
    return (aid, bid) if aid < bid else (bid, aid)


def find_duplicate_pairs(nodes, edges, threshold, min_degree):
    """Returns list of (canonical_id, duplicate_id, jaccard, info) tuples."""
    by_id = {n.get("id"): n for n in nodes}
    adj = adjacency_map(edges)

    # Build candidate set with the relaxed file_* floor: nodes with
    # degree >= MIN_DEGREE_FILE_CANONICAL are eligible only as part of a pair
    # with a file_* node; nodes with degree >= min_degree are eligible always.
    relaxed_floor = min(MIN_DEGREE_FILE_CANONICAL, min_degree)
    candidates = [nid for nid, neighbors in adj.items() if len(neighbors) >= relaxed_floor]
    candidates.sort()

    pairs = []
    seen = set()  # track merged-away ids so we don't double-merge
    for i, a_id in enumerate(candidates):
        if a_id in seen:
            continue
        a_n = adj[a_id]
        a_deg = len(a_n)
        a_is_file = a_id.startswith("file_")
        for b_id in candidates[i + 1 :]:
            if b_id in seen:
                continue
            b_n = adj[b_id]
            b_deg = len(b_n)
            b_is_file = b_id.startswith("file_")
            # Effective floor: relaxed if at least one side is a file_* canonical
            either_file = a_is_file or b_is_file
            floor = MIN_DEGREE_FILE_CANONICAL if either_file else min_degree
            if a_deg < floor or b_deg < floor:
                continue
            inter = a_n & b_n
            union = a_n | b_n
            if not union:
                continue
            jaccard = len(inter) / len(union)
            if jaccard >= threshold:
                a_node = by_id.get(a_id, {"id": a_id})
                b_node = by_id.get(b_id, {"id": b_id})
                # SAFETY GUARD: labels must share at least MIN_LABEL_OVERLAP
                # stemmed words. Catches the false-positive case where two
                # genuinely-different concepts both get mentioned in the same
                # small set of files and end up with identical adjacency.
                if not labels_compatible(
                    a_node.get("label", ""),
                    b_node.get("label", ""),
                    MIN_LABEL_OVERLAP,
                ):
                    continue
                canon, dup = pick_canonical(a_node, b_node)
                pairs.append(
                    {
                        "canonical": canon,
                        "duplicate": dup,
                        "jaccard": round(jaccard, 3),
                        "canonical_label": by_id.get(canon, {}).get("label", "?"),
                        "duplicate_label": by_id.get(dup, {}).get("label", "?"),
                    }
                )
                seen.add(dup)
    return pairs


def apply_merges(g, pairs):
    """Mutates g in place. Returns (nodes_removed, edges_removed, self_loops, deduped)."""
    nodes = g["nodes"]
    edge_field = "links" if "links" in g else "edges"
    edges = g[edge_field]
    by_id = {n.get("id"): n for n in nodes}

    merge_map = {p["duplicate"]: p["canonical"] for p in pairs}

    # Promote duplicate metadata onto canonical
    for p in pairs:
        dup_node = by_id.get(p["duplicate"])
        canon_node = by_id.get(p["canonical"])
        if not dup_node or not canon_node:
            continue
        dup_label = dup_node.get("label", "")
        if dup_label:
            aliases = canon_node.setdefault("aliases", [])
            if dup_label not in aliases:
                aliases.append(dup_label)
        dup_src = dup_node.get("source_file") or dup_node.get("source")
        canon_src = canon_node.get("source_file") or canon_node.get("source")
        if dup_src and dup_src != canon_src:
            merged_srcs = canon_node.setdefault("merged_source_files", [])
            if dup_src not in merged_srcs:
                merged_srcs.append(dup_src)

    # Rewrite edges
    def remap(nid):
        return merge_map.get(nid, nid)

    new_edges = []
    seen_keys = set()
    self_loops = 0
    deduped = 0

    for e in edges:
        src_key = "source" if "source" in e else "from"
        tgt_key = "target" if "target" in e else "to"
        s = remap(e[src_key])
        t = remap(e[tgt_key])
        if s == t:
            self_loops += 1
            continue
        rel = e.get("relation", e.get("relationship", ""))
        key = (s, t, rel)
        if key in seen_keys:
            deduped += 1
            continue
        seen_keys.add(key)
        e2 = dict(e)
        e2[src_key] = s
        e2[tgt_key] = t
        new_edges.append(e2)

    # Drop merged-away nodes
    new_nodes = [n for n in nodes if n.get("id") not in merge_map]

    # Rewrite hyperedge member references
    for h in g.get("hyperedges", []):
        for k in ("members", "nodes"):
            if k in h:
                remapped = [remap(m) for m in h[k]]
                # Dedupe within a single hyperedge
                seen_m = set()
                clean = []
                for m in remapped:
                    if m not in seen_m:
                        seen_m.add(m)
                        clean.append(m)
                h[k] = clean

    nodes_removed = len(nodes) - len(new_nodes)
    edges_removed = len(edges) - len(new_edges)

    g["nodes"] = new_nodes
    g[edge_field] = new_edges
    return nodes_removed, edges_removed, self_loops, deduped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("graph_path", help="Path to graph.json")
    ap.add_argument("--dry-run", action="store_true", help="Don't write the output, just print what would change")
    ap.add_argument("--threshold", type=float, default=JACCARD_THRESHOLD, help=f"Jaccard threshold (default {JACCARD_THRESHOLD})")
    ap.add_argument("--min-degree", type=int, default=MIN_DEGREE, help=f"Skip nodes with degree < this (default {MIN_DEGREE})")
    args = ap.parse_args()

    path = Path(args.graph_path)
    g = json.loads(path.read_text())
    edges_field = "links" if "links" in g else "edges"
    print(f"Loaded {len(g['nodes'])} nodes / {len(g[edges_field])} edges from {path}")

    pairs = find_duplicate_pairs(g["nodes"], g[edges_field], args.threshold, args.min_degree)
    print(f"\nFound {len(pairs)} duplicate pairs at jaccard >= {args.threshold}, min_degree {args.min_degree}:")
    for p in pairs:
        print(f"  jaccard={p['jaccard']}")
        print(f"    canonical: {p['canonical']!r} -> {p['canonical_label']!r}")
        print(f"    duplicate: {p['duplicate']!r} -> {p['duplicate_label']!r}")

    if not pairs:
        print("\nNothing to merge. Done.")
        return

    if args.dry_run:
        print("\n--dry-run set; not modifying graph.json")
        return

    # Backup
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    backup = path.with_suffix(f".json.backup_{ts}_pre_dedupe")
    shutil.copy2(path, backup)
    print(f"\nBackup: {backup.name}")

    nodes_removed, edges_removed, self_loops, deduped = apply_merges(g, pairs)
    print(f"Removed {nodes_removed} duplicate nodes")
    print(f"Dropped {self_loops} self-loops, deduped {deduped} now-redundant edges")
    print(f"After: {len(g['nodes'])} nodes / {len(g[edges_field])} edges")

    path.write_text(json.dumps(g))
    print(f"Wrote {path}")
    print("\nNext: re-render GRAPH_REPORT.md (your stage_finish rebuild step) and run /graphify --update so the cache picks up the canonicalized state.")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
