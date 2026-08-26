#!/usr/bin/env python3
"""
graphify_stage_select.py — Stage selection for the graphify staged rollout.

Walks a corpus folder, applies the standard filters (>= MIN_WORDS, no [AI Extract]),
checks the cache for REAL LLM extractions (not preflight stubs), and bin-packs
the misses into ~50K-word chunks ready for parallel dispatch.

Why this exists (Lesson #18 in RUNBOOK.md):
The graphify cache stores BOTH preflight regex stubs AND real LLM extractions
under the same SHA256 keying. A naive "does a cache file exist?" check counts
preflight-only entries as "done" and reports `0 tokens needed` even when the
semantic LLM layer is missing. This script discriminates the two via
is_llm_extraction() so you only re-run the files that genuinely need LLM work.

LLM cache entry signature (any ONE is sufficient):
  - has non-empty hyperedges list
  - any edge has confidence != "EXTRACTED"
  - any edge has confidence_score != 1.0

Preflight-only signature: structural relations ("references", "expresses_floor")
with confidence="EXTRACTED" and score=1.0. These are NOT real semantic hits —
re-extract.

Run from your vault root (or pass --vault-root). The cache is expected at
`<vault-root>/graphify-out/cache/`.

Usage:
    python3 graphify_stage_select.py <corpus_folder> [--target-words 50000] \
        [--stage-skip-pct 0.0] [--vault-root .]

Examples:
    # Full pass on a folder
    python3 graphify_stage_select.py "Notes"

    # Skip the most-recent 30% (already done in a prior stage), take the rest
    python3 graphify_stage_select.py "Journals" --stage-skip-pct 0.30
"""
import argparse
import json
import hashlib
import os
import re
import sys
from pathlib import Path
from datetime import datetime


# ── Cache key ────────────────────────────────────────────────────────────────
#
# ⚠️ This key MUST be byte-identical to graphify.cache.file_hash(). Hand-rolled
# variants have drifted from it four ways at once, and the symptom is silent:
# the sizer reports 0 cache hits against a perfectly good cache and bills the
# operator again for work already paid for. The four:
#   1. entries live under cache/semantic/ (and, for newer ones, one level
#      deeper under a p{fingerprint}/ directory) — a flat cache/ lookup misses
#   2. the digest takes the path RELATIVE to the vault root, lowercased, so a
#      cache survives the vault being moved or shared between machines
#   3. for .md the library hashes only the body BELOW the YAML frontmatter, so
#      a metadata-only rewrite does not invalidate an expensive extraction
#   4. swallowing errors into the miss pile hides all of the above
#
# ALWAYS prefer the library function. The local reimplementation exists for the
# common case where this script runs under a python without graphify installed
# (graphify usually lives in a virtualenv the wrapper scripts do not share).

try:
    from graphify.cache import file_hash as _lib_file_hash
except Exception:
    _lib_file_hash = None

_FRONTMATTER_DELIM = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)


def _body_content(content: bytes) -> bytes:
    """Strip YAML frontmatter. Mirrors graphify.cache._body_content."""
    text = content.decode(errors="replace")
    opener = _FRONTMATTER_DELIM.match(text)
    if opener is None:
        return content
    closer = _FRONTMATTER_DELIM.search(text, opener.end())
    if closer is None:
        return content
    return text[closer.start() + 3:].encode()


def cache_key(path: Path, root: Path) -> str:
    """Cache key for a file. Delegates to the library when it is importable."""
    if _lib_file_hash is not None:
        return _lib_file_hash(path, root=root)
    raw = path.read_bytes()
    content = _body_content(raw) if path.suffix.lower() == ".md" else raw
    resolved = path.resolve()
    try:
        salt = resolved.relative_to(Path(root).resolve()).as_posix().lower()
    except ValueError:
        salt = resolved.as_posix().lower()
    h = hashlib.sha256()
    h.update(content)
    h.update(b"\x00")
    h.update(salt.encode())
    return h.hexdigest()


