#!/usr/bin/env python3
"""
graphify_stage_finish.py -- End-to-end finish for a graphify stage.

Combines the per-chunk result JSON files, runs canonicalize, union-merges with
the existing graph.json, reclusters, regenerates a simplified GRAPH_REPORT.md,
and saves the semantic cache.

Handles the API drift discovered in Lesson #43: graphify's report.generate()
and analyze.suggest_questions() now require community_labels and other args
the old runbook snippet didn't pass. We sidestep that by writing a simplified
report by hand, with community labels derived from the highest-degree node
in each community.

Usage:
    python3 graphify_stage_finish.py \\
        --num-chunks 20 \\
        --stage-name "stage 2" \\
        --token-cost-k 1377 \\
        --vault-root /path/to/vault

If graphify is installed via pipx, use the pipx python interpreter.
For multi-vault setups, pass --vault-root to target a specific vault.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from collections import Counter
from datetime import date

# Auto-detect vault root from script location: scripts/ sits under a parent folder
_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_VAULT = _SCRIPT_DIR.parent.parent  # scripts/ -> parent folder -> vault root
# VAULT is set in main() from --vault-root arg; default here for import-time safety
VAULT = DEFAULT_VAULT
sys.path.insert(0, str(_SCRIPT_DIR))

from graphify_canonicalize import canonicalize, force_valid_file_types


SLASH_LABEL_RE = re.compile(r"^[\w\s\u00C0-\uFFFF]+/[\w\s\u00C0-\uFFFF]+$")


def clean_slash_label(label: str) -> str:
    """Lesson #45: replace `/` in non-path-shaped labels with comma.

    Strict path-form labels (like `Folder/File`) are left alone for canonicalize
    to handle via strip_folder_prefix. Non-path uses (`Person/Role`,
    `friend/potential`) get rewritten so they pass validation.
    """
    if "/" not in label:
        return label
    if SLASH_LABEL_RE.match(label):
        return label  # path-form, let canonicalize strip it
    return label.replace("/", ", ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--num-chunks", type=int, required=True)
    ap.add_argument("--stage-name", default="stage")
    ap.add_argument("--token-cost-k", type=int, default=0,
                    help="Reported total LLM token cost for this stage, in thousands")
    ap.add_argument("--chunk-prefix", default=None)
    ap.add_argument("--graph-path", default=None)
    ap.add_argument("--vault-root", default=None,
                    help="Root directory of the vault. Defaults to auto-detected from script location.")
    ap.add_argument("--corpus-folder", default=None,
                    help="Content folder inside vault (for multi-vault layout). Auto-detected if omitted.")
    ap.add_argument("--report-title", default=None,
                    help="Title prefix for the GRAPH_REPORT.md. Defaults to stage name.")
    ap.add_argument("--report-path", default=None,
                    help="Where to write the report. Auto-detected per layout if omitted.")
    ap.add_argument("--cache-dir", default=None,
                    help="Override cache dir. Auto-detected per layout if omitted.")
    args = ap.parse_args()

    # Lesson #64: resolve vault root from arg, not hardcoded constant
    global VAULT
    if args.vault_root:
        VAULT = Path(args.vault_root)
    else:
        VAULT = DEFAULT_VAULT
    os.chdir(VAULT)

    # Lesson #87/#90: auto-detect vault layout. Personal puts cache+chunks+graph
    # under graphify-out/. Multi-vault splits them: cache at <vault>/graphify-out/cache/
    # (sibling of content folder), chunks+graph inside <vault>/<corpus>/graphify-out/.
    team_cache = VAULT / "graphify-out" / "cache"
    if team_cache.exists():
        # Need a corpus folder to know where chunks/graph live
        corpus = args.corpus_folder
        if not corpus:
            # Auto-detect: find child directories that have graphify-out nested
            candidates = [c for c in VAULT.iterdir()
                          if c.is_dir() and c.name != "graphify-out"]
            found = []
            for c in candidates:
                for sub in [c, *[s for s in c.iterdir() if s.is_dir()]]:
                    if (sub / "graphify-out").exists() and sub != VAULT:
                        found.append(c.name)
                        break
            if len(found) == 1:
                corpus = found[0]
            elif len(found) > 1:
                print(f"ERROR: multi-vault layout detected but can't auto-detect corpus folder. "
                      f"Candidates: {found}. Pass --corpus-folder.",
                      file=sys.stderr)
                sys.exit(1)
            else:
                corpus = None
        if corpus:
            base = f"{corpus}/graphify-out"
            # Check if there's a nested subfolder with graphify-out
            nested = VAULT / corpus
            for sub in nested.iterdir() if nested.is_dir() else []:
                if sub.is_dir() and (sub / "graphify-out").exists():
                    base = f"{corpus}/{sub.name}/graphify-out"
                    break
            layout = f"team-vault (corpus: {corpus})"
            default_cache_dir = str(team_cache)
        else:
            base = "graphify-out"
            layout = "personal"
            default_cache_dir = str(VAULT / "graphify-out" / "cache")
    else:
        base = "graphify-out"
        layout = "personal"
        default_cache_dir = str(VAULT / "graphify-out" / "cache")

    if args.chunk_prefix is None:
        args.chunk_prefix = str(VAULT / base / ".chunk_")
    if args.graph_path is None:
        args.graph_path = str(VAULT / base / "graph.json")
    if args.report_path is None:
        args.report_path = str(VAULT / base / "GRAPH_REPORT.md")
    if args.cache_dir is None:
        args.cache_dir = default_cache_dir

    print(f"Layout: {layout}")
    print(f"  base: {base}")
    print(f"  cache_dir: {args.cache_dir}")

    print("=" * 60)
    print(f"GRAPHIFY STAGE FINISH -- {args.stage_name}")
    print(f"  vault: {VAULT}")
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
            print(f"  MISSING chunk {i:02d}, aborting")
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
    print(f"  combined: {len(combined_nodes)} nodes, {len(combined_edges)} edges, {len(combined_hyper)} hyperedges")
    if label_fixes:
        print(f"  auto-cleaned {label_fixes} slash-in-label cosmetics")

    # Lesson #72: validate source_file is never a directory (crashes save_semantic_cache)
    dir_fixes = 0
    vault_root = VAULT  # dynamic, set from --vault-root
    for item_list in [combined_nodes, combined_edges, combined_hyper]:
        for item in item_list:
            sf = item.get("source_file", "")
            if sf:
                p = vault_root / sf
                if p.is_dir():
                    item["source_file"] = None
                    dir_fixes += 1
    if dir_fixes:
        print(f"  FIXED {dir_fixes} source_file values that pointed to directories (Lesson #72)")

    # Lesson #70: validate source_file is a specific .md path, not empty
    empty_sf = sum(1 for n in combined_nodes if not n.get("source_file"))
    if empty_sf:
        print(f"  NOTE: {empty_sf} nodes have no source_file (expected for cross-file inferred nodes)")

    # Lesson #81: filter non-dict items from nodes/edges/hyperedges
    # (60+ file chunks sometimes return node IDs as strings or edges as lists)
    bad_nodes = sum(1 for n in combined_nodes if not isinstance(n, dict))
    bad_edges = sum(1 for e in combined_edges if not isinstance(e, dict))
    bad_hyper = sum(1 for h in combined_hyper if not isinstance(h, dict))
    if bad_nodes or bad_edges or bad_hyper:
        combined_nodes = [n for n in combined_nodes if isinstance(n, dict)]
        combined_edges = [e for e in combined_edges if isinstance(e, dict)]
        combined_hyper = [h for h in combined_hyper if isinstance(h, dict)]
        print(f"  WARNING: dropped {bad_nodes} non-dict nodes, {bad_edges} non-dict edges, {bad_hyper} non-dict hyperedges (Lesson #81)")

    raw_extraction = {"nodes": combined_nodes, "edges": combined_edges, "hyperedges": combined_hyper}
    raw_path = Path(f"{base}/.{args.stage_name.replace(' ', '_')}_raw.json")
    raw_path.write_text(json.dumps(raw_extraction, indent=2, ensure_ascii=False), encoding="utf-8")
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

    canon_path = Path(f"{base}/.{args.stage_name.replace(' ', '_')}_canon.json")
    canon_path.write_text(json.dumps(canon, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  wrote {canon_path}")

    # === Step 3: backup + union merge ===
    print()
    print("Step 3: union-merging with existing graph.json...")
    ts = time.strftime("%Y%m%d_%H%M")
    graph_path = Path(args.graph_path)
    if graph_path.exists():
        backup = Path(args.graph_path + f".backup_{ts}_pre_{args.stage_name.replace(' ', '_')}_finish")
        backup.write_bytes(graph_path.read_bytes())
        print(f"  backed up: {backup}")
        existing = json.loads(graph_path.read_text(encoding="utf-8"))
    else:
        print(f"  no existing graph.json found, starting fresh")
        existing = {"nodes": [], "links": [], "hyperedges": []}
    existing_nodes = existing["nodes"]
    existing_edges = existing.get("links", [])
    print(f"  existing: {len(existing_nodes)} nodes, {len(existing_edges)} edges")

    existing_node_ids = {n["id"] for n in existing_nodes}
    existing_node_labels = {(n.get("label"), n.get("file_type", "document")): n["id"] for n in existing_nodes}

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

    existing_edge_keys = {(e.get("source"), e.get("target"), e.get("relation")) for e in existing_edges}
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
    print(f"  MERGED TOTAL: {len(merged_nodes)} nodes, {len(merged_edges)} edges, {len(merged_hyper)} hyperedges")
    print(f"  growth: nodes +{added_nodes} ({100*added_nodes/max(1,len(existing_nodes)):.1f}%), "
          f"edges +{added_edges} ({100*added_edges/max(1,len(existing_edges)):.1f}%)")

    Path(args.graph_path).write_text(json.dumps(merged_graph, ensure_ascii=False), encoding="utf-8")
    print(f"  wrote {args.graph_path}")

    # === Step 3.5: adjacency-based dedupe (catches what canonicalize misses) ===
    print()
    print("Step 3.5: adjacency-based dedupe (post-canonicalize quality pass)...")
    try:
        scripts_dir = Path(__file__).parent
        sys.path.insert(0, str(scripts_dir))
        from graphify_dedupe_by_adjacency import (
            find_duplicate_pairs,
            apply_merges,
            JACCARD_THRESHOLD,
            MIN_DEGREE,
        )
        edge_field = "links" if "links" in merged_graph else "edges"
        pairs = find_duplicate_pairs(
            merged_graph["nodes"],
            merged_graph[edge_field],
            JACCARD_THRESHOLD,
            MIN_DEGREE,
        )
        if pairs:
            print(f"  found {len(pairs)} adjacency duplicates to merge:")
            for p in pairs:
                print(f"    {p['canonical_label']!r} <- {p['duplicate_label']!r}  jacc={p['jaccard']}")
            nodes_removed, edges_removed, self_loops, deduped = apply_merges(merged_graph, pairs)
            print(f"  removed {nodes_removed} duplicate nodes, {self_loops} self-loops, deduped {deduped} edges")
            Path(args.graph_path).write_text(json.dumps(merged_graph, ensure_ascii=False), encoding="utf-8")
            print(f"  re-wrote {args.graph_path}")
        else:
            print("  no duplicates found, graph is clean")
    except Exception as e:
        print(f"  WARNING: dedupe pass failed ({e}), continuing without it")
        import traceback; traceback.print_exc()

    # === Step 3.7: prune dangling edges (Lesson #70) ===
    print()
    print("Step 3.7: pruning dangling edges...")
    try:
        from graphify.export import prune_dangling_edges
        merged_graph, pruned = prune_dangling_edges(merged_graph)
    except ImportError:
        edge_field = "links" if "links" in merged_graph else "edges"
        node_ids_set = {n["id"] for n in merged_graph["nodes"]}
        before_edges = len(merged_graph[edge_field])
        merged_graph[edge_field] = [
            e for e in merged_graph[edge_field]
            if e.get("source", "") in node_ids_set and e.get("target", "") in node_ids_set
        ]
        pruned = before_edges - len(merged_graph[edge_field])
    if pruned:
        print(f"  pruned {pruned} dangling edges (endpoints referenced non-existent nodes)")
        Path(args.graph_path).write_text(json.dumps(merged_graph, ensure_ascii=False), encoding="utf-8")
        print(f"  re-wrote {args.graph_path}")
    else:
        print("  no dangling edges, graph is clean")

    # === Step 3.8: clean stale _src/_tgt edge metadata (Lesson #56 root cause fix) ===
    edge_field_38 = "links" if "links" in merged_graph else "edges"
    node_ids_38 = {n["id"] for n in merged_graph["nodes"]}
    stale_meta = 0
    for e in merged_graph[edge_field_38]:
        for field in ("_src", "_tgt"):
            if field in e and e[field] not in node_ids_38:
                del e[field]
                stale_meta += 1
    if stale_meta:
        print(f"\nStep 3.8: removed {stale_meta} stale _src/_tgt edge metadata refs")
        Path(args.graph_path).write_text(json.dumps(merged_graph, ensure_ascii=False), encoding="utf-8")

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

    # Lesson #43: build community_labels from highest-degree node per community
    community_labels = {}
    for cid, nids in communities.items():
        best = max(nids, key=lambda n: G.degree(n))
        community_labels[cid] = G.nodes[best].get("label", best)

    gn = god_nodes(G, top_n=20)
    try:
        sc = surprising_connections(G, communities, top_n=10)
    except (KeyError, Exception) as e:
        print(f"  surprising_connections failed ({e}), skipping (Lesson #56)")
        sc = []

    confs = Counter(d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True))
    total_e = sum(confs.values())

    today = date.today().isoformat()
    report_title = args.report_title or f"{VAULT.name}"
    lines = []
    lines.append(f"# Graph Report - {report_title}  ({today})")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, {len(communities)} communities")
    ext = round(100 * confs.get("EXTRACTED", 0) / max(1, total_e))
    inf = round(100 * confs.get("INFERRED", 0) / max(1, total_e))
    amb = round(100 * confs.get("AMBIGUOUS", 0) / max(1, total_e))
    lines.append(f"- Extraction: {ext}% EXTRACTED, {inf}% INFERRED, {amb}% AMBIGUOUS")
    if args.token_cost_k:
        lines.append(f"- {args.stage_name} token cost: ~{args.token_cost_k}K LLM tokens")
    lines.append("")
    lines.append("## Top God Nodes")
    for i, n in enumerate(gn, 1):
        lines.append(f"{i}. `{n['label']}` ({n['edges']} edges)")
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

    Path(args.report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote {args.report_path}")

    # === Step 5: cache save ===
    print()
    print("Step 5: saving semantic cache...")
    try:
        from graphify.cache import save_semantic_cache
        # graphify.cache writes to <root>/graphify-out/cache/, so pass the grandparent
        # of the target cache dir so the lib lands exactly where we expect.
        cache_root = Path(args.cache_dir).parent.parent

        # Fix: save_semantic_cache does `root / source_file` for non-absolute paths.
        # For personal vault, root might not be the vault root but source_files are
        # relative to VAULT, so root/source_file doesn't exist. Normalize source_files
        # to absolute VAULT paths before passing in. Absolute paths bypass the
        # root-concatenation in save_semantic_cache so cache_root is only used for
        # the output directory, not file resolution.
        def _abs_canon(items):
            result = []
            for item in (items or []):
                sf = item.get("source_file", "")
                if sf and not Path(sf).is_absolute():
                    abs_sf = VAULT / sf
                    if abs_sf.exists():
                        item = dict(item)
                        item["source_file"] = str(abs_sf)
                result.append(item)
            return result

        abs_nodes = _abs_canon(canon["nodes"])
        abs_edges = _abs_canon(canon["edges"])
        abs_hyper = _abs_canon(canon.get("hyperedges") or [])

        saved = save_semantic_cache(abs_nodes, abs_edges, abs_hyper, root=cache_root)
        print(f"  cached {saved} per-file entries at {cache_root}/graphify-out/cache/")
    except Exception as e:
        print(f"  WARN: cache save failed: {e}")

    # Lesson #93: update the extraction manifest so future select runs can
    # short-circuit on file mtime instead of SHA.
    #
    # Lesson #106 (2026-04-21): record EVERY file sent to the stage (from
    # .chunk_NN_files.txt), not just files whose LLM outputs produced canonical
    # nodes/edges. Files covered only by preflight-wikilink edges (no LLM-new
    # items) were silently missed, so coverage audits kept flagging them as
    # MISSING on subsequent runs.
    #
    # Lesson #106b: source_file on chunk items can be the STAGED path
    # (graphify-input/flattened_name.md). That path does not resolve to a real
    # file after cleanup. Unflatten to the original vault path by trying each
    # `_` → `/` combination against the actual filesystem.
    try:
        manifest_path = (VAULT / base / "extraction_manifest.json").resolve()
        manifest = {"version": 1, "entries": {}}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
                if "entries" not in manifest:
                    manifest = {"version": 1, "entries": {}}
            except Exception:
                manifest = {"version": 1, "entries": {}}

        now = time.time()

        def resolve_source_file(sf):
            """Map source_file (possibly staged/flattened) to an absolute vault
            path. Returns Path or None."""
            if not sf or not isinstance(sf, str):
                return None
            p = (VAULT / sf).resolve() if not Path(sf).is_absolute() else Path(sf).resolve()
            if p.is_file():
                return p
            stripped = sf
            for prefix in ("graphify-input/",):
                if stripped.startswith(prefix):
                    stripped = stripped[len(prefix):]
                    break
            parts = stripped.split("_")
            if len(parts) > 1:
                from itertools import combinations
                for k in range(1, len(parts)):
                    for slash_positions in combinations(range(1, len(parts)), k):
                        segs = []
                        cur = []
                        for i, piece in enumerate(parts):
                            cur.append(piece)
                            if i + 1 in slash_positions:
                                segs.append("_".join(cur))
                                cur = []
                        segs.append("_".join(cur))
                        cand = VAULT / "/".join(segs)
                        if cand.is_file():
                            return cand.resolve()
            return None

        staged_source_files = set()
        for item_list in [canon["nodes"], canon["edges"], canon.get("hyperedges") or []]:
            for item in item_list:
                sf = item.get("source_file")
                if sf and isinstance(sf, str):
                    staged_source_files.add(sf)

        node_counts_by_staged = Counter(
            n.get("source_file", "") for n in canon["nodes"] if n.get("source_file")
        )

        # Also read chunk input lists so preflight-only files get manifest entries.
        stage_input_files = set()
        chunk_prefix = args.chunk_prefix
        for i in range(1, args.num_chunks + 1):
            list_path = Path(f"{chunk_prefix}{i:02d}_files.txt")
            if not list_path.is_file():
                continue
            for line in list_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                stage_input_files.add(line)

        all_refs = staged_source_files | stage_input_files

        files_in_stage = {}
        unresolved = []
        for sf in all_refs:
            abs_p = resolve_source_file(sf)
            if abs_p:
                prev_sf = files_in_stage.get(str(abs_p))
                if sf in staged_source_files and (prev_sf is None or prev_sf not in staged_source_files):
                    files_in_stage[str(abs_p)] = sf
                elif prev_sf is None:
                    files_in_stage[str(abs_p)] = sf
            else:
                unresolved.append(sf)

        for abs_path, staged_sf in files_in_stage.items():
            try:
                content = Path(abs_path).read_bytes()
                sha = hashlib.sha256(content + b"\x00" + abs_path.encode()).hexdigest()
            except Exception:
                sha = None
            manifest["entries"][abs_path] = {
                "llm_time": now,
                "sha": sha,
                "node_count": node_counts_by_staged.get(staged_sf or "", 0),
                "stage": args.stage_name,
            }

        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  manifest updated: {len(files_in_stage)} files recorded in {manifest_path}")
        if unresolved:
            print(f"  WARN: {len(unresolved)} source_file refs could not be resolved to vault paths:")
            for sf in list(unresolved)[:5]:
                print(f"    {sf}")
    except Exception as e:
        print(f"  WARN: manifest update failed: {e}")

    # === Step 5b: verify upgrade (Lesson #46, directory count is misleading) ===
    print()
    print("Step 5b: verifying upgrade...")
    try:
        from graphify_stage_select import is_llm_extraction
        cache_dir = Path(args.cache_dir)
        cutoff = time.time() - 3600  # last hour
        upgraded = 0
        recent = 0
        for c in cache_dir.glob("*.json"):
            if c.stat().st_mtime < cutoff:
                continue
            recent += 1
            try:
                if is_llm_extraction(json.loads(c.read_text())):
                    upgraded += 1
            except Exception:
                pass
        print(f"  {recent} cache entries touched in last hour")
        print(f"  {upgraded} of those carry LLM signature (hyperedges or non-EXTRACTED confidence)")
        if upgraded == 0 and recent > 0:
            print(f"  WARN: cache touches happened but none look like LLM upgrades. Investigate.")
    except Exception as e:
        print(f"  WARN: upgrade verification failed: {e}")

    # === Top god nodes summary ===
    print()
    print("=" * 60)
    print(f"TOP 15 GOD NODES (post-{args.stage_name})")
    print("=" * 60)
    for i, n in enumerate(gn[:15], 1):
        print(f"  {i:2d}. {n['label']} ({n['edges']})")

    print()
    print(f"{args.stage_name.upper()} FINISH COMPLETE")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
