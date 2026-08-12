#!/usr/bin/env python3
"""Unit suite for the derived-report exclusion in build-journal-index.py.

THE BUG THIS GUARDS (INSIGHT-REPORTS-INDEXED-AS-ENTRIES): the /weekly and
/monthly reports are DERIVED FROM the journal index, and the canonical report
frontmatter in insights/SKILL.md ("Format:" block) carries `creationDate` plus
`type: insight`. The indexer's only admission test was `if "creationDate" in
meta`, and its walk is recursive over the whole journal folder, so every report
was re-admitted as a floorless pseudo-entry: `total` inflates, the floor
distribution the NEXT report is computed from gets diluted, and the error
compounds run over run because each pass indexes the previous pass's output.

Folder placement never protected against this. `os.walk(journal_dir)` reaches
`Weekly Insights/` exactly as it reaches `June 2026/`, so the move of reports
into month folders neither caused nor fixed it. `type` is the honest signal, so
that is what the exclusion keys on.

Run directly (the ci.sh gate globs scripts/test_*.py):
    python3 scripts/test_journal_index_derived.py
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE / "build-journal-index.py"


def _load_indexer():
    """Load build-journal-index.py (hyphenated -> importlib) so the constants
    under test are the SHIPPED ones, not a copy."""
    spec = importlib.util.spec_from_file_location("journal_index_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


IDX = _load_indexer()


ENTRY = """---
creationDate: 2026-06-11
floor: Reason
floor_level: high
---

# A real day

Body.
"""

# The canonical report shape, copied from insights/SKILL.md's Format block.
REPORT = """---
creationDate: 2026-06-15
type: insight
period: weekly
date_range: 2026-06-09 to 2026-06-15
entries_analyzed: 4
primary_floor: Reason
---

# Weekly report

Body.
"""


class DerivedReportExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.journals = self.vault / "📓 Journals"
        self.meta = self.vault / "⚙️ Meta"
        (self.journals / "June 2026").mkdir(parents=True)
        (self.journals / "Weekly Insights").mkdir(parents=True)
        self.meta.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, text: str) -> None:
        p = self.journals / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def _run(self) -> dict:
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "--vault-root", str(self.vault)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._stderr = proc.stderr
        return json.loads((self.meta / "journal-index.json").read_text(encoding="utf-8"))

    # --- the headline case --------------------------------------------------
    def test_report_in_month_folder_is_not_indexed(self):
        self._write("June 2026/2026-06-11 A Real Day.md", ENTRY)
        self._write("June 2026/Jun. 9-15, 2026 Weekly.md", REPORT)
        idx = self._run()
        self.assertEqual(idx["total"], 1)
        self.assertEqual([e["file"] for e in idx["entries"]],
                         ["June 2026/2026-06-11 A Real Day.md"])

    def test_report_in_insights_folder_is_not_indexed_either(self):
        # The exclusion is by `type`, not by folder, so BOTH save conventions
        # behave identically. This is the test that would have failed under a
        # folder-based fix.
        self._write("June 2026/2026-06-11 A Real Day.md", ENTRY)
        self._write("Weekly Insights/weekly-review-2026-06-15.md", REPORT)
        idx = self._run()
        self.assertEqual(idx["total"], 1)

    def test_legacy_report_types_are_excluded(self):
        self._write("June 2026/2026-06-11 A Real Day.md", ENTRY)
        self._write("Weekly Insights/old-weekly.md",
                    REPORT.replace("type: insight", "type: weekly-review"))
        self._write("Weekly Insights/old-monthly.md",
                    REPORT.replace("type: insight", "type: monthly-review"))
        idx = self._run()
        self.assertEqual(idx["total"], 1)

    # --- scoping guards: the exclusion must not eat real entries ------------
    def test_real_entries_are_untouched(self):
        self._write("June 2026/2026-06-11 A Real Day.md", ENTRY)
        self._write("June 2026/2026-06-14 Another.md",
                    ENTRY.replace("2026-06-11", "2026-06-14").replace("Reason", "Courage"))
        idx = self._run()
        self.assertEqual(idx["total"], 2)
        self.assertEqual({e["floor"] for e in idx["entries"]}, {"Reason", "Courage"})

    def test_entry_with_an_unrelated_type_is_still_indexed(self):
        # Only the derived-output types are excluded. A journal entry that
        # happens to carry `type: journal` (or anything else) stays in.
        self._write("June 2026/2026-06-11 Typed.md",
                    ENTRY.replace("floor: Reason", "type: journal\nfloor: Reason"))
        idx = self._run()
        self.assertEqual(idx["total"], 1)

    def test_exclusion_is_reported_never_silent(self):
        # The house rule: nothing disappears without saying so.
        self._write("June 2026/2026-06-11 A Real Day.md", ENTRY)
        self._write("Weekly Insights/weekly-review-2026-06-15.md", REPORT)
        self._run()
        self.assertIn("excluded 1 derived report", self._stderr)
        self.assertIn("weekly-review-2026-06-15.md", self._stderr)

    def test_no_reports_means_no_note(self):
        self._write("June 2026/2026-06-11 A Real Day.md", ENTRY)
        self._run()
        self.assertNotIn("derived report", self._stderr)

    # --- the constant itself ------------------------------------------------
    def test_derived_types_cover_the_shipped_report_format(self):
        self.assertIn("insight", IDX.DERIVED_TYPES)
        self.assertNotIn("journal", IDX.DERIVED_TYPES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
