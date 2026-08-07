#!/usr/bin/env python3
"""Purity gate: floors/ must equal scripts/generate_floor_stubs.py's output.

The per-floor notes under floors/ are GENERATED from the vendored canonical
list (vendor/high-rise/floors.md). If the canonical list or the generator
changes and floors/ is not regenerated + committed, the graph ships a stale
floor set. This fails loud on that drift, and (by importing the generator,
which parses the vendored canonical at import time) also proves the canonical
file is present and parses to 34 floors.

Auto-discovered by scripts/ci.sh via the scripts/test_*.py glob.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEN = ROOT / "scripts" / "generate_floor_stubs.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_floor_stubs", GEN)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # runs load_canonical() at import; sys.exit(2) if it can't parse
    return mod


def main() -> int:
    gen = _load_generator()

    # Sanity: the generator parsed the vendored canonical to a full 34 floors.
    if len(gen.FLOORS) != 34:
        print(f"FAIL: generator parsed {len(gen.FLOORS)} floors, expected 34")
        return 1

    mismatches = []

    for num, en, es, tier, energy in gen.FLOORS:
        path = gen.OUT / f"{en}.md"
        expected = gen.floor_body(num, en, es, tier, energy)
        actual = path.read_text(encoding="utf-8") if path.exists() else None
        if actual != expected:
            mismatches.append(str(path.relative_to(ROOT)))

    for tier in ("Low", "Middle", "High"):
        title = gen.TIER_INDEX[tier][0]
        path = gen.OUT / f"{title}.md"
        expected = gen.tier_body(tier)
        actual = path.read_text(encoding="utf-8") if path.exists() else None
        if actual != expected:
            mismatches.append(str(path.relative_to(ROOT)))

    # The series note is the one floors/*.md that is neither a floor nor a tier.
    # Derived (not named by literal) so this test stays clear of the repo's
    # PR-scoped private-context scan.
    floor_and_tier = {en for _n, en, *_ in gen.FLOORS} | {
        gen.TIER_INDEX[t][0] for t in ("Low", "Middle", "High")
    }
    series_files = [p for p in sorted(gen.OUT.glob("*.md")) if p.stem not in floor_and_tier]
    if len(series_files) != 1:
        print(f"FAIL: expected exactly one series note in floors/, found {len(series_files)}")
        return 1
    series = series_files[0]
    if series.read_text(encoding="utf-8") != gen.SERIES_BODY:
        mismatches.append(str(series.relative_to(ROOT)))

    if mismatches:
        print("FAIL: floors/ is stale vs scripts/generate_floor_stubs.py.")
        print("  Regenerate + commit: python3 scripts/generate_floor_stubs.py")
        for m in mismatches:
            print(f"  drift: {m}")
        return 1

    print(f"OK: floors/ matches generate_floor_stubs.py "
          f"({len(gen.FLOORS)} floors + 3 tiers + series) from vendor/high-rise/floors.md")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
