#!/usr/bin/env python3
"""The journal index holds journals only — not the reports /weekly and /monthly write.

Proves build-journal-index.py skips a typed non-journal note living under the
journal folder (the insight reports in "Weekly Insights/" and "Monthly
Insights/"), while still indexing an entry that carries NO type field at all —
the shape of every entry written before the daily-journal template gained
`type: journal` (#379).

That second assertion is the load-bearing one: a filter written as
`type == "journal"` passes the exclusion half and silently empties the index on
any vault that journaled before #379.

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

# Fixtures keyed by path relative to the journal dir.
FIXTURES = {
    "typed-journal.md": (
        "---\ncreationDate: 2026-08-01\ntype: journal\nfloor: Gratitude\n---\nlived it.\n"
    ),
    # No type field: every entry written before #379 looks like this.
    "legacy-untyped.md": (
        "---\ncreationDate: 2026-08-02\nfloor: Reason\n---\nalso lived it.\n"
    ),
    "Weekly Insights/2026-W31.md": (
        "---\ncreationDate: 2026-08-03\ntype: insight\nperiod: weekly\n"
        "primary_floor: Courage\n---\na report about the week.\n"
    ),
    "Monthly Insights/2026-07.md": (
        "---\ncreationDate: 2026-08-03\ntype: insight\nperiod: monthly\n"
        "primary_floor: Gratitude\n---\na report about the month.\n"
    ),
}

INDEXED = ("typed-journal.md", "legacy-untyped.md")
EXCLUDED = ("Weekly Insights", "Monthly Insights")


def _build_index(vault: Path):
    journals = vault / "Journals"
    journals.mkdir()
    (vault / "Meta").mkdir()
    for rel, text in FIXTURES.items():
        dest = journals / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    # encoding/errors are explicit for the same reason this whole family of
    # fixes exists: text=True alone decodes the child's stdout with
    # locale.getpreferredencoding(), which is cp1252 on a stock Windows box.
    # build-journal-index.py prints the indexed path, so a vault path with one
    # accent turns this test's own failure report into a UnicodeDecodeError.
    res = subprocess.run(
        [sys.executable, str(BUILD), "--vault-root", str(vault), "--journal-dir", "Journals"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if res.returncode != 0:
        print("FAIL: build-journal-index.py errored:")
        print((res.stdout + res.stderr).strip())
        return None
    idx = json.loads((vault / "Meta" / "journal-index.json").read_text(encoding="utf-8"))
    return idx


def test_type_filter() -> bool:
    with tempfile.TemporaryDirectory(prefix="type-filter-idx-") as tmp:
        idx = _build_index(Path(tmp))
        if idx is None:
            return False
        ok = True
        # os.walk builds nested paths with the platform separator; normalize.
        files = [e["file"].replace("\\", "/") for e in idx["entries"]]

        for name in INDEXED:
            if name not in files:
                print(f"FAIL: {name} missing from the index (indexed: {files})")
                ok = False

        for folder in EXCLUDED:
            leaked = [f for f in files if f.startswith(f"{folder}/")]
            if leaked:
                print(f"FAIL: {folder} report(s) indexed as journal entries: {leaked}")
                ok = False

        if len(files) != len(INDEXED):
            print(f"FAIL: expected {len(INDEXED)} entries, got {len(files)}: {files}")
            ok = False

        if idx.get("total") != len(files):
            print(f"FAIL: total {idx.get('total')!r} disagrees with {len(files)} entries")
            ok = False

        if ok:
            print("OK: insight reports excluded; typed AND untyped journals both indexed")
        return ok


def main() -> int:
    return 0 if test_type_filter() else 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
