#!/usr/bin/env python3
"""Unit suite for the two shipped non-dev-register Stop detectors.

Proves each one matches the SYMPTOM SHAPE rather than the NAME of the concept,
using the two strings measured on the real incident. Both earlier patterns were
inert against those exact strings:

  * the revision-id rule pinned the literal phrase "commit sha", so
    "I saved it as <hex> so we can get back to it." did not match;
  * the blind-decision rule was a five-phrase allow-list, so
    "There are 56 changed files. Should I save these to git, or leave them?"
    -- the incident verbatim, in the plain wording the register itself pushes
    the model toward -- did not match.

Every positive assertion is paired with its negative control, and the two
false-positive shapes that would make a Stop warn worthless are pinned too: the
transcript's own JSONL identifier fields, and each rule's own body text.

Reads the SHIPPED templates, never a copy, so an edit to a pattern is graded
here. Run directly (the ci.sh gate globs scripts/test_*.py):
    python3 scripts/test_nondev_register_detectors.py
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TDIR = REPO / "templates" / "hookify-rules"

REVISION_RULE = TDIR / "hookify.warn-revision-id-narrated-at-user.local.md"
BLIND_RULE = TDIR / "hookify.warn-blind-technical-decision.local.md"

# --- the two strings measured on the real incident ---------------------------
MEASURED_REVISION_ID = "I saved it as a3f4c488 so we can get back to it."
MEASURED_BLIND_DECISION = (
    "There are 56 changed files. Should I save these to git, or leave them?"
)

# --- what the two earlier patterns were, verbatim, for the negative controls --
OLD_REVISION_PIN = r"(?i)commit sha"
OLD_BLIND_ALLOWLIST = (
    r"(?i)(commit and push\?|should i (commit|push|merge)|which merge strategy|"
    r"keep or delete the branch|do you want me to (commit|push|merge))"
)
NAIVE_HEX = r"\b[0-9a-f]{7,40}\b"

# A realistic Claude Code transcript record. Every line of a real transcript
# carries uuid / sessionId / parentUuid values, which is why a bare hex pattern
# fires on every Stop of every session before any jargon is ever narrated.
TRANSCRIPT_RECORD = (
    '{"parentUuid":"a3f4c488-9d25-46d7-932b-2a847cfc4f01",'
    '"sessionId":"22d2bb61-9d25-46d7-932b-2a847cfc4f01","type":"assistant",'
    '"uuid":"e5ca997fdeadbee","version":"2.0.14","gitBranch":"claude/nondev-register",'
    '"message":{"content":[{"type":"text","text":"Saved. Your notes from the call '
    'are in your vault."}]}}'
)

# Plain-language turns a non-developer should be able to have without a fire.
CLEAN_TURNS = (
    "Saved. Your notes from the call are in your vault.",
    "I saved your journal entry for today and filed the two follow-ups.",
    "Should I book the flight now, or wait for a better price?",
    "Do you want me to add that to your to-do list?",
    "Want me to write that down before you forget it?",
    "I saved everything and made a backup first, so nothing is lost.",
    "That is effaced now, and the older draft is defaced beyond repair.",
    "Your notes are stored safely. Anything else you want captured?",
    "I kept a copy of the older version, so we can always go back.",
)

_PATTERN_LINE = re.compile(r"^\s*pattern:\s*'(.*)'\s*$", re.MULTILINE)
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


def rule_parts(path: Path):
    """(compiled_pattern, raw_pattern, body_text) from a shipped template."""
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER.search(text)
    assert m, f"{path} has no frontmatter block"
    fm, body = m.group(1), m.group(2)
    pm = _PATTERN_LINE.search(fm)
    assert pm, f"{path} has no single-quoted `pattern:` line"
    raw = pm.group(1).replace("''", "'")  # YAML single-quote unescaping
    return re.compile(raw), raw, body


class DetectorShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rev, cls.rev_raw, cls.rev_body = rule_parts(REVISION_RULE)
        cls.blind, cls.blind_raw, cls.blind_body = rule_parts(BLIND_RULE)

    # --- positive: the measured incident strings now match -------------------
    def test_revision_id_matches_measured_incident_string(self):
        m = self.rev.search(MEASURED_REVISION_ID)
        self.assertIsNotNone(
            m, f"the revision-id detector must match the measured string: "
               f"{MEASURED_REVISION_ID!r}")
        self.assertIn("a3f4c488", m.group(0))

    def test_blind_decision_matches_measured_incident_string(self):
        m = self.blind.search(MEASURED_BLIND_DECISION)
        self.assertIsNotNone(
            m, f"the blind-decision detector must match the measured string: "
               f"{MEASURED_BLIND_DECISION!r}")

    # --- negative control: the OLD patterns were inert on those same strings --
    def test_old_revision_pin_was_inert(self):
        self.assertIsNone(
            re.search(OLD_REVISION_PIN, MEASURED_REVISION_ID),
            "control broken: the old 'commit sha' pin must NOT match the measured "
            "string, or this suite proves nothing about the fix")

    def test_old_blind_allowlist_was_inert(self):
        self.assertIsNone(
            re.search(OLD_BLIND_ALLOWLIST, MEASURED_BLIND_DECISION),
            "control broken: the old five-phrase allow-list must NOT match the "
            "measured string, or this suite proves nothing about the fix")

    # --- silence on clean plain-language text --------------------------------
    def test_both_detectors_silent_on_clean_turns(self):
        for turn in CLEAN_TURNS:
            with self.subTest(turn=turn):
                self.assertIsNone(self.rev.search(turn))
                self.assertIsNone(self.blind.search(turn))

    # --- silence on the transcript's own identifier fields -------------------
    def test_revision_detector_silent_on_transcript_identifiers(self):
        self.assertIsNone(
            self.rev.search(TRANSCRIPT_RECORD),
            "the revision-id detector must not fire on a transcript's own uuid / "
            "sessionId / parentUuid values, or it fires on every Stop of every session")

    def test_naive_hex_would_have_fired_on_transcript_identifiers(self):
        self.assertIsNotNone(
            re.search(NAIVE_HEX, TRANSCRIPT_RECORD),
            "control broken: a bare hex pattern MUST match the transcript record, or "
            "the previous assertion is proving nothing")

    def test_all_letter_and_all_digit_runs_are_excluded(self):
        for token in ("deadbeef", "facadedface", "1234567", "20260818"):
            with self.subTest(token=token):
                self.assertIsNone(
                    self.rev.search(f"I saved it as {token} so we can get back to it."),
                    "a hex-looking run with no digits, or none with letters, is not a "
                    "revision id")

    # --- self-fire: a rule must not match its own shipped text ---------------
    def test_rules_do_not_match_their_own_bodies(self):
        self.assertIsNone(self.rev.search(self.rev_body))
        self.assertIsNone(self.blind.search(self.blind_body))

    def test_rules_do_not_match_the_canonical_register_block(self):
        """The register block lists the very phrases these rules hunt. Reading it
        puts that text in the transcript, so a pattern that matches it would fire
        on any session where the rule file was consulted."""
        block = (REPO / "templates" / "rules" / "session-close.md").read_text(
            encoding="utf-8")
        start = block.index("<!-- NONDEV-REGISTER:START")
        end = block.index("<!-- NONDEV-REGISTER:END -->")
        canonical = block[start:end]
        self.assertIsNone(self.rev.search(canonical))
        self.assertIsNone(self.blind.search(canonical))

    # --- shipped-state invariants -------------------------------------------
    def test_both_rules_are_stop_event_only(self):
        """Layer 3 adds zero hot-path cost: no PreToolUse / PostToolUse /
        UserPromptSubmit."""
        for path in (REVISION_RULE, BLIND_RULE):
            with self.subTest(rule=path.name):
                fm = _FRONTMATTER.search(path.read_text(encoding="utf-8")).group(1)
                self.assertRegex(fm, re.compile(r"^event:\s*stop\s*$", re.MULTILINE))

    def test_both_rules_classified_exactly_once(self):
        manifest = json.loads((TDIR / "activation.json").read_text(encoding="utf-8"))
        declared = list(manifest["default"]) + list(manifest["opt_in"])
        for path in (REVISION_RULE, BLIND_RULE):
            with self.subTest(rule=path.name):
                self.assertEqual(declared.count(path.name), 1)

    def test_retired_jargon_template_is_gone(self):
        """The vocabulary-list detector was removed, not left on disk to rot."""
        self.assertFalse(
            (TDIR / "hookify.warn-jargon-narrated-at-user.local.md").exists(),
            "the open-ended jargon word-list template must not ship: on a "
            "full-transcript field it cannot distinguish narrated-at-the-user from "
            "appeared-anywhere")


if __name__ == "__main__":
    unittest.main(verbosity=2)
