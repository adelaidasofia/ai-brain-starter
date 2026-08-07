#!/usr/bin/env python3
"""floor_arc round-trips through the journal index + is declared in the schema.

Proves build-journal-index.py indexes `floor_arc` as a real list (not the raw
"[...]" string), leaves a still-day entry without an arc, preserves the primary
`floor`, and that templates/schemas/journal.json declares `floor_arc` +
`floor_level`. The last-element-of-the-arc == primary-`floor` contract (the
spec) is also asserted on the moved-day fixture.

Auto-discovered by scripts/ci.sh via the scripts/test_*.py glob.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "scripts" / "build-journal-index.py"
SCHEMA = ROOT / "templates" / "schemas" / "journal.json"

# Fixtures: a day that moved (has an arc), a still day (no arc), a legacy
# array-floor entry (must still index without crashing).
FIXTURES = {
    "moved-day.md": (
        "---\ncreationDate: 2026-04-11\nfloor: Hope\nfloor_level: Middle\n"
        "floor_arc: [Fear, Frustration, Hope]\n---\nmoved.\n"
    ),
    "still-day.md": (
        "---\ncreationDate: 2026-04-12\nfloor: Peace\nfloor_level: High\n---\nflat.\n"
    ),
    "legacy-array.md": (
        "---\ncreationDate: 2026-04-13\nfloor: [Grief, Love]\nfloor_level: High\n---\nlegacy.\n"
    ),
}


def _build_index(vault: Path):
    (vault / "Journals").mkdir()
    (vault / "Meta").mkdir()
    for name, text in FIXTURES.items():
        (vault / "Journals" / name).write_text(text, encoding="utf-8")
    res = subprocess.run(
        [sys.executable, str(BUILD), "--vault-root", str(vault), "--journal-dir", "Journals"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        print("FAIL: build-journal-index.py errored:")
        print((res.stdout + res.stderr).strip())
        return None
    idx = json.loads((vault / "Meta" / "journal-index.json").read_text(encoding="utf-8"))
    return {e["file"]: e for e in idx["entries"]}


def test_index() -> bool:
    with tempfile.TemporaryDirectory(prefix="floor-arc-idx-") as tmp:
        by_file = _build_index(Path(tmp))
        if by_file is None:
            return False
        ok = True
        moved = by_file.get("moved-day.md", {})
        if moved.get("floor_arc") != ["Fear", "Frustration", "Hope"]:
            print(f"FAIL: floor_arc not indexed as a list: {moved.get('floor_arc')!r}")
            ok = False
        if moved.get("floor") != "Hope":
            print(f"FAIL: primary floor lost/altered: {moved.get('floor')!r}")
            ok = False
        # Spec contract: the arc's last element is where the entry landed == floor.
        arc = moved.get("floor_arc")
        if isinstance(arc, list) and arc and arc[-1] != moved.get("floor"):
            print(f"FAIL: arc last element {arc[-1]!r} != primary floor {moved.get('floor')!r}")
            ok = False
        if "floor_arc" in by_file.get("still-day.md", {}):
            print("FAIL: a still day was given a floor_arc")
            ok = False
        if "legacy-array.md" not in by_file:
            print("FAIL: legacy array-floor entry dropped from the index")
            ok = False
        if ok:
            print("OK: floor_arc round-trips as a list; still day has none; "
                  "primary preserved; legacy array-floor still indexes")
        return ok


def test_schema() -> bool:
    props = json.loads(SCHEMA.read_text(encoding="utf-8"))["properties"]
    ok = True
    fa = props.get("floor_arc")
    if not isinstance(fa, dict) or fa.get("type") != "array" or fa.get("items", {}).get("type") != "string":
        print(f"FAIL: journal.json floor_arc missing / not array-of-strings: {fa!r}")
        ok = False
    if "floor_level" not in props:
        print("FAIL: journal.json missing floor_level")
        ok = False
    if ok:
        print("OK: journal.json declares floor_arc (array of strings) + floor_level")
    return ok


def main() -> int:
    ok = test_schema()
    ok = test_index() and ok
    return 0 if ok else 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
