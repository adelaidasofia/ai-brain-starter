#!/usr/bin/env python3
"""
graphify_coverage_audit.py

Single source of truth for "what has / hasn't been graphified in this vault."

Unions three stores:
  1. extraction_manifest.json  (per-file SHA + llm_time + stage)
  2. cache/*.json              (per-chunk extractions, source_file on nodes)
  3. graph.json                (final merged graph, source_file on nodes)

Classifies every eligible .md file as:
  - CURRENT  — in manifest, content SHA matches stored SHA
  - STALE    — in manifest (or cache/graph as source), but content SHA differs
               -> file edited since last graphify
  - MOVED    — basename present in manifest/cache/graph under a different path
               -> likely renamed or reorganized; re-run will refresh
  - MISSING  — never processed (no match by path, flat form, or basename)

Handles:
  - Flat staging paths vs hierarchical paths
  - Absolute vs relative source_file fields
  - Vault reorgs (filename matching as fallback)
  - "Root" layout (graphify-out at vault root) vs "meta" layout (under ⚙️ Meta/)

Outputs:
  - `<graphify-out>/COVERAGE_REPORT.md`  — human-readable
  - `<graphify-out>/COVERAGE_REPORT.json` — machine-readable

Usage:
    python3 graphify_coverage_audit.py --vault-root "$(pwd)"
    python3 graphify_coverage_audit.py --vault-root "$(pwd)" --folder "Notes"
    python3 graphify_coverage_audit.py --vault-root "$(pwd)" --json-only
    python3 graphify_coverage_audit.py --vault-root "$(pwd)" --skip "Archive,Drafts"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s) if s else s


# Universal-skip directory names. Override via --skip or VAULT_SKIP_PARTS env var.
DEFAULT_SKIP_PARTS = {
    "⚙️ Meta",
    "Archive", "🗄 Archive",
    ".trash", ".obsidian", ".claude", ".git",
    "graphify-out", "graphify-input",
    "_review_alternate_drafts",
}


def load_skip_parts(extra: str | None) -> set[str]:
    skip = set(DEFAULT_SKIP_PARTS)
    env_extra = os.environ.get("VAULT_SKIP_PARTS", "")
    for src in (env_extra, extra or ""):
        if not src:
            continue
        for token in src.split(","):
            token = token.strip()
            if token:
                skip.add(token)
    return skip


def detect_layout(vault: Path):
    """Find the graphify-out folder. Two common layouts:
      - root layout:  <vault>/graphify-out/
      - meta layout:  <vault>/⚙️ Meta/graphify-out/
    """
    root_out = vault / "graphify-out"
    meta_out = vault / "⚙️ Meta" / "graphify-out"
    if root_out.exists() and (root_out / "graph.json").exists():
        return root_out, "root"
    if meta_out.exists():
        return meta_out, "meta"
    print(f"ERROR: no graphify-out found under {vault}", file=sys.stderr)
    sys.exit(1)


def is_skipped(rel_path: str, skip_parts: set[str]) -> bool:
    parts = rel_path.split("/")
    return any(p in skip_parts for p in parts)


def norm_source(sf: str, vault: Path) -> str:
    """Normalize a source_file string to its last path component for matching."""
    if not sf:
        return ""
    sf = nfc(sf)
    vs = str(vault) + "/"
    if sf.startswith(vs):
        sf = sf[len(vs):]
    return sf.rsplit("/", 1)[-1]


def sha_for(abs_path: str) -> str:
    """Replicate graphify_stage_finish.py manifest SHA scheme."""
    try:
        content = Path(abs_path).read_bytes()
        return hashlib.sha256(content + b"\x00" + abs_path.encode()).hexdigest()
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault-root", required=True)
    ap.add_argument("--folder", default="", help="Limit audit to this subfolder")
    ap.add_argument("--json-only", action="store_true", help="Write JSON only, skip markdown")
    ap.add_argument("--skip", default="", help="Extra comma-separated SKIP_PARTS names")
    args = ap.parse_args()

    vault = Path(args.vault_root).resolve()
    skip_parts = load_skip_parts(args.skip)
    out_dir, layout = detect_layout(vault)
    print(f"Layout: {layout}")
    print(f"Out dir: {out_dir}")
    print(f"Skip parts: {sorted(skip_parts)}")

    graph_path = out_dir / "graph.json"
    cache_dir = out_dir / "cache"
    manifest_path = out_dir / "extraction_manifest.json"

    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())["entries"]
        except Exception:
            manifest = {}
    print(f"Manifest entries: {len(manifest)}")

    manifest_by_basename = defaultdict(list)
    for abs_path, entry in manifest.items():
        manifest_by_basename[abs_path.rsplit("/", 1)[-1]].append((abs_path, entry))

    tails = defaultdict(set)

    def register(sf: str):
        if not sf:
            return
        tail = norm_source(sf, vault)
        tails[tail].add(sf)

    graph_sources = 0
    if graph_path.exists():
        g = json.loads(graph_path.read_text())
        for n in g.get("nodes", []):
            sf = n.get("source_file")
            if sf:
                register(sf)
                graph_sources += 1
    print(f"Graph nodes with source_file: {graph_sources}")

    cache_sources = 0
    if cache_dir.exists():
        for cf in cache_dir.glob("*.json"):
            try:
                d = json.loads(cf.read_text())
            except Exception:
                continue
            for n in d.get("nodes", []):
                sf = n.get("source_file")
                if sf:
                    register(sf)
                    cache_sources += 1
    print(f"Cache node source_files: {cache_sources}")

    for k in manifest.keys():
        register(k)

    print(f"Unique processed tails: {len(tails)}")

    root = vault / args.folder if args.folder else vault
    if not root.exists():
        print(f"ERROR: folder not found: {root}", file=sys.stderr)
        sys.exit(1)

    classes = {"current": [], "stale": [], "missing": []}
    stats_by_folder = defaultdict(lambda: Counter())

    n_checked = 0
    for p in root.rglob("*.md"):
        rel = str(p)[len(str(vault)) + 1:]
        if is_skipped(rel, skip_parts):
            continue
        if p.stem.endswith(" 2") or p.stem.endswith(" 3"):
            continue
        n_checked += 1
        abs_p = str(p.resolve())

        status = None
        stored_sha = None
        stored_time = None

        entry = manifest.get(abs_p)
        if entry:
            stored_sha = entry.get("sha")
            stored_time = entry.get("llm_time")
            current_sha = sha_for(abs_p)
            if stored_sha and current_sha and stored_sha == current_sha:
                status = "current"
            else:
                status = "stale"
        else:
            basename = p.name
            moved_entries = manifest_by_basename.get(basename, [])
            if moved_entries:
                # Basename match in manifest, file moved. Conservative: classify
                # CURRENT unless mtime is newer than stored llm_time.
                newest = max(moved_entries, key=lambda x: x[1].get("llm_time") or 0)
                _old_path, old_entry = newest
                stored_time = old_entry.get("llm_time")
                stored_sha = old_entry.get("sha")
                try:
                    file_mtime = p.stat().st_mtime
                except Exception:
                    file_mtime = 0
                if stored_time and file_mtime > stored_time + 5:
                    status = "stale"
                else:
                    status = "current"
            else:
                # Tier 3: source_file match in graph/cache
                flat = nfc(rel.replace("/", "_"))
                matched = False
                nfc_basename = nfc(basename)
                if nfc_basename in tails or flat in tails:
                    matched = True
                else:
                    for t in tails:
                        if t.endswith(nfc_basename) and (t == nfc_basename or t[-(len(nfc_basename) + 1)] in ("_", "/")):
                            matched = True
                            break
                if matched:
                    status = "current"
                else:
                    status = "missing"

        top = rel.split("/", 1)[0]
        sub = rel.split("/")[1] if "/" in rel else ""
        folder_key = f"{top}/{sub}" if sub else top
        stats_by_folder[folder_key][status] += 1

        classes[status].append({
            "rel": rel,
            "stored_sha": stored_sha,
            "stored_time": stored_time,
            "stored_stage": entry.get("stage") if entry else None,
        })

    print()
    print(f"Eligible files checked: {n_checked}")
    for k in ("current", "stale", "missing"):
        print(f"  {k:8s}  {len(classes[k]):5d}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vault": str(vault),
        "layout": layout,
        "folder_filter": args.folder or None,
        "totals": {k: len(v) for k, v in classes.items()},
        "by_folder": {k: dict(v) for k, v in stats_by_folder.items()},
        "missing": [c["rel"] for c in classes["missing"]],
        "stale": [
            {"rel": c["rel"], "stored_time": c["stored_time"], "stage": c["stored_stage"]}
            for c in classes["stale"]
        ],
    }
    json_path = out_dir / "COVERAGE_REPORT.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nWrote {json_path}")

    if args.json_only:
        return

    lines = []
    lines.append(f"# Graphify Coverage Report")
    lines.append("")
    lines.append(f"Generated: {payload['generated_at']}")
    lines.append(f"Layout: {layout}")
    if args.folder:
        lines.append(f"Scope: `{args.folder}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Status | Count | Meaning |")
    lines.append("|---|---|---|")
    lines.append(f"| Current | {len(classes['current'])} | Already processed (direct match or moved with unchanged content) |")
    lines.append(f"| Stale | {len(classes['stale'])} | Edited since last graphify, re-run to pick up changes |")
    lines.append(f"| Missing | {len(classes['missing'])} | Never processed |")
    lines.append(f"| **Total** | **{n_checked}** | |")
    lines.append("")

    lines.append("## By folder (top 25)")
    lines.append("")
    lines.append("| Folder | Current | Stale | Missing |")
    lines.append("|---|---:|---:|---:|")
    sorted_folders = sorted(
        stats_by_folder.items(),
        key=lambda kv: sum(kv[1].values()),
        reverse=True,
    )[:25]
    for folder, counts in sorted_folders:
        lines.append(
            f"| `{folder}` | {counts.get('current', 0)} | {counts.get('stale', 0)} | "
            f"{counts.get('missing', 0)} |"
        )
    lines.append("")

    if classes["stale"]:
        lines.append("## Stale (re-run graphify on these)")
        lines.append("")
        lines.append("These files have been edited since their last graphify run.")
        lines.append("")
        for c in sorted(classes["stale"], key=lambda x: x["rel"])[:100]:
            t = c["stored_time"]
            ago = ""
            if t:
                days = int((time.time() - t) / 86400)
                ago = f" (last run {days}d ago, stage: {c['stored_stage']})"
            lines.append(f"- `{c['rel']}`{ago}")
        if len(classes["stale"]) > 100:
            lines.append(f"- ...and {len(classes['stale']) - 100} more")
        lines.append("")

    if classes["missing"]:
        lines.append("## Missing (never graphified)")
        lines.append("")
        miss_by_folder = defaultdict(list)
        for c in classes["missing"]:
            top = c["rel"].split("/", 1)[0]
            sub = c["rel"].split("/")[1] if "/" in c["rel"] else ""
            miss_by_folder[f"{top}/{sub}" if sub else top].append(c["rel"])
        for folder in sorted(miss_by_folder, key=lambda k: -len(miss_by_folder[k]))[:15]:
            files = miss_by_folder[folder]
            lines.append(f"### `{folder}` ({len(files)})")
            lines.append("")
            for f in files[:20]:
                lines.append(f"- `{f}`")
            if len(files) > 20:
                lines.append(f"- ...and {len(files) - 20} more")
            lines.append("")

    md_path = out_dir / "COVERAGE_REPORT.md"
    md_path.write_text("\n".join(lines))
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
