#!/usr/bin/env python3
"""Floors written as wikilinks must resolve exactly like plain names.

A floor recorded as `floor: Aceptación` draws NO edge in the Obsidian graph —
frontmatter has to carry `floor: "[[Acceptance|Aceptación]]"` for the link to
exist. On a real vault that change made every entry fail the consistency check:
`parse_inline_list` saw a value that opened and closed with a bracket, split it
as a YAML flow list, and handed back `[Acceptance|Aceptación]` — one bracket
short and matching nothing in the scale.

This locks in both halves: a wikilink is ONE value, and name comparison sees
through the link syntax. Auto-discovered by scripts/ci.sh via test_*.py.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "scripts" / "_floors.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_floors", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path, frontmatter):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append("{}: {}".format(key, value))
    lines += ["---", "", "body"]
    path.write_text("\n".join(lines), encoding="utf-8")


def _fail(label, got, want):
    print("FAIL: {} — got {!r}, want {!r}".format(label, got, want))
    return False


def test_strip_wikilink():
    m = _load_module()
    cases = [
        ('[[Acceptance|Aceptación]]', "Aceptación"),
        ("[[Joy]]", "Joy"),
        ('"[[Willingness|Voluntad]]"', "Voluntad"),
        ("Razón", "Razón"),          # plain names pass through untouched
        ("middle", "middle"),
        ("[a, b]", "[a, b]"),        # a real flow list is not a link
    ]
    ok = True
    for raw, want in cases:
        got = m.strip_wikilink(raw)
        if got != want:
            ok = _fail("strip_wikilink({!r})".format(raw), got, want) and False
    if None is not m.strip_wikilink(None):
        ok = _fail("strip_wikilink(None)", m.strip_wikilink(None), None) and False
    if ok:
        print("OK: wikilink syntax stripped; plain values and real lists untouched")
    return ok


def test_wikilink_is_one_value():
    """The regression itself: a link must not be split as a flow list."""
    m = _load_module()
    ok = True
    got = m.parse_inline_list('[[Willingness|Voluntad]]')
    if got != ["[[Willingness|Voluntad]]"]:
        ok = _fail("parse_inline_list(wikilink)", got, ["[[Willingness|Voluntad]]"])
    got = m.landed_floor({"floor": '"[[Acceptance|Aceptación]]"'})
    if m.normalise_name(got) != "aceptacion":
        ok = _fail("landed_floor -> normalise_name", m.normalise_name(got), "aceptacion")
    got = m.parse_inline_list("[Fear, Hope]")
    if got != ["Fear", "Hope"]:
        ok = _fail("parse_inline_list(real list)", got, ["Fear", "Hope"])
    if ok:
        print("OK: a wikilink is one value; genuine flow lists still split")
    return ok


def _vault(root):
    """Emoji-decorated English folder — the layout the exact-path list missed."""
    for name, number, tier in (
        ("Acceptance", 23, "middle"),
        ("Willingness", 22, "middle"),
        ("Compassion", 26, "high"),
    ):
        _write(root / "📝 Notes" / "Floors" / "{}.md".format(name),
               {"floor_number": number, "floor_tier": tier,
                "aliases": "[{}, {}]".format(name.lower(), "aceptación"
                                             if name == "Acceptance" else "voluntad"
                                             if name == "Willingness" else "compasión")})


def test_decorated_folder_is_found():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _vault(root)
        floors = _load_module().Floors(root)
        if not floors:
            print("FAIL: floor notes under '📝 Notes/Floors' were not found")
            return False
        if floors.num('[[Acceptance|Aceptación]]') != 23:
            return _fail("num(wikilink)", floors.num('[[Acceptance|Aceptación]]'), 23)
        print("OK: emoji-decorated parent folder discovered; wikilink resolves to its number")
        return True


def test_check_accepts_wikilinks():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _vault(root)
        floors = _load_module().Floors(root)
        clean = {
            "floor": '"[[Acceptance|Aceptación]]"',
            "floor_level": "middle",
            "floor_arc": '["[[Compassion|Compasión]]", "[[Acceptance|Aceptación]]"]',
        }
        issues = floors.check(clean, label="clean.md")
        if issues:
            print("FAIL: a consistent wikilink entry was flagged: {}".format(issues))
            return False
        wrong_tier = dict(clean, floor_level="high")
        if not floors.check(wrong_tier, label="wrong.md"):
            print("FAIL: a wrong tier went unreported behind link syntax")
            return False
        print("OK: consistent wikilink entries pass; real contradictions still caught")
        return True


def main() -> int:
    ok = test_strip_wikilink()
    ok = test_wikilink_is_one_value() and ok
    ok = test_decorated_folder_is_found() and ok
    ok = test_check_accepts_wikilinks() and ok
    return 0 if ok else 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