def find_cache_entry(cache_base: Path, key: str):
    """Find a cache entry under any layout this repo has shipped.

    Deliberately permissive: checks the base directory, the semantic/
    subdirectory, and one level of p{fingerprint}/ nesting under either. It can
    only ever find more than a flat lookup, never fewer, so it is safe across
    the personal and team cache layouts without needing to know which is active.
    """
    for base in (cache_base, cache_base / "semantic"):
        if not base.is_dir():
            continue
        flat = base / f"{key}.json"
        if flat.exists():
            return flat
        for sub in base.iterdir():
            if sub.is_dir():
                cand = sub / f"{key}.json"
                if cand.exists():
                    return cand
    return None


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
    ap.add_argument("corpus_folder",
                    help="Folder relative to vault root, e.g. 'Notes' or 'Journals'")
    ap.add_argument("--vault-root", default=".",
                    help="Vault root directory (default: current working directory)")
    ap.add_argument("--target-words", type=int, default=50000)
    ap.add_argument("--stage-skip-pct", type=float, default=0.0,
                    help="Skip the most-recent N%% of files (e.g. 0.30 for 30%%)")
    ap.add_argument("--min-words", type=int, default=500)
    ap.add_argument("--skip-ai-extract", action="store_true", default=True)
    ap.add_argument("--out-prefix", default="graphify-out/.chunk_")
    args = ap.parse_args()

    vault = Path(args.vault_root).resolve()
    cache_dir = vault / "graphify-out" / "cache"
    os.chdir(vault)

    folder = Path(args.corpus_folder)
    if not folder.exists():
        print(f"folder not found: {folder}", file=sys.stderr)
        sys.exit(1)

    all_files = [f for f in folder.rglob("*.md") if "_review_alternate_drafts" not in f.parts]
    print(f"Total files in {folder}: {len(all_files)}")

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
            text = f.read_text(encoding="utf-8", errors="ignore")
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

    # Cache check (LLM-only — see is_llm_extraction docstring)
    llm_hits = 0
    preflight_only_hits = 0
    real_misses = []
    errors = []
    for f in eligible:
        try:
            cache_file = find_cache_entry(cache_dir, cache_key(f, vault))
            if cache_file is not None:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if is_llm_extraction(data):
                    llm_hits += 1
                else:
                    preflight_only_hits += 1
                    real_misses.append(f)
            else:
                real_misses.append(f)
        except Exception as exc:
            # NEVER silently: an error is not a cache miss, and counting it as one
            # inflates the cost estimate without anyone noticing.
            errors.append((f, exc))
            real_misses.append(f)
    if errors:
        print(f"  ⚠️  {len(errors)} file(s) could not be checked against the cache:")
        for f, exc in errors[:5]:
            print(f"      {f}: {type(exc).__name__}: {exc}")

    print()
    print(f"Cache breakdown (preflight-aware):")
    print(f"  Real LLM hits:          {llm_hits}")
    print(f"  Preflight-only (redo):  {preflight_only_hits}")
    print(f"  Total needing LLM work: {len(real_misses)}")

    # Wrong-root sanity check (Lesson #46): if a folder previously had a graph
    # built and you suddenly see 0% cache hits, you're almost certainly running
    # against the wrong vault root. Warn loudly so the operator catches it
    # before dispatching expensive subagents.
    if eligible and llm_hits == 0 and (eligible[0].parent.parent / "graphify-out" / "graph.json").exists():
        print()
        print("⚠️  WARNING: 0 cache hits but a graph.json exists nearby.")
        print("    You may be running against the wrong vault root.")
        print("    Verify --vault-root points at the same directory the cache was built from.")

    # Bin-pack
    total_words = sum(word_counts[str(f)] for f in real_misses)
    target_chunks = max(1, (total_words + args.target_words - 1) // args.target_words)
    print()
    print(f"Total words needing LLM: {total_words:,}")
    print(f"Target chunks: {target_chunks}")

    items = sorted([(f, word_counts[str(f)]) for f in real_misses], key=lambda x: -x[1])
    bins = [[] for _ in range(target_chunks)]
    bin_w = [0] * target_chunks
    for f, w in items:
        idx = bin_w.index(min(bin_w))
        bins[idx].append(f)
        bin_w[idx] += w

    # Clean old chunk files matching this prefix
    for old in Path("graphify-out").glob(".chunk_*_files.txt"):
        old.unlink()
    for old in Path("graphify-out").glob(".chunk_*_result.json"):
        old.unlink()

    for i, (b, w) in enumerate(zip(bins, bin_w), 1):
        Path(f"{args.out_prefix}{i:02d}_files.txt").write_text(
            "\n".join(str(f) for f in b), encoding="utf-8"
        )
        print(f"  chunk_{i:02d}: {len(b)} files, {w:,} words")

    print()
    print(f"Total: {sum(len(b) for b in bins)} files, {sum(bin_w):,} words")
    naive = sum(bin_w) * 2.55 / 1000
    grep_first = sum(bin_w) * 2.55 * 0.54 / 1000  # 46% reduction with Grep-first prompt
    print(f"Expected cost naive: ~{naive:.0f}K tokens")
    print(f"With Grep-first prompt (RUNBOOK Lesson #25): ~{grep_first:.0f}K tokens")


if __name__ == "__main__":
    main()
