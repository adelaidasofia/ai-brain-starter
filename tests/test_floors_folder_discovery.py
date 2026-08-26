#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_floors_folder_discovery.py — stdlib-only regression tests for where
scripts/_floors.py looks for a vault's floor notes.

Run: python3 tests/test_floors_folder_discovery.py
No pytest dependency. Exits non-zero on any failure. Touches no real vault.

Why this file exists
--------------------
FLOOR_NOTE_DIRS enumerated four layouts: floors/, Notes/Floors, Notas/Floors and
"📝 Notas/Floors". It carried the emoji + Spanish spelling but not the emoji +
English one.

"📝 Notes/Floors" is not a hypothetical spelling. It is what
phases/phase-02-03-plugins-folders.md creates and what phase-10a-journaling.md
writes all 34 floor notes into on a default English install. Those vaults read
as having NO floor vocabulary, so Floors() was empty, and
build-journal-index.py took the "no floor notes found — frontmatter consistency
check skipped" branch on every run. The index still built; the check behind it
silently never ran, and a wrong floor_number in an entry went unreported
indefinitely.

An exact-string match is the wrong tool for user-facing folders that routinely
carry an emoji prefix. Discovery now normalises the name (emoji, punctuation,
accents, case) instead, so the module lives up to the contract in its own
docstring: "Any vault works, in any language."
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("floors_mod", ROOT / "scripts" / "_floors.py")
floors_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(floors_mod)

FAILS: list = []

NOTE = """---
type: concept
floor_number: 24
floor_level: Middle
floor_tier: middle
aliases: [reason, razón]
---
# Reason
"""


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append("%s%s" % (name, ": " + detail if detail else ""))
        print("  FAIL %s%s" % (name, ": " + detail if detail else ""))


def vault_with(layout):
    """Build a temp vault whose floor note lives at `layout`, return its root."""
    root = Path(tempfile.mkdtemp())
    folder = root.joinpath(*layout)
    folder.mkdir(parents=True)
    (folder / "Reason.md").write_text(NOTE, encoding="utf-8")
    return root


def test_layouts_that_must_be_found():
    """Every layout the product creates, plus the ones already supported."""
    print("test_layouts_that_must_be_found")
    layouts = [
        (("📝 Notes", "Floors"), "emoji + English — what phase-02-03 creates"),
        (("📝 Notas", "Floors"), "emoji + Spanish"),
        (("Notes", "Floors"), "plain English"),
        (("Notas", "Floors"), "plain Spanish"),
        (("floors",), "root-level floors/"),
        (("📝 Notes", "Pisos"), "emoji + English notes, Spanish floors"),
        (("🗂️ Notas", "Pisos"), "different emoji, all Spanish"),
    ]
    for layout, label in layouts:
        root = vault_with(layout)
        try:
            f = floors_mod.Floors(root)
            check(
                "%s (%s)" % ("/".join(layout), label),
                bool(f) and f.num("Reason") == 24,
                "vocabulary came back empty — build-journal-index skips its check",
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


def test_absent_vocabulary_stays_absent():
    """The negative control: no floor notes must NOT be silently invented."""
    print("test_absent_vocabulary_stays_absent")
    root = Path(tempfile.mkdtemp())
    try:
        (root / "📝 Notes").mkdir()
        (root / "📝 Notes" / "Some Note.md").write_text("# no frontmatter\n", encoding="utf-8")
        f = floors_mod.Floors(root)
        check("a vault with no floor notes reports no vocabulary", not bool(f))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_each_note_absorbed_once():
    """Named list and discovery overlap; a note must not be counted twice."""
    print("test_each_note_absorbed_once")
    root = vault_with(("📝 Notes", "Floors"))
    try:
        f = floors_mod.Floors(root)
        check("floor resolves to a single number", f.num("Reason") == 24,
              "got %r" % (f.num("Reason"),))
        check("tier survives the merge", f.tier(24) == "middle",
              "got %r" % (f.tier(24),))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_folder_key_normalises():
    """The helper the discovery rests on."""
    print("test_folder_key_normalises")
    cases = [
        ("📝 Notes", "notes"),
        ("📝 Notas", "notas"),
        ("Notes", "notes"),
        ("  Pisos  ", "pisos"),
        ("🗂️ Floors", "floors"),
    ]
    for raw, want in cases:
        got = floors_mod.folder_key(raw)
        check("folder_key(%r) == %r" % (raw, want), got == want, "got %r" % (got,))


def main():
    test_layouts_that_must_be_found()
    test_absent_vocabulary_stays_absent()
    test_each_note_absorbed_once()
    test_folder_key_normalises()
    if FAILS:
        print("\n%d failure(s):" % len(FAILS))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
