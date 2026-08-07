#!/usr/bin/env python3
"""Unit suite for the skill-copy drift classifier in sync-skills.py (MYC-3076).

THE BUG THIS GUARDS (LIVE-SKILL-COPY-DRIFT): the deployed ai-brain-starter clone
(~/.claude/skills/ai-brain-starter) self-updates on the ~6-day auto-update, but
the propagation of that skill CONTENT into the bare copies that actually serve a
skill (~/.claude/skills/<name>, plain dirs) runs ONLY inside the auto-update's
`head != origin` branch. Once the clone reaches origin/main by any path, sync
never re-fires, so the clone can sit AHEAD of the bare copies indefinitely with
zero signal — the exact silent-drift class MYC-720 fought, one level up. On
2026-07-14 the daily-journal + insights movement mechanics reached the clone but
NOT the bare copies serving /journal and /weekly.

classify_drift() is the detector. It is DIRECTIONAL on purpose: a bare copy that
LEADS upstream (a local edit later upstreamed — e.g. the array-floor form) must
never be reported as "behind", or the surface would nag the user to overwrite
their own newer work with an older version. Only upstream-ahead content counts.

Run directly (the ci.sh gate globs scripts/test_*.py): python3 scripts/test_skill_copy_drift.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_sync_skills():
    """Load sync-skills.py (hyphenated -> importlib) so we test the SHIPPED
    classifier, not a copy. Mirrors surface-deployed-hooks-behind.py's import."""
    path = _HERE / "sync-skills.py"
    spec = importlib.util.spec_from_file_location("sync_skills_under_test", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SS = _load_sync_skills()


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _skill(root: Path, name: str, body: str) -> None:
    """Create <root>/<name>/SKILL.md with the given body."""
    _write(root / name / "SKILL.md", body)


# Realistic SKILL.md fragments: the clone gained the movement sections the bare
# copy lacks (the 2026-07-14 case). Headings are what the classifier keys on.
BARE_JOURNAL = """---
name: daily-journal
---
## Setup
Body.
### Step 4: Identify the floor
Name the floor.
### Step 7: Save the entry
Save it.
"""

CLONE_JOURNAL_AHEAD = """---
name: daily-journal
---
## Setup
Body.
## Crisis protocol
Safety override.
### Step 4: Identify the floor
Name the floor.
### Step 6.5: The door
One small action.
### Step 7: Save the entry
Save it.
"""


class ClassifyDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.clone = base / "clone" / "skills"   # ai-brain-starter/skills
        self.install = base / "install"          # ~/.claude/skills
        self.clone.mkdir(parents=True)
        self.install.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _by_name(self, drifts):
        return {d["name"]: d for d in drifts}

    # --- negative control: identical copy is silent -------------------------
    def test_identical_is_silent(self):
        _skill(self.clone, "daily-journal", BARE_JOURNAL)
        _skill(self.install, "daily-journal", BARE_JOURNAL)
        self.assertEqual(SS.classify_drift(self.clone, self.install), [])

    # --- the headline case: clone gained sections the bare copy lacks -------
    def test_behind_reports_missing_sections(self):
        _skill(self.clone, "daily-journal", CLONE_JOURNAL_AHEAD)
        _skill(self.install, "daily-journal", BARE_JOURNAL)
        d = self._by_name(SS.classify_drift(self.clone, self.install))["daily-journal"]
        self.assertEqual(d["status"], "behind")
        joined = " | ".join(d["missing_sections"]).lower()
        self.assertIn("crisis protocol", joined)
        self.assertIn("step 6.5", joined)
        # It must NOT invent extra_sections for a clean upstream-ahead case.
        self.assertEqual(d["extra_sections"], [])

    # --- directional guard: a bare copy that LEADS upstream is NOT "behind" -
    def test_leads_is_not_behind(self):
        # install has a section the clone lacks (local edit not yet upstreamed).
        _skill(self.clone, "daily-journal", BARE_JOURNAL)
        _skill(self.install, "daily-journal", CLONE_JOURNAL_AHEAD)
        d = self._by_name(SS.classify_drift(self.clone, self.install))["daily-journal"]
        self.assertEqual(d["status"], "leads")

    # --- both sides have unique sections -> diverged ------------------------
    def test_diverged_when_both_have_unique_sections(self):
        _skill(self.clone, "daily-journal", BARE_JOURNAL + "\n## Upstream Only\nx\n")
        _skill(self.install, "daily-journal", BARE_JOURNAL + "\n## Local Only\ny\n")
        d = self._by_name(SS.classify_drift(self.clone, self.install))["daily-journal"]
        self.assertEqual(d["status"], "diverged")
        self.assertTrue(any("upstream only" in s.lower() for s in d["missing_sections"]))

    # --- same headings, changed body -> content drift (surfaced, softer) ----
    def test_body_change_same_headings_is_content(self):
        _skill(self.clone, "daily-journal", BARE_JOURNAL.replace("Save it.", "Save it, verbatim, in place."))
        _skill(self.install, "daily-journal", BARE_JOURNAL)
        d = self._by_name(SS.classify_drift(self.clone, self.install))["daily-journal"]
        self.assertEqual(d["status"], "content")
        self.assertEqual(d["missing_sections"], [])

    # --- skip guards mirror sync-skills' own overwrite guards ---------------
    def test_symlinked_install_is_skipped(self):
        if os.name == "nt":
            self.skipTest("symlink creation needs privilege on Windows")
        _skill(self.clone, "daily-journal", CLONE_JOURNAL_AHEAD)
        real = Path(self._tmp.name) / "real-journal"
        _write(real / "SKILL.md", BARE_JOURNAL)
        (self.install / "daily-journal").symlink_to(real, target_is_directory=True)
        self.assertEqual(SS.classify_drift(self.clone, self.install), [])

    def test_git_fork_install_is_skipped(self):
        _skill(self.clone, "daily-journal", CLONE_JOURNAL_AHEAD)
        _skill(self.install, "daily-journal", BARE_JOURNAL)
        (self.install / "daily-journal" / ".git").mkdir()
        self.assertEqual(SS.classify_drift(self.clone, self.install), [])

    # --- a skill only in the clone (no bare copy) is not a drift ------------
    def test_clone_only_skill_is_not_reported(self):
        _skill(self.clone, "brand-new-skill", CLONE_JOURNAL_AHEAD)
        self.assertEqual(SS.classify_drift(self.clone, self.install), [])

    # --- a bare copy missing its SKILL.md entirely reads as behind ----------
    def test_bare_missing_skillmd_is_behind(self):
        _skill(self.clone, "daily-journal", CLONE_JOURNAL_AHEAD)
        (self.install / "daily-journal").mkdir(parents=True)  # dir exists, no SKILL.md
        d = self._by_name(SS.classify_drift(self.clone, self.install))["daily-journal"]
        self.assertEqual(d["status"], "behind")

    # --- message builder: surfaces BOTH directions with opposite actions -----
    # Canonical is the best version; a copy behind it is applied, a copy AHEAD
    # of it is an improvement to upstream so every client gets it (never synced
    # down, which would delete the newer local work).
    def test_message_surfaces_behind_and_leads_distinctly(self):
        _skill(self.clone, "daily-journal", CLONE_JOURNAL_AHEAD)
        _skill(self.install, "daily-journal", BARE_JOURNAL)          # behind
        _skill(self.clone, "insights", BARE_JOURNAL)
        _skill(self.install, "insights", CLONE_JOURNAL_AHEAD)        # LEADS canonical
        msg = SS.drift_message(SS.classify_drift(self.clone, self.install))
        self.assertIsNotNone(msg)
        # behind: named with the missing section + the apply command.
        self.assertIn("daily-journal", msg)
        self.assertIn("Crisis protocol", msg)
        self.assertIn("Behind canonical", msg)
        # leads: named under an "ahead" section, framed as upstream-not-sync-down.
        self.assertIn("insights", msg)
        self.assertIn("Ahead of canonical", msg)
        low = msg.lower()
        self.assertIn("upstream", low)
        self.assertTrue("do not run sync" in low or "don't sync" in low
                        or "would overwrite" in low,
                        "leads message must warn against syncing an ahead copy down")

    def test_message_leads_only_still_fires(self):
        # Even with NOTHING behind, a copy ahead of canonical must be surfaced —
        # a trapped improvement is not a silent-OK state.
        _skill(self.clone, "insights", BARE_JOURNAL)
        _skill(self.install, "insights", CLONE_JOURNAL_AHEAD)
        msg = SS.drift_message(SS.classify_drift(self.clone, self.install))
        self.assertIsNotNone(msg)
        self.assertIn("Ahead of canonical", msg)
        self.assertIn("insights", msg)

    def test_message_none_when_all_synced(self):
        _skill(self.clone, "daily-journal", BARE_JOURNAL)
        _skill(self.install, "daily-journal", BARE_JOURNAL)
        self.assertIsNone(SS.drift_message(SS.classify_drift(self.clone, self.install)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
