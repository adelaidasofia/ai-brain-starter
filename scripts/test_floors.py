#!/usr/bin/env python3
"""Floor vocabulary is read from the vault's notes, never declared in code.

Builds synthetic vaults in a temp dir — one English (notes shaped like
generate_floor_stubs.py output), one Spanish (notes shaped like a hand-written
vault) — and proves the same module reads both. Auto-discovered by
scripts/ci.sh via the scripts/test_*.py glob.
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
    lines.append("---")
    lines.append("")
    lines.append("body")
    path.write_text("\n".join(lines), encoding="utf-8")


def english_vault(root):
    """Notes shaped like generate_floor_stubs.py output."""
    _write(root / "floors" / "Boredom.md", {
        "type": "floor", "floor_number": 9, "floor_name": "Boredom",
        "floor_level": "Low", "aliases": '[Aburrimiento, "Floor 9", "Piso 9"]',
    })
    _write(root / "floors" / "Peace.md", {
        "type": "floor", "floor_number": 34, "floor_name": "Peace",
        "floor_level": "High", "aliases": "[Paz]",
    })
    # A tier-index note: no floor_number, must be ignored entirely.
    _write(root / "floors" / "Low Floors.md", {"type": "index"})
    return root


def spanish_vault(root):
    """Notes shaped like a hand-written Spanish vault."""
    _write(root / "📝 Notas" / "Floors" / "Aburrimiento.md", {
        "type": "concept", "floor_number": 9, "floor_tier": "bajo",
        "aliases": "[aburrimiento, boredom, bored]",
    })
    _write(root / "📝 Notas" / "Floors" / "Paz.md", {
        "type": "concept", "floor_number": 34, "floor_tier": "alto",
        "aliases": "[paz, peace, peaceful]",
    })
    return root


def test_english_names():
    mod = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        floors = mod.Floors(english_vault(Path(tmp)))
        ok = True
        if not floors:
            print("FAIL: english vault produced no vocabulary"); ok = False
        if floors.num("Boredom") != 9:
            print("FAIL: 'Boredom' -> {}, want 9".format(floors.num("Boredom"))); ok = False
        if floors.num("Peace") != 34:
            print("FAIL: 'Peace' -> {}, want 34".format(floors.num("Peace"))); ok = False
        # Cross-language alias declared by the note itself.
        if floors.num("Aburrimiento") != 9:
            print("FAIL: alias 'Aburrimiento' -> {}, want 9".format(floors.num("Aburrimiento"))); ok = False
        # Positional aliases are not names.
        if floors.num("Floor 9") is not None:
            print("FAIL: 'Floor 9' resolved as a name"); ok = False
        if floors.num("Piso 9") is not None:
            print("FAIL: 'Piso 9' resolved as a name"); ok = False
        # The tier-index note carries no floor_number and must not appear.
        if floors.num("Low Floors") is not None:
            print("FAIL: tier-index note entered the vocabulary"); ok = False
        if ok:
            print("OK: english vault vocabulary")
        return ok


def test_spanish_names():
    mod = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        floors = mod.Floors(spanish_vault(Path(tmp)))
        ok = True
        if floors.num("Aburrimiento") != 9:
            print("FAIL: 'Aburrimiento' -> {}, want 9".format(floors.num("Aburrimiento"))); ok = False
        # Accent-insensitive, and the filename is a name even without floor_name.
        if floors.num("paz") != 34:
            print("FAIL: 'paz' -> {}, want 34".format(floors.num("paz"))); ok = False
        if floors.num("boredom") != 9:
            print("FAIL: english alias 'boredom' -> {}, want 9".format(floors.num("boredom"))); ok = False
        if ok:
            print("OK: spanish vault vocabulary")
        return ok


def test_empty_vault():
    mod = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        floors = mod.Floors(Path(tmp))
        ok = True
        if floors:
            print("FAIL: empty vault is truthy"); ok = False
        if floors.num("Boredom") is not None:
            print("FAIL: empty vault resolved a name"); ok = False
        if ok:
            print("OK: empty vault loads no vocabulary and stays falsy")
        return ok


def main() -> int:
    ok = test_english_names()
    ok = test_spanish_names() and ok
    ok = test_empty_vault() and ok
    return 0 if ok else 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
