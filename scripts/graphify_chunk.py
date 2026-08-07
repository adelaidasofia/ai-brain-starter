#!/usr/bin/env python3
"""
graphify_chunk.py — Word-balanced chunking for graphify subagent dispatch.

Uses greedy bin-packing to spread files across N chunks of equal *word count*,
not equal *file count*. This avoids the slow-stragglers problem where one chunk
has 100K words and another has 2K — the long chunk takes 5x longer and risks
context overflow.

Defaults to 50 files per chunk (vs the skill's 20-25), which cuts subagent count
by 60% and reduces prompt-instruction redundancy proportionally. Larger chunks
are safe because Claude subagents have plenty of context budget; the bottleneck
was per-chunk overhead, not per-chunk size.

Skips files that the LLM extraction phase shouldn't process:
  - Files <500 words (too short for meaningful inferred edges)
  - "_review_alternate_drafts/" quarantine
  - Optional: "[AI Extract]" prefixed files (already-LLM-extracted summaries)

Usage:
    python3 graphify_chunk.py <input_dir> --out-dir graphify-out [--target-chunks 12] [--skip-ai-extract]
"""

import sys
import argparse
import json
from pathlib import Path
from typing import Iterable


MIN_WORDS_FOR_LLM = 500


def file_word_count(path: Path) -> int:
    try:
        return len(path.read_text(errors="ignore").split())
    except Exception:
        return 0


def is_excluded(path: Path, exclude_patterns: list) -> bool:
    """Return True if any path component matches an exclude pattern (case-insensitive substring)."""
    if not exclude_patterns:
        return False
    parts_lower = [p.lower() for p in path.parts]
    for pat in exclude_patterns:
        pat_lower = pat.lower()
        if any(pat_lower in part for part in parts_lower):
            return True
    return False


def collect_files(input_dir: Path, skip_ai_extract: bool, min_words: int,
                  exclude_patterns: list = None) -> list:
    """Return [(file, word_count)] for files eligible for LLM extraction."""
    exclude_patterns = exclude_patterns or []
    out = []
    skipped_short = skipped_quarantine = skipped_ai = skipped_excluded = 0
    for f in sorted(input_dir.rglob("*.md")):
        if "_review_alternate_drafts" in f.parts:
            skipped_quarantine += 1
            continue
        if is_excluded(f, exclude_patterns):
            skipped_excluded += 1
            continue
        if skip_ai_extract and f.name.startswith("[AI Extract]"):
            skipped_ai += 1
            continue
        wc = file_word_count(f)
        if wc < min_words:
            skipped_short += 1
            continue
        out.append((f, wc))
    print(f"  skipped: {skipped_short} short, {skipped_quarantine} quarantine, "
          f"{skipped_ai} ai-extract, {skipped_excluded} excluded")
    return out


def greedy_balance(files: list[tuple[Path, int]], target_chunks: int) -> list[list[Path]]:
    """
    Greedy bin-packing: sort files by word count desc, place each in the
    currently-smallest bin. Produces chunks of similar total word count.
    """
    files_sorted = sorted(files, key=lambda x: -x[1])
    bins: list[tuple[int, list[Path]]] = [(0, []) for _ in range(target_chunks)]

    for f, wc in files_sorted:
        min_idx = min(range(target_chunks), key=lambda i: bins[i][0])
        total, contents = bins[min_idx]
        contents.append(f)
        bins[min_idx] = (total + wc, contents)

    return [b[1] for b in bins]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_dir")
    ap.add_argument("--out-dir", default="graphify-out")
    ap.add_argument("--target-chunks", type=int, default=12,
                    help="number of chunks to produce (default 12)")
    ap.add_argument("--min-words", type=int, default=MIN_WORDS_FOR_LLM,
                    help=f"skip files smaller than this (default {MIN_WORDS_FOR_LLM})")
    ap.add_argument("--skip-ai-extract", action="store_true",
                    help="skip [AI Extract]-prefixed files (homogeneous LLM outputs, low inference yield)")
    ap.add_argument("--exclude", action="append", default=[],
                    help="path substring to exclude (case-insensitive). Repeat for multiple. "
                         "Example: --exclude Archive --exclude .obsidian")
    args = ap.parse_args()

    input_dir = Path(args.input_dir).resolve()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)

    print(f"Collecting files from {input_dir}...")
    if args.exclude:
        print(f"  excluding: {args.exclude}")
    files = collect_files(input_dir, args.skip_ai_extract, args.min_words, args.exclude)
    total_words = sum(wc for _, wc in files)
    print(f"  {len(files)} files, {total_words:,} words eligible for LLM extraction")
    print()

    print(f"Bin-packing into {args.target_chunks} chunks (target ~{total_words // args.target_chunks:,} words each)...")
    chunks = greedy_balance(files, args.target_chunks)

    for i, chunk in enumerate(chunks, 1):
        chunk_words = sum(file_word_count(p) for p in chunk)
        chunk_path = out_dir / f".chunk_{i:02d}_files.txt"
        vault_root = input_dir.parent
        rel_paths = [str(p.relative_to(vault_root)) for p in chunk]
        chunk_path.write_text("\n".join(rel_paths))
        print(f"  chunk_{i:02d}: {len(chunk):3} files, {chunk_words:6,} words")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
