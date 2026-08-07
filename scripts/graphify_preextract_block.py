#!/usr/bin/env python3
"""
graphify_preextract_block.py — Generate {PREEXTRACT_BLOCK} for a given chunk

Usage:
    python3 graphify_preextract_block.py <chunk_files_path>

Reads .minimax_preextract.json and the chunk's file list,
outputs the formatted PREEXTRACT_BLOCK text to stdout.
If no pre-extract exists, outputs empty string (safe fallback).

Example:
    block=$(python3 scripts/graphify_preextract_block.py "Meta/graphify-out/.chunk_01_files.txt")
"""

import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("Usage: graphify_preextract_block.py <chunk_files_path>", file=sys.stderr)
        sys.exit(1)

    chunk_files_path = Path(sys.argv[1])
    preextract_path = chunk_files_path.parent / ".minimax_preextract.json"

    if not preextract_path.exists():
        sys.exit(0)

    preextract = json.loads(preextract_path.read_text())
    files_data = preextract.get("files", {})

    if not files_data:
        sys.exit(0)

    chunk_files = [
        line.strip() for line in chunk_files_path.read_text().splitlines()
        if line.strip()
    ]

    matched = []
    for cf in chunk_files:
        basename = Path(cf).name
        for pf, data in files_data.items():
            if pf == cf or Path(pf).name == basename:
                matched.append((cf, data))
                break

    if not matched:
        sys.exit(0)

    lines = [
        "PRE-EXTRACTED ENTITY DATA (from a cheap extractor model, skip entity discovery, focus on cross-file inference):",
        ""
    ]

    for filepath, data in matched:
        lines.append(f"File: {filepath}")
        for key in ["people", "places", "companies", "concepts", "frameworks", "emotions", "decisions", "key_relationships"]:
            val = data.get(key, [])
            if val:
                if isinstance(val, list):
                    val_str = ", ".join(str(v) for v in val)
                else:
                    val_str = str(val)
                label = key.replace("_", " ").title()
                lines.append(f"  {label}: {val_str}")
        lines.append("")

    print("\n".join(lines))


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
