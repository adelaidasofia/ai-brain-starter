#!/usr/bin/env python3
"""
graphify_stage_select.py — Stage selection for the graphify staged rollout.

Walks a corpus folder, applies the standard filters (≥500 words, no [AI Extract]),
checks the cache for REAL LLM extractions (not preflight stubs — see Lesson #39),
and bin-packs the misses into ~50K-word chunks ready for parallel dispatch.

LESSON #39 (2026-04-11):
The original stage2_select.py counted ANY cache file as "done", but the cache
contains both preflight regex stubs AND real LLM extractions under the same
SHA256 keying. Result: false-negative work ("0 tokens needed") when the
semantic layer is actually missing.

LLM cache entry signature (any ONE is sufficient):
  - has non-empty hyperedges list
  - any edge has confidence != "EXTRACTED"
  - any edge has confidence_score != 1.0

Preflight-only signature: structural relations ("references", "expresses_floor")
with confidence="EXTRACTED" and score=1.0. These are NOT real hits — re-extract.

Usage:
    python3 graphify_stage_select.py <corpus_folder> [--target-words 50000] [--stage-pct 1.0] [--stage-skip-pct 0.30]

Examples:
    # Stage 3: full Daily Logs
    python3 graphify_stage_select.py "📅 Daily Logs"

    # Stage 2: skip the recent 30% (already done in Stage 1), take the rest
    python3 graphify_stage_select.py "📓 Journals" --stage-skip-pct 0.30

    # Stage 5 sub-stage: full Writing folder
    python3 graphify_stage_select.py "✍️ Writing"
"""
import argparse
import json
import hashlib
import os
import sys
from pathlib import Path
from datetime import datetime

DEFAULT_VAULT = Path.cwd()  # default to current working directory; override via --vault-root
CACHE_DIR = DEFAULT_VAULT / "⚙️ Meta" / "graphify-out" / "cache"


