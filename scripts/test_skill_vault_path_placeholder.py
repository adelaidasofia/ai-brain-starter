#!/usr/bin/env python3
"""Guards the vault-root placeholder convention in skills/*/SKILL.md.

THE BUG THIS GUARDS (SKILL-PLACEHOLDER-NEVER-RENDERED): a skill file may quote a
vault path as `[VAULT_PATH]/...`, but NOTHING in this repo substitutes that token
inside a skill file. normalize_path_substitutions() in
scripts/install-hooks-user-level.py resolves `[VAULT_PATH]`, and it resolves it
only in the settings.json HOOK TEMPLATE -- never in markdown. So the token
reaches every install verbatim, and both of its failure modes are silent:

  * a command keeps a literal `[VAULT_PATH]` and runs against a path that cannot
    exist;
  * a step phrased "if <path> exists" simply never fires, so a report loses a
    section and nothing is raised.

skills/insights/SKILL.md carried BOTH spellings -- 12 `[VAULT_PATH]` and 8
`<VAULT_PATH]`-style angle-bracket ones -- so a reader who learned to substitute
one still passed the other through. The angle-bracket spelling is the worse of
the two: inside the `VAULT_ROOT="<VAULT_PATH>"` snippets it is also shell
redirection syntax, so a copy-paste is a syntax error rather than a bad path.

Two invariants, both cheap:

  1. ONE spelling. `[VAULT_PATH]` is the token the repo already substitutes
     elsewhere, so it is the one that survives. `<VAULT_PATH>` is banned outright.
  2. A skill that uses the token must SAY it is a placeholder, in the file, so the
     instruction travels with the thing it governs instead of living in a doc
     nobody reads at the moment of substitution.

Deliberately NOT solved by rendering the token at copy time: that would make every
installed copy differ from canonical, and classify_drift() in sync-skills.py would
then report all four skills as locally-modified forever -- the same nag this repo
already fought in test_skill_copy_drift.py. Keeping copies byte-identical to
canonical and resolving at read time is what keeps that surface quiet.

Run directly (the scripts/ci.sh gate globs scripts/test_*.py):
    python3 scripts/test_skill_vault_path_placeholder.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

CANONICAL = "[VAULT_PATH]"
BANNED = re.compile(r"<VAULT_PATH>")

# The note must name the token and the word "placeholder". Kept this loose on
# purpose: the wording is free to improve, the two load-bearing words are not.
NOTE = re.compile(r"`?\[VAULT_PATH\]`?[^\n]{0,120}placeholder", re.IGNORECASE)


def skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


class TestVaultPathPlaceholder(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(SKILLS_DIR.is_dir(), f"no skills/ dir at {SKILLS_DIR}")
        self.files = skill_files()
        self.assertTrue(self.files, "no skills/*/SKILL.md found -- glob broke")

    def test_no_angle_bracket_spelling(self) -> None:
        """<VAULT_PATH> is banned: nothing substitutes it and it collides with
        shell redirection inside the VAULT_ROOT="..." snippets."""
        offenders = []
        for f in self.files:
            hits = BANNED.findall(f.read_text(encoding="utf-8"))
            if hits:
                offenders.append(f"{f.relative_to(REPO_ROOT)} ({len(hits)}x)")
        self.assertEqual(
            [], offenders,
            "use [VAULT_PATH], the spelling the repo already substitutes:\n  "
            + "\n  ".join(offenders),
        )

    def test_users_of_the_token_declare_it_a_placeholder(self) -> None:
        """A skill quoting [VAULT_PATH] must say in-file that it is a placeholder."""
        missing = []
        for f in self.files:
            text = f.read_text(encoding="utf-8")
            if CANONICAL in text and not NOTE.search(text):
                missing.append(str(f.relative_to(REPO_ROOT)))
        self.assertEqual(
            [], missing,
            "these skills quote [VAULT_PATH] without saying it is a placeholder "
            "to resolve before running anything:\n  " + "\n  ".join(missing),
        )

    def test_guard_still_bites(self) -> None:
        """Negative controls -- a guard that cannot fail is worse than none."""
        self.assertTrue(BANNED.search('VAULT_ROOT="<VAULT_PATH>" python3 x.py'))
        self.assertFalse(BANNED.search('VAULT_ROOT="[VAULT_PATH]" python3 x.py'))
        self.assertTrue(NOTE.search("`[VAULT_PATH]` below is a placeholder, not a path."))
        self.assertFalse(NOTE.search("Read `[VAULT_PATH]/Meta/journal-index.json` first."))


if __name__ == "__main__":
    unittest.main(verbosity=2)
