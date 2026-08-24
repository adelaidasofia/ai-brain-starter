#!/usr/bin/env python3
"""Unit suite for scripts/check-nondev-register-sync.py.

The checker's whole job is to prove the non-dev register REACHES an installed
vault. Its predecessor compared two repo files to each other and stayed green
while the rule reached zero users, so the assertions that matter here are the
REACH ones: the generated CLAUDE.md template is on no drift-check propagation
scope, a home moved off a scope must fail, and a scope deleted from
drift-check.sh must fail too.

Every positive assertion is paired with the negative control that would have
shipped silently before this existed. Run directly (the ci.sh gate globs
scripts/test_*.py):
    python3 scripts/test_check_nondev_register_sync.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent


def _load_checker():
    """Load check-nondev-register-sync.py (hyphenated -> importlib) so the tests
    grade the SHIPPED module, not a copy."""
    path = _HERE / "check-nondev-register-sync.py"
    spec = importlib.util.spec_from_file_location("nondev_register_sync_under_test", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHK = _load_checker()

REAL_DRIFT_TEXT = (_REPO / CHK.DRIFT_CHECK).read_text(encoding="utf-8")

GOOD_BLOCK = (
    f"{CHK.MARK_START} canonical -->\n"
    "## Plain-language register — NON-NEGOTIABLE\n"
    "\n"
    "Governs every phase, every skill, every session.\n"
    "\n"
    "1. **Never narrate machinery.** No raw revision id.\n"
    "2. **Never end a turn on a technical either/or.** Do the safe thing.\n"
    "3. **Never make them paste a credential.** Set it for them.\n"
    f"{CHK.MARK_END}"
)

# drift-check with Scopes A and B intact but the templates/rules source gone --
# the shape of "someone deleted Scope C and D".
DRIFT_WITHOUT_RULES_SCOPE = """#!/usr/bin/env bash
STARTER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
if [[ -d "$STARTER_DIR/skills" ]]; then
  for skill_dir in "$STARTER_DIR/skills"/*/; do :; done
fi
src="$STARTER_DIR/scripts/$name"
extra="$STARTER_DIR/docs/CHANGELOG.md"
"""


def _run(repo_root: Path):
    """(exit_code, combined_output) from one checker run."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = CHK.check(repo_root)
    return rc, out.getvalue() + err.getvalue()


class ScopeParserTests(unittest.TestCase):
    def test_parses_the_real_drift_check(self):
        scopes = CHK.parse_propagation_scopes(REAL_DRIFT_TEXT)
        self.assertGreaterEqual(len(scopes), CHK.MIN_EXPECTED_SCOPES, scopes)
        self.assertTrue(
            any(s.startswith("templates/rules") for s in scopes),
            f"drift-check propagates templates/rules; parser saw {scopes}")

    def test_rules_templates_are_covered(self):
        scopes = CHK.parse_propagation_scopes(REAL_DRIFT_TEXT)
        self.assertIsNotNone(
            CHK.scope_covering("templates/rules/session-close.md", scopes))
        self.assertIsNotNone(
            CHK.scope_covering("templates/rules/session-start-checks.md", scopes))

    def test_generated_claude_md_template_is_covered_by_nothing(self):
        """The measured finding that started this: the generated template is on
        NO propagation scope, so it reaches no existing install."""
        scopes = CHK.parse_propagation_scopes(REAL_DRIFT_TEXT)
        self.assertIsNone(
            CHK.scope_covering("templates/generated/claude-md-template.md", scopes),
            "if this ever passes, drift-check gained a scope for the generated "
            "template and the NEW_INSTALL_COPIES comment needs re-measuring")

    def test_parser_finds_nothing_in_an_empty_file(self):
        self.assertEqual(CHK.parse_propagation_scopes(""), [])


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _repo(self, blocks=None, drift_text=REAL_DRIFT_TEXT, extra_files=()):
        """A fixture repo carrying every declared copy of the block."""
        blocks = blocks or {}
        root = self.tmp / "repo"
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        (root / CHK.DRIFT_CHECK).write_text(drift_text, encoding="utf-8")
        for rel in tuple(CHK.HOMES) + tuple(CHK.NEW_INSTALL_COPIES) + tuple(extra_files):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            body = blocks.get(rel, GOOD_BLOCK)
            p.write_text(f"# heading\n\nintro\n\n{body}\n\ntail\n", encoding="utf-8")
        return root

    # --- positive: the real shipped repo -------------------------------------
    def test_real_repo_passes(self):
        rc, out = _run(_REPO)
        self.assertEqual(rc, 0, out)
        self.assertIn("drift-check scope source", out)

    def test_minimal_good_fixture_passes(self):
        rc, out = _run(self._repo())
        self.assertEqual(rc, 0, out)

    # --- REACH negatives -----------------------------------------------------
    def test_home_moved_off_every_scope_fails(self):
        """The exit criterion: the verifier fails if the rule leaves a
        propagation scope."""
        off_scope = "templates/generated/claude-md-template.md"
        with mock.patch.object(CHK, "HOMES", (off_scope,)), \
             mock.patch.object(CHK, "NEW_INSTALL_COPIES", ()):
            rc, out = _run(self._repo())
        self.assertEqual(rc, 1, out)
        self.assertIn("REACH", out)

    def test_scope_deleted_from_drift_check_fails(self):
        """Parsing drift-check rather than hardcoding a filename means deleting
        the templates/rules scope is caught here, not discovered by a user."""
        rc, out = _run(self._repo(drift_text=DRIFT_WITHOUT_RULES_SCOPE))
        self.assertEqual(rc, 1, out)
        self.assertIn("REACH", out)

    # --- AGREE negative ------------------------------------------------------
    def test_copies_that_disagree_fail(self):
        drifted = GOOD_BLOCK.replace("Do the safe thing.", "Ask the user to choose.")
        self.assertNotEqual(drifted, GOOD_BLOCK)  # fixture sanity
        rc, out = _run(self._repo(
            blocks={CHK.NEW_INSTALL_COPIES[0]: drifted}))
        self.assertEqual(rc, 1, out)
        self.assertIn("AGREE", out)

    def test_second_home_that_disagrees_fails(self):
        drifted = GOOD_BLOCK.replace("every phase", "the close phase")
        rc, out = _run(self._repo(blocks={CHK.HOMES[1]: drifted}))
        self.assertEqual(rc, 1, out)
        self.assertIn("AGREE", out)

    # --- INTACT negatives ----------------------------------------------------
    def test_each_pin_deleted_from_every_copy_fails(self):
        removals = {
            "narrate machinery": "1. **Never narrate machinery.** No raw revision id.\n",
            "technical either/or":
                "2. **Never end a turn on a technical either/or.** Do the safe thing.\n",
            "paste a credential":
                "3. **Never make them paste a credential.** Set it for them.\n",
            "every phase": "Governs every phase, every skill, every session.\n",
        }
        for pin, line in removals.items():
            with self.subTest(pin=pin):
                gutted = GOOD_BLOCK.replace(line, "")
                self.assertNotIn(pin, gutted.lower())  # fixture sanity
                blocks = {rel: gutted
                          for rel in tuple(CHK.HOMES) + tuple(CHK.NEW_INSTALL_COPIES)}
                rc, out = _run(self._repo(blocks=blocks))
                self.assertEqual(rc, 1, out)
                self.assertIn("INTACT", out)

    # --- CANNOT CHECK (fail loud, never a silent pass) -----------------------
    def test_missing_drift_check_cannot_check(self):
        root = self._repo()
        (root / CHK.DRIFT_CHECK).unlink()
        rc, out = _run(root)
        self.assertEqual(rc, 2, out)
        self.assertIn("CANNOT CHECK", out)

    def test_unparseable_drift_check_cannot_check(self):
        rc, out = _run(self._repo(drift_text="#!/usr/bin/env bash\nexit 0\n"))
        self.assertEqual(rc, 2, out)
        self.assertIn("propagation source", out)

    def test_block_missing_from_a_declared_copy_cannot_check(self):
        rc, out = _run(self._repo(blocks={CHK.HOMES[0]: "no markers here"}))
        self.assertEqual(rc, 2, out)
        self.assertIn("CANNOT CHECK", out)

    def test_non_utf8_input_cannot_check(self):
        root = self._repo()
        (root / CHK.HOMES[0]).write_bytes(b"\xff\xfe not utf-8 \x00")
        rc, out = _run(root)
        self.assertEqual(rc, 2, out)
        self.assertIn("CANNOT CHECK", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