def is_llm_extraction(cache_data: dict) -> bool:
    """Return True if a cache entry is from LLM extraction, False if preflight-only."""
    if cache_data.get("hyperedges"):
        return True
    for edge in cache_data.get("edges", []) or cache_data.get("links", []):
        conf = edge.get("confidence")
        score = edge.get("confidence_score")
        if conf and conf != "EXTRACTED":
            return True
        if score is not None and score != 1.0:
            return True
    for node in cache_data.get("nodes", []):
        conf = node.get("confidence")
        if conf and conf != "EXTRACTED":
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus_folder", nargs="*",
                    help="One or more folders relative to vault root, e.g. '📝 Notes' '✍️ Writing'. "
                         "Can also be passed via --include.")
    ap.add_argument("--include", action="append", default=[], metavar="FOLDER",
                    help="Alias for the positional corpus_folder arg. Repeatable: "
                         "--include 'External Inputs/Slack' --include 'External Inputs/Notion'. "
                         "Useful for connector-driven runs where the explicit-flag form reads "
                         "more clearly. Combines with any positional args; at least one source required.")
    ap.add_argument("--target-words", type=int, default=50000)
    ap.add_argument("--max-files-per-chunk", type=int, default=45,
                    help="Max files per chunk (Lesson #81: 60+ causes schema collapse, default 45)")
    ap.add_argument("--stage-skip-pct", type=float, default=0.0,
                    help="Skip the most-recent N%% of files (e.g. 0.30 for 30%%, used by Stage 2 to skip Stage 1's slice)")
    ap.add_argument("--min-words", type=int, default=500)
    ap.add_argument("--skip-ai-extract", action="store_true", default=True)
    ap.add_argument("--out-prefix", default=None,
                    help="Name prefix for chunk files (written inside out_dir, e.g. 'notes' -> .notes_chunk_01)")
    ap.add_argument("--vault-root", default=None,
                    help="Root directory of the vault. Defaults to personal vault.")
    args = ap.parse_args()

    vault = Path(args.vault_root) if args.vault_root else DEFAULT_VAULT
    if not vault.exists():
        print(f"vault not found: {vault}", file=sys.stderr)
        sys.exit(1)

    # Combine positional folders + --include folders (de-duped, order-preserving)
    folder_args = list(args.corpus_folder) + list(args.include)
    if not folder_args:
        print("error: at least one folder required (positional arg or --include)", file=sys.stderr)
        sys.exit(2)
    seen: set[str] = set()
    folders = []
    for cf in folder_args:
        if cf in seen:
            continue
        seen.add(cf)
        f = vault / cf
        if not f.exists():
            print(f"folder not found: {f}", file=sys.stderr)
            sys.exit(1)
        folders.append(f)

    # Auto-detect graphify-out layout (Lesson #53):
    # - Personal vault: <vault>/⚙️ Meta/graphify-out/ for cache + chunks + graph
    # - Team vault layout: <vault>/graphify-out/cache/ at vault root (sibling of content folder),
    #   <vault>/<corpus_folder>/⚙️ Meta/graphify-out/ for chunks + graph
    onde_team_cache = vault / "graphify-out" / "cache"
    onde_team_out = folders[0] / "⚙️ Meta" / "graphify-out"
    personal_out = vault / "⚙️ Meta" / "graphify-out"
    if onde_team_cache.exists() and onde_team_out.exists():
        cache_dir = onde_team_cache
        out_dir = onde_team_out
        layout = "onde-team"
    else:
        cache_dir = personal_out / "cache"
        out_dir = personal_out
        layout = "personal"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Fix: --out-prefix is a NAME, not a path. Resolve inside out_dir.
    if args.out_prefix:
        out_prefix = str(out_dir / f".{args.out_prefix}_chunk_")
    else:
        out_prefix = str(out_dir / ".chunk_")
    print(f"Layout: {layout}")
    print(f"  cache_dir: {cache_dir}")
    print(f"  out_dir:   {out_dir}")

    # Lesson #68: exclude ⚙️ Meta/ and Archive/ from both vaults.
    # Also skip _review_alternate_drafts/ (quarantine folder) and conflict copies.
    SKIP_PARTS = {
        "_review_alternate_drafts", "⚙️ Meta", "Archive", "🗄 Archive",
        ".claude", ".git", ".obsidian", ".trash", "node_modules", "worktrees",
    }
    def skip(f):
        if any(p in SKIP_PARTS for p in f.parts):
            return True
        stem = f.stem
        # iCloud/GDrive conflict copies ("foo 2.md")
        if stem.endswith(" 2") or stem.endswith(" 3"):
            return True
        return False
    all_files = []
    for folder in folders:
        folder_files = [f for f in folder.rglob("*.md") if not skip(f)]
        all_files.extend(folder_files)
    folder_label = ", ".join(str(f.relative_to(vault)) for f in folders)
    print(f"Total files in [{folder_label}]: {len(all_files)}")

    if args.stage_skip_pct > 0:
        def sort_key(f):
            try:
                return datetime.strptime(f.stem[:10], "%Y-%m-%d").timestamp()
            except Exception:
                return f.stat().st_mtime
        all_files.sort(key=sort_key, reverse=True)
        skip_count = int(len(all_files) * args.stage_skip_pct)
        candidates = all_files[skip_count:]
        print(f"Skipping most-recent {args.stage_skip_pct*100:.0f}% ({skip_count} files)")
        print(f"Candidates: {len(candidates)}")
    else:
        candidates = all_files

    eligible = []
    skipped_small = 0
    skipped_ai = 0
    word_counts = {}
    for f in candidates:
        if args.skip_ai_extract and f.stem.startswith("[AI Extract]"):
            skipped_ai += 1
            continue
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        wc = len(text.split())
        word_counts[str(f)] = wc
        if wc < args.min_words:
            skipped_small += 1
            continue
        eligible.append(f)
    print(f"Eligible for LLM: {len(eligible)}")
    print(f"  Skipped <{args.min_words} words: {skipped_small}")
    if args.skip_ai_extract:
        print(f"  Skipped [AI Extract]: {skipped_ai}")

    # Cache check (LLM-only, per Lesson #39).
    # Lesson #93: mtime manifest short-circuits SHA-based lookup. If the file
    # hasn't been modified since its last LLM extraction, skip it regardless of
    # whether the SHA cache key matches. This prevents cosmetic edits (frontmatter
    # tweaks, wikilink fixes, whitespace) from triggering full re-extraction.
    manifest_path = out_dir / "extraction_manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text()).get("entries", {})
        except Exception:
            manifest = {}

    llm_hits = 0
    preflight_only_hits = 0
    mtime_hits = 0
    real_misses = []
    for f in eligible:
        try:
            abs_path = str(f.resolve())
            file_mtime = f.stat().st_mtime
            m_entry = manifest.get(abs_path)
            # Short-circuit on mtime manifest: if the file hasn't changed since
            # its last LLM extraction, treat as cached. 5-second slack absorbs
            # filesystem mtime precision variance between Python + the OS.
            if m_entry and file_mtime <= m_entry.get("llm_time", 0) + 5:
                mtime_hits += 1
                llm_hits += 1
                continue
            # Fall back to SHA-based cache check (covers moved/renamed files
            # that somehow preserved mtime, plus the first run after shipping
            # this fix when the manifest is still empty).
            # Lesson #94: graphify.cache hashes content + \x00 + relative_to(root).
            # Try both relative-to-vault and absolute path; the lib falls back
            # to absolute when relative_to raises ValueError.
            content = f.read_bytes()
            cache_file = None
            # Relative path variants to try for cache lookup
            candidate_paths = []
            try:
                candidate_paths.append(str(f.resolve().relative_to(vault.resolve())).encode())
            except ValueError:
                pass
            candidate_paths.append(abs_path.encode())
            for cp in candidate_paths:
                h = hashlib.sha256(content + b"\x00" + cp).hexdigest()
                cf = cache_dir / f"{h}.json"
                if cf.exists():
                    cache_file = cf
                    break
            if cache_file is not None:
                data = json.loads(cache_file.read_text())
                if is_llm_extraction(data):
                    llm_hits += 1
                else:
                    preflight_only_hits += 1
                    real_misses.append(f)
            else:
                real_misses.append(f)
        except Exception:
            real_misses.append(f)

    print()
    print(f"Cache breakdown (preflight-aware):")
    print(f"  LLM hits total:         {llm_hits}  (mtime-manifest: {mtime_hits}, SHA: {llm_hits - mtime_hits})")
    print(f"  Preflight-only (redo):  {preflight_only_hits}")
    print(f"  Total needing LLM work: {len(real_misses)}")

    # Bin-pack
    total_words = sum(word_counts[str(f)] for f in real_misses)
    target_chunks = max(1, (total_words + args.target_words - 1) // args.target_words)
    print()
    print(f"Total words needing LLM: {total_words:,}")
    print(f"Target chunks: {target_chunks}")

    max_fpc = args.max_files_per_chunk
    items = sorted([(f, word_counts[str(f)]) for f in real_misses], key=lambda x: -x[1])
    bins = [[] for _ in range(target_chunks)]
    bin_w = [0] * target_chunks
    for f, w in items:
        # Find lightest bin that hasn't hit file-count cap (Lesson #81)
        candidates = [(bin_w[i], i) for i in range(len(bins)) if len(bins[i]) < max_fpc]
        if not candidates:
            # All bins full, create a new one
            bins.append([])
            bin_w.append(0)
            candidates = [(0, len(bins) - 1)]
        _, idx = min(candidates)
        bins[idx].append(f)
        bin_w[idx] += w

    # Clean old chunk files matching this prefix pattern
    prefix_name = Path(out_prefix).name  # e.g. ".chunk_" or ".notes_chunk_"
    prefix_glob = prefix_name + "*"
    for old in out_dir.glob(prefix_glob + "_files.txt"):
        old.unlink()
    for old in out_dir.glob(prefix_glob + "_result.json"):
        old.unlink()

    for i, (b, w) in enumerate(zip(bins, bin_w), 1):
        Path(f"{out_prefix}{i:02d}_files.txt").write_text("\n".join(str(f) for f in b))
        print(f"  chunk_{i:02d}: {len(b)} files, {w:,} words")

    print()
    print(f"Total: {sum(len(b) for b in bins)} files, {sum(bin_w):,} words")
    naive = sum(bin_w) * 2.55 / 1000
    grep_first = sum(bin_w) * 2.55 * 0.54 / 1000  # 46% reduction per Lesson #42
    print(f"Expected cost naive: ~{naive:.0f}K tokens")
    print(f"With Grep-first (Lesson #42 — 46% reduction): ~{grep_first:.0f}K tokens")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
