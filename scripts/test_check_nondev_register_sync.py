#!/usr/bin/env python3
"""Unit suite for scripts/check-nondev-register-sync.py.

Proves the checker (a) is GREEN on the real shipped files today, and (b) is
not vacuously green -- it actually catches every drift shape it exists to
catch. A guard earns trust only by failing on the thing it catches, so every
positive assertion below is paired with the negative control that would have
shipped silently before this existed.

Run directly (the ci.sh gate globs scripts/test_*.py):
    python3 scripts/test_check_nondev_register_sync.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_checker():
    """Load check-nondev-register-sync.py (hyphenated -> importlib) so we test
    the SHIPPED module, not a copy. Mirrors test_skill_copy_drift.py's pattern."""
    path = _HERE / "check-nondev-register-sync.py"
    spec = importlib.util.spec_from_file_location("nondev_register_sync_under_test", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHK = _load_checker()

# A minimal but structurally realistic template section, matching what
# claude-md-template.md actually ships, so edits to it exercise the real
# heading/body-extraction logic rather than a toy fixture.
GOOD_TEMPLATE = """# Memory

## Rules
[From their behavior preferences]

## Plain-Language Rules — NON-NEGOTIABLE

Most people using this vault are not developers. Applies every phase.

1. **Never narrate machinery.** No jargon at them.
2. **Never end a turn on a technical either/or.** Do the safe thing and say so.

## Accountability Rules — NON-NEGOTIABLE

You are not a yes-machine.
"""

GOOD_SESSION_CLOSE = """# Session close protocol

**Most people running this are not developers.** Plain-language register and the no-blind-technical-decision rule are promoted to CLAUDE.md (`## Plain-Language Rules — NON-NEGOTIABLE`) — always loaded, governs every phase, not just this one. The git/resource detail in the final step runs automatically and silently.

## How it runs
"""


class NondevRegisterSyncTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return p

    # --- positive: the real shipped files -----------------------------------
    def test_real_shipped_files_pass(self):
        rc = CHK.check(CHK.DEFAULT_TEMPLATE, CHK.DEFAULT_SESSION_CLOSE)
        self.assertEqual(rc, 0,
                          "the checker must be green against the files this repo actually ships")

    # --- positive: a minimal correct fixture also passes ---------------------
    def test_minimal_good_fixture_passes(self):
        t = self._write("template.md", GOOD_TEMPLATE)
        s = self._write("session-close.md", GOOD_SESSION_CLOSE)
        self.assertEqual(CHK.check(t, s), 0)

    # --- negative: the rule vanished from the template entirely -------------
    def test_rule_deleted_from_template_cannot_check(self):
        no_rule = GOOD_TEMPLATE.replace(
            "## Plain-Language Rules — NON-NEGOTIABLE\n\n"
            "Most people using this vault are not developers. Applies every phase.\n\n"
            "1. **Never narrate machinery.** No jargon at them.\n"
            "2. **Never end a turn on a technical either/or.** Do the safe thing and say so.\n\n",
            "",
        )
        self.assertNotIn("not developers", no_rule.lower())  # fixture sanity
        t = self._write("template.md", no_rule)
        s = self._write("session-close.md", GOOD_SESSION_CLOSE)
        self.assertEqual(CHK.check(t, s), 2,
                          "a template missing the rule anchor entirely must fail loud (2), never pass")

    # --- negative: half the rule silently deleted (machinery-ban half) ------
    def test_machinery_ban_half_deleted_fails(self):
        half = GOOD_TEMPLATE.replace(
            "1. **Never narrate machinery.** No jargon at them.\n", "")
        t = self._write("template.md", half)
        s = self._write("session-close.md", GOOD_SESSION_CLOSE)
        self.assertEqual(CHK.check(t, s), 1)

    # --- negative: half the rule silently deleted (no-blind-decision half) --
    def test_no_blind_decision_half_deleted_fails(self):
        half = GOOD_TEMPLATE.replace(
            "2. **Never end a turn on a technical either/or.** Do the safe thing and say so.\n",
            "")
        t = self._write("template.md", half)
        s = self._write("session-close.md", GOOD_SESSION_CLOSE)
        rc = CHK.check(t, s)
        self.assertEqual(rc, 1)

    # --- negative: the incident's actual failure mode -- two diverging copies
    def test_session_close_reverts_to_full_duplicate_fails(self):
        # Someone pastes the old full paragraph back into session-close.md
        # instead of leaving the pointer. This is the exact drift shape the
        # task exists to prevent: two copies that will diverge.
        duplicated = (
            "# Session close protocol\n\n"
            "**Most people running this are not developers.** They journal, plan, "
            "think, run a business. Speak to them in plain language. Never narrate "
            "machinery — \"git snapshot\", \"Bash task\", \"mutex\", \"worktree\" — "
            "at them.\n\n## How it runs\n"
        )
        t = self._write("template.md", GOOD_TEMPLATE)
        s = self._write("session-close.md", duplicated)
        rc = CHK.check(t, s)
        self.assertEqual(rc, 1,
                          "a full duplicate copy in session-close.md must fail, "
                          "not silently coexist with the promoted rule")

    # --- negative: pointer removed entirely, nothing left behind ------------
    def test_session_close_pointer_removed_fails(self):
        stripped = "# Session close protocol\n\n## How it runs\n"
        t = self._write("template.md", GOOD_TEMPLATE)
        s = self._write("session-close.md", stripped)
        self.assertEqual(CHK.check(t, s), 1)

    # --- negative: pointer survives a template rename but goes STALE --------
    def test_stale_pointer_after_heading_rename_fails(self):
        renamed = GOOD_TEMPLATE.replace(
            "## Plain-Language Rules — NON-NEGOTIABLE",
            "## Communication Register Rules — NON-NEGOTIABLE",
        )
        t = self._write("template.md", renamed)
        s = self._write("session-close.md", GOOD_SESSION_CLOSE)  # still names the OLD heading
        rc = CHK.check(t, s)
        self.assertEqual(rc, 1,
                          "a pointer naming a heading that no longer exists must fail, "
                          "not pass because the anchor sentence alone still matches")

    # --- negative: ambiguous anchor (two sections both look like the rule) --
    def test_duplicate_anchor_in_template_cannot_check(self):
        doubled = GOOD_TEMPLATE + (
            "\n## Some Other Section\n\nAlso mentions not developers by accident.\n"
        )
        t = self._write("template.md", doubled)
        s = self._write("session-close.md", GOOD_SESSION_CLOSE)
        self.assertEqual(CHK.check(t, s), 2)

    # --- fail loud on unusable input ------------------------------------------
    def test_missing_template_file_exits_2(self):
        s = self._write("session-close.md", GOOD_SESSION_CLOSE)
        rc = CHK.check(self.tmp / "does-not-exist.md", s)
        self.assertEqual(rc, 2)

    def test_missing_session_close_file_exits_2(self):
        t = self._write("template.md", GOOD_TEMPLATE)
        rc = CHK.check(t, self.tmp / "does-not-exist.md")
        self.assertEqual(rc, 2)

    # --- fail loud on a non-UTF-8 file, not an unhandled traceback -----------
    # read_text(encoding="utf-8") raises UnicodeDecodeError, a ValueError
    # subclass -- NOT an OSError. A bare `except OSError` would let this
    # propagate as an unhandled exception instead of a clean exit 2.
    def test_non_utf8_template_exits_2_not_traceback(self):
        t = self.tmp / "template.md"
        t.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
        s = self._write("session-close.md", GOOD_SESSION_CLOSE)
        rc = CHK.check(t, s)
        self.assertEqual(rc, 2)

    def test_non_utf8_session_close_exits_2_not_traceback(self):
        t = self._write("template.md", GOOD_TEMPLATE)
        s = self.tmp / "session-close.md"
        s.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
        rc = CHK.check(t, s)
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    unittest.main(verbosity=2)
