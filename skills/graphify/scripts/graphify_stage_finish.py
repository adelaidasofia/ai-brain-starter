#!/usr/bin/env python3
"""
graphify_stage_finish.py — End-to-end finish for a graphify stage.

Combines per-chunk result JSON files, runs canonicalize, union-merges with the
existing graph.json, reclusters, regenerates a simplified GRAPH_REPORT.md, and
saves the semantic cache.

This script handles API drift in the underlying graphify package: the upstream
report.generate() and analyze.suggest_questions() require community_labels and
other args the bare runbook snippet doesn't pass. We sidestep that by writing a
simplified report by hand, with community labels derived from the highest-degree
node in each community.

Run from your vault root (or pass --vault-root). Requires the graphify pipx env
because it imports `graphify.cluster`, `graphify.analyze`, and the project's
sibling `graphify_canonicalize.py`.

Usage:
    python3 graphify_stage_finish.py \
        --num-chunks 20 \
        --stage-name "stage 2" \
        --token-cost-k 1377
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from collections import Counter
from datetime import date


SLASH_LABEL_RE = re.compile(r"^[\w\s\u00C0-\uFFFF]+/[\w\s\u00C0-\uFFFF]+$")


def clean_slash_label(label: str) -> str:
    """Replace `/` in non-path-shaped labels with comma.

    Strict path-form labels (like `Folder/File`) are left alone for canonicalize
    to handle via strip_folder_prefix. Non-path uses (`Person/Role`,
    `friend/potential`) get rewritten so they pass validation.
    """
    if "/" not in label:
        return label
    if SLASH_LABEL_RE.match(label):
        return label  # path-form, let canonicalize strip it
    # In-parenthesis case: `Name (friend/teammate)` → `Name (friend, teammate)`
    return label.replace("/", ", ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--num-chunks", type=int, required=True)
    ap.add_argument("--stage-name", default="stage")
    ap.add_argument("--token-cost-k", type=int, default=0,
                    help="Reported total LLM token cost for this stage, in thousands")
    ap.add_argument("--vault-root", default=".",
                    help="Vault root directory (default: current working directory)")
    ap.add_argument("--chunk-prefix", default="graphify-out/.chunk_")
    ap.add_argument("--graph-path", default="graphify-out/graph.json")
    args = ap.parse_args()

    vault = Path(args.vault_root).resolve()
    os.chdir(vault)
    sys.path.insert(0, str(Path(__file__).parent))

    # Local import — graphify_canonicalize.py is a sibling script
    from graphify_canonicalize import canonicalize, force_valid_file_types

    print("=" * 60)
    print(f"GRAPHIFY STAGE FINISH — {args.stage_name}")
    print("=" * 60)
    print()

    # === Step 1: combine chunks ===
    print(f"Step 1: combining {args.num_chunks} chunk results...")
    combined_nodes = []
    combined_edges = []
    combined_hyper = []
    label_fixes = 0
    for i in range(1, args.num_chunks + 1):
        p = Path(f"{args.chunk_prefix}{i:02d}_result.json")
        if not p.exists():
            print(f"  MISSING chunk {i:02d} — aborting")
            sys.exit(1)
        data = json.loads(p.read_text())
        for n in data.get("nodes", []):
            old = n.get("label", "")
            new = clean_slash_label(old)
            if new != old:
                n["label"] = new
                label_fixes += 1
        combined_nodes.extend(data.get("nodes", []))
        combined_edges.extend(data.get("edges", []))
        combined_hyper.extend(data.get("hyperedges", []))
    print(f"  combined: {len(combined_nodes)} nodes, "
          f"{len(combined_edges)} edges, {len(combined_hyper)} hyperedges")
    if label_fixes:
        print(f"  auto-cleaned {label_fixes} slash-in-label cosmetics")

    raw_extraction = {
        "nodes": combined_nodes,
        "edges": combined_edges,
        "hyperedges": combined_hyper,
    }
    raw_path = Path(f"graphify-out/.{args.stage_name.replace(' ', '_')}_raw.json")
    raw_path.write_text(json.dumps(raw_extraction, indent=2))
    print(f"  wrote {raw_path}")

    # === Step 2: canonicalize ===
    print()
    print("Step 2: canonicalizing...")
    canon = canonicalize(raw_extraction)
    ft_fixes = force_valid_file_types(canon["nodes"])
    if ft_fixes:
        print(f"  fixed {ft_fixes} invalid file_type values")
    print(f"  canonical: {len(canon['nodes'])} nodes "
          f"({100*(1-len(canon['nodes'])/max(1,len(combined_nodes))):.0f}% reduction), "
          f"{len(canon['edges'])} edges "
          f"({100*(1-len(canon['edges'])/max(1,len(combined_edges))):.0f}% reduction)")

    canon_path = Path(f"graphify-out/.{args.stage_name.replace(' ', '_')}_canon.json")
    canon_path.write_text(json.dumps(canon, indent=2))
    print(f"  wrote {canon_path}")

    # === Step 3: backup + union merge ===
    print()
    print("Step 3: union-merging with existing graph.json...")
    ts = time.strftime("%Y%m%d_%H%M")
    backup = Path(args.graph_path + f".backup_{ts}_pre_{args.stage_name.replace(' ', '_')}_finish")
    backup.write_bytes(Path(args.graph_path).read_bytes())
    print(f"  backed up: {backup}")

    existing = json.loads(open(args.graph_path).read())
    existing_nodes = existing["nodes"]
    existing_edges = existing.get("links", [])
    print(f"  existing: {len(existing_nodes)} nodes, {len(existing_edges)} edges")

    existing_node_ids = {n["id"] for n in existing_nodes}
    existing_node_labels = {
        (n.get("label"), n.get("file_type", "document")): n["id"]
        for n in existing_nodes
    }

    merged_nodes = list(existing_nodes)
    id_remap = {}
    for n in canon["nodes"]:
        nid = n["id"]
        label_key = (n.get("label"), n.get("file_type", "document"))
        if nid in existing_node_ids:
            continue
        if label_key in existing_node_labels:
            id_remap[nid] = existing_node_labels[label_key]
            continue
        merged_nodes.append(n)
        existing_node_ids.add(nid)
        existing_node_labels[label_key] = nid

    added_nodes = len(merged_nodes) - len(existing_nodes)
    print(f"  added {added_nodes} new nodes (remapped {len(id_remap)} via label match)")

    existing_edge_keys = {
        (e.get("source"), e.get("target"), e.get("relation"))
        for e in existing_edges
    }
    merged_edges = list(existing_edges)
    added_edges = 0
    for e in canon["edges"]:
        src = id_remap.get(e["source"], e["source"])
        tgt = id_remap.get(e["target"], e["target"])
        rel = e.get("relation")
        key = (src, tgt, rel)
        if key in existing_edge_keys:
            continue
        new_e = dict(e)
        new_e["source"] = src
        new_e["target"] = tgt
        merged_edges.append(new_e)
        existing_edge_keys.add(key)
        added_edges += 1
    print(f"  added {added_edges} new edges")

    existing_hyper = existing.get("hyperedges", [])
    existing_hyper_ids = {h.get("id") for h in existing_hyper}
    new_hyper = []
    for h in canon.get("hyperedges", []):
        if h.get("id") in existing_hyper_ids:
            continue
        h_copy = dict(h)
        h_copy["nodes"] = [id_remap.get(nid, nid) for nid in h.get("nodes", [])]
        new_hyper.append(h_copy)
        existing_hyper_ids.add(h.get("id"))
    merged_hyper = existing_hyper + new_hyper
    print(f"  added {len(new_hyper)} new hyperedges")

    merged_graph = dict(existing)
    merged_graph["nodes"] = merged_nodes
    merged_graph["links"] = merged_edges
    merged_graph["hyperedges"] = merged_hyper

    print()
    print(f"  MERGED TOTAL: {len(merged_nodes)} nodes, "
          f"{len(merged_edges)} edges, {len(merged_hyper)} hyperedges")
    print(f"  growth: nodes +{added_nodes} "
          f"({100*added_nodes/max(1,len(existing_nodes)):.1f}%), "
          f"edges +{added_edges} ({100*added_edges/max(1,len(existing_edges)):.1f}%)")

    Path(args.graph_path).write_text(json.dumps(merged_graph))
    print(f"  wrote {args.graph_path}")

    # === Step 4: recluster + regenerate report ===
    print()
    print("Step 4: reclustering + regenerating report...")
    from networkx.readwrite import json_graph
    from graphify.cluster import cluster, score_all
    from graphify.analyze import god_nodes, surprising_connections

    G = json_graph.node_link_graph(merged_graph, edges="links")
    print(f"  G: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    communities = cluster(G)
    print(f"  {len(communities)} communities")
    scores = score_all(G, communities)

    # Build community_labels from highest-degree node per community
    # (sidesteps API drift in graphify.report.generate)
    community_labels = {}
    for cid, nids in communities.items():
        best = max(nids, key=lambda n: G.degree(n))
        community_labels[cid] = G.nodes[best].get("label", best)

    gn = god_nodes(G, top_n=20)
    sc = surprising_connections(G, communities, top_n=10)

    confs = Counter(d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True))
    total_e = sum(confs.values())

    today = date.today().isoformat()
    lines = []
    lines.append(f"# Graph Report — {args.stage_name} merge ({today})")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- {G.number_of_nodes()} nodes · {G.number_of_edges()} edges · {len(communities)} communities")
    ext = round(100 * confs.get("EXTRACTED", 0) / max(1, total_e))
    inf = round(100 * confs.get("INFERRED", 0) / max(1, total_e))
    amb = round(100 * confs.get("AMBIGUOUS", 0) / max(1, total_e))
    lines.append(f"- Extraction: {ext}% EXTRACTED · {inf}% INFERRED · {amb}% AMBIGUOUS")
    if args.token_cost_k:
        lines.append(f"- {args.stage_name} token cost: ~{args.token_cost_k}K LLM tokens")
    lines.append("")
    lines.append("## Top God Nodes")
    for i, n in enumerate(gn, 1):
        lines.append(f"{i}. `{n['label']}` — {n.get('degree', n.get('edges', 0))} edges")
    lines.append("")
    lines.append("## Surprising Connections")
    for s in sc[:10]:
        src_label = s.get("source_label", s.get("source", "?"))
        tgt_label = s.get("target_label", s.get("target", "?"))
        rel = s.get("relation", "?")
        conf = s.get("confidence", "?")
        lines.append(f"- `{src_label}` --{rel}--> `{tgt_label}`  [{conf}]")
    lines.append("")
    lines.append("## Top 20 Communities")
    sorted_comms = sorted(communities.items(), key=lambda kv: -len(kv[1]))
    for cid, nids in sorted_comms[:20]:
        label = community_labels[cid]
        cohesion = scores.get(cid, 0.0)
        lines.append(f"### {label} (community {cid}, {len(nids)} nodes, cohesion {cohesion})")
        sample = [G.nodes[nid].get("label", nid) for nid in nids[:8]]
        more = max(0, len(nids) - 8)
        lines.append(f"  {', '.join(sample)}{f' (+{more} more)' if more else ''}")
        lines.append("")

    Path("graphify-out/GRAPH_REPORT.md").write_text("\n".join(lines))
    print(f"  wrote graphify-out/GRAPH_REPORT.md")

    # === Step 5: cache save ===
    print()
    print("Step 5: saving semantic cache...")
    try:
        from graphify.cache import save_semantic_cache
        saved = save_semantic_cache(
            canon["nodes"], canon["edges"], canon.get("hyperedges"), root=Path.cwd()
        )
        print(f"  cached {saved} per-file entries")
    except Exception as e:
        print(f"  WARN: cache save failed: {e}")

    # === Step 5b: verify upgrade ===
    # The cache is keyed by SHA256(content + null + path), so LLM extractions
    # OVERWRITE preflight stubs at the same hash. Directory count stays flat
    # even when work landed. Count entries that were touched in this run AND
    # carry an LLM signature so the operator can audit success in one line.
    print()
    print("Step 5b: verifying upgrade...")
    try:
        from graphify_stage_select import is_llm_extraction
        cache_dir = Path("graphify-out/cache")
        cutoff = time.time() - 3600  # last hour
        upgraded = 0
        recent = 0
        for c in cache_dir.rglob("*.json"):
            if c.stat().st_mtime < cutoff:
                continue
            recent += 1
            try:
                if is_llm_extraction(json.loads(c.read_text())):
                    upgraded += 1
            except Exception:
                pass
        print(f"  {recent} cache entries touched in last hour")
        print(f"  {upgraded} of those carry LLM signature")
        if upgraded == 0 and recent > 0:
            print("  WARN: cache touches happened but none look like LLM upgrades. Investigate.")
    except Exception as e:
        print(f"  WARN: upgrade verification failed: {e}")

    # === Top god nodes summary ===
    print()
    print("=" * 60)
    print(f"TOP 15 GOD NODES (post-{args.stage_name})")
    print("=" * 60)
    for i, n in enumerate(gn[:15], 1):
        print(f"  {i:2d}. {n['label']} ({n.get('degree', n.get('edges', 0))})")

    print()
    print(f"{args.stage_name.upper()} FINISH COMPLETE")


if __name__ == "__main__":
    main()
