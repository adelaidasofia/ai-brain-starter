#!/usr/bin/env python3
"""
graphify_prep.py — Run BEFORE the LLM extraction phase of /graphify.

Does the work that doesn't need an LLM:
  1. Dedupe " 2.md" files (md5-identical -> delete; different -> quarantine)
  2. Pre-extract every [[wikilink]] as a node + EXTRACTED edge
  3. Pre-extract YAML frontmatter (floor tags, dates, source) as edges
  4. Write a "preflight" JSON the LLM extraction will be merged with

Why: each file has dozens of wikilinks already. Regex catches 100% of them
in <2 seconds. The LLM only needs to do INFERRED / semantic work after this.
Combined with dedupe, this typically cuts LLM token cost by 50-65% on
journal-style markdown vaults.

Usage:
    python3 graphify_prep.py <input_dir>           # dry-run (counts only)
    python3 graphify_prep.py <input_dir> --apply   # delete dupes + write preflight

Outputs (when --apply):
    graphify-out/.graphify_preflight.json       (nodes + edges from regex)
    graphify-out/_prep_report.md                (what was done, human-readable)
    graphify-input/_review_alternate_drafts/    (quarantined non-identical " 2.md" files)
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir  # noqa: E402

# --- regex patterns ---
WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+?)(?:\|([^\[\]]+?))?(?:#[^\[\]]*)?\]\]")
YAML_FENCE_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
KEY_VAL_RE = re.compile(r"^([a-zA-Z_][\w_]*)\s*:\s*(.*)$", re.MULTILINE)
LIST_ITEM_RE = re.compile(r"^\s*-\s*(.+)$", re.MULTILINE)

# Concept whitelist — optional file with second-tier named entities that
# frequently appear unwikilinked in vault prose. One term per line, # for
# comments, blank lines ignored. Place at <vault>/Meta/concept_whitelist.txt
# or pass --whitelist-path.

# The 34 floors of the High-Rise framework (expanded from 16 in April 2026
# by mapping 150+ named human emotions onto the building). ai-brain-starter
# installs this framework into every vault, so these names apply to all repo users.
CANONICAL_FLOORS = {
    # Low (1-18): reactive
    "disgust", "shame", "embarrassment", "guilt", "apathy", "resignation",
    "confusion", "loneliness", "boredom", "grief", "disappointment", "hurt",
    "fear", "frustration", "desire", "anger", "contempt", "pride",
    # Middle (19-24): transitional
    "courage", "hope", "neutrality", "willingness", "acceptance", "reason",
    # High (25-34): generative
    "trust", "compassion", "humility", "belonging", "love", "gratitude",
    "excitement", "wonder", "joy", "peace",
}


def slugify(text: str, maxlen: int = 60) -> str:
    """ASCII-safe lowercase slug for IDs."""
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_-]+", "_", s).strip("_")
    return s[:maxlen]


def normalize_label(label: str) -> str:
    """Normalize a node label for dedup."""
    label = label.strip()
    for suf in (" (Floor)", " Floor", " floor"):
        if label.endswith(suf):
            label = label[: -len(suf)]
            break
    return label.strip().lower()


def canonical_id(label: str) -> str:
    """Canonical ID = c_<normalized slug>. Used for cross-file dedup."""
    return "c_" + slugify(normalize_label(label))


def load_whitelist(path: Path) -> tuple[list[str], "re.Pattern | None"]:
    """Load the concept whitelist and build a single compiled regex with word
    boundaries (case-insensitive). Returns (canonical_terms, compiled_re).

    Empty whitelist or missing file -> returns ([], None) and prep skips
    whitelist scanning.
    """
    if not path.exists():
        return [], None
    terms = []
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        terms.append(line)
    if not terms:
        return [], None
    terms_sorted = sorted(set(terms), key=lambda t: -len(t))
    pattern = r"\b(?:" + "|".join(re.escape(t) for t in terms_sorted) + r")\b"
    compiled = re.compile(pattern, re.IGNORECASE)
    return terms_sorted, compiled


def parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter as a dict (no PyYAML -- handle simple key/value + lists)."""
    m = YAML_FENCE_RE.match(text)
    if not m:
        return {}
    body = m.group(1)
    out = {}
    current_list_key = None
    for line in body.splitlines():
        if not line.strip():
            current_list_key = None
            continue
        if line.startswith(" ") and current_list_key:
            li = LIST_ITEM_RE.match(line)
            if li:
                out.setdefault(current_list_key, []).append(li.group(1).strip().strip("'\""))
            continue
        kv = KEY_VAL_RE.match(line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip()
            if val.startswith("[") and val.endswith("]"):
                items = [x.strip().strip("'\"") for x in val[1:-1].split(",") if x.strip()]
                out[key] = items
            elif not val:
                current_list_key = key
            else:
                out[key] = val.strip("'\"")
                current_list_key = None
    return out


def is_excluded(path: Path, exclude_patterns: list[str]) -> bool:
    """Return True if any path component matches an exclude pattern (case-insensitive substring)."""
    if not exclude_patterns:
        return False
    parts_lower = [p.lower() for p in path.parts]
    for pat in exclude_patterns:
        pat_lower = pat.lower()
        if any(pat_lower in part for part in parts_lower):
            return True
    return False


def dedupe(input_dir: Path, apply: bool, exclude_patterns: list[str] = None) -> dict:
    """Find and (optionally) remove duplicates in two passes:
    Pass A: ' 2.md' siblings in the same directory (md5-identical -> delete; different -> quarantine).
    Pass B: cross-directory filename collisions (md5-identical -> keep root version, delete others).
    """
    exclude_patterns = exclude_patterns or []
    qdir = input_dir / "_review_alternate_drafts"
    deleted_a, quarantined_a = [], []
    deleted_b, quarantined_b = [], []

    # Pass A: " 2.md" suffix duplicates
    for d in sorted(input_dir.rglob("* 2.md")):
        if "_review_alternate_drafts" in d.parts:
            continue
        if is_excluded(d, exclude_patterns):
            continue
        orig = d.parent / (d.name[:-5] + ".md")
        if not orig.exists():
            quarantined_a.append((d, "orphan_no_original"))
            continue
        if hashlib.md5(orig.read_bytes()).hexdigest() == hashlib.md5(d.read_bytes()).hexdigest():
            deleted_a.append(d)
        else:
            quarantined_a.append((d, "alternate_draft"))

    if apply:
        if quarantined_a:
            qdir.mkdir(exist_ok=True)
        for d in deleted_a:
            d.unlink()
        for d, _reason in quarantined_a:
            shutil.move(str(d), str(qdir / d.name))

    # Pass B: cross-directory filename collisions
    by_name = {}
    for f in input_dir.rglob("*.md"):
        if "_review_alternate_drafts" in f.parts:
            continue
        if is_excluded(f, exclude_patterns):
            continue
        by_name.setdefault(f.name, []).append(f)

    for name, copies in by_name.items():
        if len(copies) < 2:
            continue
        copies_sorted = sorted(copies, key=lambda p: (len(p.parts), str(p)))
        canonical = copies_sorted[0]
        canonical_md5 = hashlib.md5(canonical.read_bytes()).hexdigest()
        for other in copies_sorted[1:]:
            other_md5 = hashlib.md5(other.read_bytes()).hexdigest()
            if other_md5 == canonical_md5:
                deleted_b.append(other)
            else:
                quarantined_b.append((other, "cross_dir_diff"))

    if apply:
        if quarantined_b:
            qdir.mkdir(exist_ok=True)
        for d in deleted_b:
            d.unlink()
        for d, _reason in quarantined_b:
            target = qdir / d.name
            if target.exists():
                target = qdir / f"{d.parent.name}__{d.name}"
            shutil.move(str(d), str(target))

    if apply:
        for sub in sorted(input_dir.iterdir(), reverse=True):
            if sub.is_dir() and sub.name != "_review_alternate_drafts":
                try:
                    if not any(sub.iterdir()):
                        sub.rmdir()
                except OSError:
                    pass

    return {
        "deleted_passA_2md": len(deleted_a),
        "quarantined_passA_2md": len(quarantined_a),
        "deleted_passB_crossdir": len(deleted_b),
        "quarantined_passB_crossdir": len(quarantined_b),
        "qdir": str(qdir),
    }


def extract_structural(input_dir: Path, whitelist_path: Path = None,
                       exclude_patterns: list[str] = None) -> dict:
    """Walk markdown files; pull every [[wikilink]] + frontmatter signal as nodes/edges.
    Also scans each file against the concept whitelist for unwikilinked second-tier
    entities.
    """
    exclude_patterns = exclude_patterns or []
    nodes_by_id = {}
    edges = []
    edge_keys = set()
    files_seen = 0

    whitelist_terms, whitelist_re = [], None
    if whitelist_path:
        whitelist_terms, whitelist_re = load_whitelist(whitelist_path)
    whitelist_canon = {t.lower(): t for t in whitelist_terms}
    whitelist_hits = 0

    for f in sorted(input_dir.rglob("*.md")):
        if "_review_alternate_drafts" in f.parts:
            continue
        if is_excluded(f, exclude_patterns):
            continue
        files_seen += 1
        text = f.read_text(errors="ignore")
        rel = str(f.relative_to(input_dir.parent))
        file_stem_id = "file_" + slugify(f.stem)

        stem = f.stem
        display = stem if len(stem) <= 60 else stem[:57].rstrip() + "..."
        if file_stem_id not in nodes_by_id:
            nodes_by_id[file_stem_id] = {
                "id": file_stem_id,
                "label": display,
                "file_type": "document",
                "source_file": rel,
                "is_file_node": True,
            }

        fm = parse_frontmatter(text)

        # Floor tags from frontmatter
        floors = fm.get("dominant_floors") or fm.get("floor") or []
        if isinstance(floors, str):
            floors = [floors]
        for floor in floors:
            floor_norm = normalize_label(floor)
            if floor_norm in CANONICAL_FLOORS:
                cid = canonical_id(floor)
                if cid not in nodes_by_id:
                    nodes_by_id[cid] = {
                        "id": cid,
                        "label": floor.strip().title(),
                        "file_type": "document",
                        "source_file": rel,
                        "is_floor": True,
                    }
                key = (file_stem_id, cid, "expresses_floor")
                if key not in edge_keys:
                    edge_keys.add(key)
                    edges.append({
                        "source": file_stem_id, "target": cid,
                        "relation": "expresses_floor",
                        "confidence": "EXTRACTED", "confidence_score": 1.0,
                        "source_file": rel, "weight": 1.0,
                    })

        # Wikilinks
        wikilinked_in_file = set()
        for m in WIKILINK_RE.finditer(text):
            target = m.group(1).strip()
            if not target or len(target) > 60:
                continue
            cid = canonical_id(target)
            wikilinked_in_file.add(cid)
            if cid not in nodes_by_id:
                nodes_by_id[cid] = {
                    "id": cid,
                    "label": target,
                    "file_type": "document",
                    "source_file": rel,
                }
            key = (file_stem_id, cid, "references")
            if key not in edge_keys:
                edge_keys.add(key)
                edges.append({
                    "source": file_stem_id, "target": cid,
                    "relation": "references",
                    "confidence": "EXTRACTED", "confidence_score": 1.0,
                    "source_file": rel, "weight": 1.0,
                })

        # Concept whitelist scan
        if whitelist_re is not None:
            seen_in_file = set()
            for m in whitelist_re.finditer(text):
                hit = m.group(0)
                canon = whitelist_canon.get(hit.lower(), hit)
                cid = canonical_id(canon)
                if cid in wikilinked_in_file or cid in seen_in_file:
                    continue
                seen_in_file.add(cid)
                if cid not in nodes_by_id:
                    nodes_by_id[cid] = {
                        "id": cid,
                        "label": canon,
                        "file_type": "document",
                        "source_file": rel,
                        "from_whitelist": True,
                    }
                key = (file_stem_id, cid, "mentions")
                if key not in edge_keys:
                    edge_keys.add(key)
                    edges.append({
                        "source": file_stem_id, "target": cid,
                        "relation": "mentions",
                        "confidence": "EXTRACTED", "confidence_score": 1.0,
                        "source_file": rel, "weight": 1.0,
                    })
                    whitelist_hits += 1

    return {
        "nodes": list(nodes_by_id.values()),
        "edges": edges,
        "hyperedges": [],
        "files_processed": files_seen,
        "whitelist_hits": whitelist_hits,
        "whitelist_terms_loaded": len(whitelist_terms),
        "input_tokens": 0,
        "output_tokens": 0,
    }


def write_report(report: dict, dedupe_stats: dict, structural: dict, out_path: Path):
    floor_edges = sum(1 for e in structural["edges"] if e["relation"] == "expresses_floor")
    wikilink_edges = sum(1 for e in structural["edges"] if e["relation"] == "references")
    floor_nodes = sum(1 for n in structural["nodes"] if n.get("is_floor"))
    file_nodes = sum(1 for n in structural["nodes"] if n.get("is_file_node"))
    concept_nodes = len(structural["nodes"]) - floor_nodes - file_nodes
    out_path.write_text(f"""# Graphify Prep Report

## Dedupe
**Pass A -- ` 2.md` sibling duplicates:**
- md5-identical deleted: **{dedupe_stats['deleted_passA_2md']}**
- alternate drafts quarantined: **{dedupe_stats['quarantined_passA_2md']}**

**Pass B -- cross-directory filename collisions:**
- md5-identical deleted (kept root copy): **{dedupe_stats['deleted_passB_crossdir']}**
- different copies quarantined: **{dedupe_stats['quarantined_passB_crossdir']}**

Quarantine: `{dedupe_stats['qdir']}`

## Structural pre-extraction (regex, no LLM)
- files processed: **{structural['files_processed']:,}**
- canonical nodes: **{len(structural['nodes']):,}**
  - file nodes: {file_nodes}
  - floor nodes: {floor_nodes}
  - concept/wikilink nodes: {concept_nodes}
- EXTRACTED edges: **{len(structural['edges']):,}**
  - wikilink references: {wikilink_edges}
  - floor expressions: {floor_edges}

The LLM pass after this only needs to add INFERRED / semantic / hyperedge content
on top of the structural baseline. It should focus on:
  - rationale_for edges (why a thing was decided)
  - semantically_similar_to (cross-file conceptual matches)
  - conceptually_related_to (implicit connections beyond wikilinks)
  - hyperedges (multi-node coherent groups)
""")


def _icloud_warmth_check(input_dir: Path, sample_size: int = 40, threshold_sec: float = 2.0):
    """Sample-read some files to detect iCloud cold-storage demand-paging.

    macOS Desktop/Documents folders are iCloud-synced. Cold reads on files not
    materialized locally take ~200ms each (vs ~0.1ms warm), making bulk scans
    ~1000x slower. This samples a batch and warns loudly if any read is slow.
    """
    import time
    import random

    ipath = str(input_dir)
    if "/Desktop/" not in ipath and "/Documents/" not in ipath:
        return

    try:
        files = [f for f in input_dir.rglob("*.md")][:2000]
    except Exception:
        return
    if len(files) < sample_size:
        return

    sample = random.sample(files[len(files)//3:], min(sample_size, len(files) - len(files)//3))
    t0 = time.time()
    total_bytes = 0
    slowest = 0.0
    for f in sample:
        t1 = time.time()
        try:
            total_bytes += len(f.read_bytes())
        except Exception:
            continue
        slowest = max(slowest, time.time() - t1)
    elapsed = time.time() - t0

    if elapsed > threshold_sec or slowest > 0.15:
        print()
        print("WARNING: iCloud cold-storage demand-paging detected")
        print(f"    Sample read of {sample_size} files took {elapsed:.1f}s (slowest single read: {slowest*1000:.0f}ms)")
        print(f"    This means bulk operations may hang for 5+ minutes waiting on iCloud.")
        print(f"    FIX: run `brctl download \"{input_dir}\"` first, then re-run this script.")
        print(f"    Waiting 5s in case you want to Ctrl-C and run brctl download...")
        print()
        time.sleep(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_dir")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete dupes + write outputs (default: dry-run)")
    ap.add_argument("--out-dir", default="graphify-out")
    ap.add_argument("--whitelist-path", type=Path, default=None,
                    help="Path to concept_whitelist.txt (one term per line). "
                         "Default: <vault>/Meta/concept_whitelist.txt")
    ap.add_argument("--exclude", action="append", default=[],
                    help="path substring to exclude (case-insensitive). Repeat for multiple. "
                         "Example: --exclude Archive --exclude .obsidian")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="skip the dedupe pass entirely. Use this when input is a curated/synced "
                         "vault you should NOT modify (e.g. team Google Drive). Still writes preflight.")
    args = ap.parse_args()

    input_dir = Path(args.input_dir).resolve()
    if not input_dir.exists():
        print(f"input dir not found: {input_dir}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out_dir)
    if args.apply or args.no_dedupe:
        out_dir.mkdir(exist_ok=True)

    print(f"=== graphify_prep ({'APPLY' if args.apply else 'DRY-RUN'}{', NO-DEDUPE' if args.no_dedupe else ''}) ===")
    print(f"input: {input_dir}")
    if args.exclude:
        print(f"excluding: {args.exclude}")
    print()

    _icloud_warmth_check(input_dir)

    # Auto-detect whitelist path if not provided
    whitelist_path = args.whitelist_path
    if whitelist_path is None:
        meta = find_meta_dir(input_dir.parent, prefer_subfolders=("concept_whitelist.txt",))
        if meta is not None:
            wl = meta / "concept_whitelist.txt"
            if wl.exists():
                whitelist_path = wl

    if args.no_dedupe:
        print("[1/2] Dedupe SKIPPED (--no-dedupe set; input is treated as read-only)")
        dedupe_stats = {
            "deleted_passA_2md": 0, "quarantined_passA_2md": 0,
            "deleted_passB_crossdir": 0, "quarantined_passB_crossdir": 0,
            "qdir": "(not used)",
        }
    else:
        print("[1/2] Dedupe (Pass A: ' 2.md' siblings, Pass B: cross-directory)...")
        dedupe_stats = dedupe(input_dir, apply=args.apply, exclude_patterns=args.exclude)
        print(f"    Pass A (' 2.md'): {dedupe_stats['deleted_passA_2md']} would-delete | "
              f"{dedupe_stats['quarantined_passA_2md']} would-quarantine")
        print(f"    Pass B (cross-dir): {dedupe_stats['deleted_passB_crossdir']} would-delete | "
              f"{dedupe_stats['quarantined_passB_crossdir']} would-quarantine")
    print()

    print("[2/2] Structural pre-extraction...")
    structural = extract_structural(input_dir, whitelist_path=whitelist_path,
                                    exclude_patterns=args.exclude)
    print(f"    {structural['files_processed']:,} files -> "
          f"{len(structural['nodes']):,} nodes, "
          f"{len(structural['edges']):,} EXTRACTED edges")
    if structural.get('whitelist_terms_loaded'):
        print(f"    whitelist: {structural['whitelist_terms_loaded']} terms loaded -> "
              f"{structural['whitelist_hits']:,} unwikilinked entity edges added")
    print()

    if args.apply or args.no_dedupe:
        preflight_path = out_dir / ".graphify_preflight.json"
        preflight_path.write_text(json.dumps(structural))
        report_path = out_dir / "_prep_report.md"
        write_report({}, dedupe_stats, structural, report_path)
        print(f"Wrote {preflight_path}")
        print(f"Wrote {report_path}")
    else:
        print("(dry-run -- re-run with --apply to make changes)")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
