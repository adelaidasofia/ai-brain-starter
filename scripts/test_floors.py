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


def test_tiers():
    mod = _load_module()
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        # English notes declare the tier as `floor_level: Low`.
        floors = mod.Floors(english_vault(Path(tmp)))
        if not floors.has_tiers:
            print("FAIL: english vault loaded no tiers"); ok = False
        if floors.tier(9) != "low":
            print("FAIL: english tier(9) -> {}, want 'low'".format(floors.tier(9))); ok = False
        if floors.tier(34) != "high":
            print("FAIL: english tier(34) -> {}, want 'high'".format(floors.tier(34))); ok = False
    with tempfile.TemporaryDirectory() as tmp:
        # Spanish notes declare it as `floor_tier: bajo`. Same canonical answer.
        floors = mod.Floors(spanish_vault(Path(tmp)))
        if floors.tier(9) != "low":
            print("FAIL: spanish tier(9) -> {}, want 'low'".format(floors.tier(9))); ok = False
        if floors.tier(34) != "high":
            print("FAIL: spanish tier(34) -> {}, want 'high'".format(floors.tier(34))); ok = False
    with tempfile.TemporaryDirectory() as tmp:
        # Names without tiers: vocabulary loads, tiers do not. Independent.
        root = Path(tmp)
        _write(root / "floors" / "Boredom.md",
               {"type": "floor", "floor_number": 9, "floor_name": "Boredom"})
        floors = mod.Floors(root)
        if not floors:
            print("FAIL: names should load without tiers"); ok = False
        if floors.has_tiers:
            print("FAIL: has_tiers true with no tier fields"); ok = False
        if floors.tier(9) is not None:
            print("FAIL: tier(9) -> {}, want None".format(floors.tier(9))); ok = False
    if ok:
        print("OK: tiers read from either field name, normalised across languages")
    return ok


def test_check():
    mod = _load_module()
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        floors = mod.Floors(english_vault(Path(tmp)))

        # Clean entry: floor 9 is Low, arc ends where floor says, number agrees.
        clean = {"floor": "Boredom", "floor_level": "low", "floor_num": "9",
                 "floor_arc": "[Peace, Boredom]"}
        if floors.check(clean):
            print("FAIL: clean entry reported {}".format(floors.check(clean))); ok = False

        # floor_level contradicts the vault's tier for floor 9.
        if len(floors.check({"floor": "Boredom", "floor_level": "high"})) != 1:
            print("FAIL: tier contradiction not caught"); ok = False

        # floor_arc must end where `floor` says the day landed.
        if len(floors.check({"floor": "Boredom", "floor_arc": "[Boredom, Peace]"})) != 1:
            print("FAIL: floor_arc mismatch not caught"); ok = False

        # A declared floor_num that disagrees with the vocabulary.
        if len(floors.check({"floor": "Boredom", "floor_num": "13"})) != 1:
            print("FAIL: floor_num mismatch not caught"); ok = False

        # A floor the vault has never heard of.
        if len(floors.check({"floor": "Schadenfreude"})) != 1:
            print("FAIL: off-scale floor not caught"); ok = False

        # Legacy list form: [primary, secondary] — the FIRST element landed.
        if floors.check({"floor": "[Boredom, Peace]", "floor_level": "low"}):
            print("FAIL: legacy list form misread"); ok = False

        # Messages carry the label when given.
        msgs = floors.check({"floor": "Schadenfreude"}, label="2026-01-02.md")
        if not msgs or "2026-01-02.md" not in msgs[0]:
            print("FAIL: label missing from message: {}".format(msgs)); ok = False

    with tempfile.TemporaryDirectory() as tmp:
        # No vocabulary: check everything, report nothing. No guessing.
        floors = mod.Floors(Path(tmp))
        if floors.check({"floor": "Schadenfreude", "floor_level": "high", "floor_num": "99",
                         "floor_arc": "[Something, Different]"}):
            print("FAIL: empty vault reported issues"); ok = False

    with tempfile.TemporaryDirectory() as tmp:
        # Names loaded but no tiers: vocabulary present, tier guard is skipped.
        root = Path(tmp)
        _write(root / "floors" / "Boredom.md",
               {"type": "floor", "floor_number": 9, "floor_name": "Boredom"})
        floors = mod.Floors(root)
        # Entry with a known floor and a floor_level value: no tier complaint because has_tiers is false.
        if floors.check({"floor": "Boredom", "floor_level": "low"}):
            print("FAIL: names-without-tiers vault complained about floor_level"); ok = False
        # Entry with an unknown floor: off-scale check still fires because vocabulary is loaded.
        if len(floors.check({"floor": "Schadenfreude"})) != 1:
            print("FAIL: off-scale check did not fire with names-only vocabulary"); ok = False

    if ok:
        print("OK: per-entry checks catch contradictions and stay silent without vocabulary")
    return ok


def test_index_integration():
    """The index builder runs the check and never skips it silently."""
    import subprocess

    ok = True
    builder = ROOT / "scripts" / "build-journal-index.py"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        english_vault(root)
        (root / "Meta").mkdir(parents=True, exist_ok=True)
        # One clean entry, one that contradicts itself.
        _write(root / "Journals" / "2026-01-01.md",
               {"creationDate": "2026-01-01", "floor": "Boredom", "floor_level": "low"})
        _write(root / "Journals" / "2026-01-02.md",
               {"creationDate": "2026-01-02", "floor": "Boredom", "floor_level": "high"})
        proc = subprocess.run(
            [sys.executable, str(builder), "--vault-root", str(root),
             "--journal-dir", "Journals", "--meta-dir", "Meta"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if "2026-01-02.md" not in proc.stderr:
            print("FAIL: contradiction not reported. Output:\n{}".format(proc.stderr)); ok = False
        if "2026-01-01.md" in proc.stderr:
            print("FAIL: clean entry reported as a problem"); ok = False
        if "2026-01-02.md" in proc.stdout:
            print("FAIL: contradiction leaked to stdout"); ok = False

    with tempfile.TemporaryDirectory() as tmp:
        # No floor notes at all: the skip must be announced, not silent.
        root = Path(tmp)
        (root / "Meta").mkdir(parents=True, exist_ok=True)
        _write(root / "Journals" / "2026-01-01.md",
               {"creationDate": "2026-01-01", "floor": "Boredom", "floor_level": "low"})
        proc = subprocess.run(
            [sys.executable, str(builder), "--vault-root", str(root),
             "--journal-dir", "Journals", "--meta-dir", "Meta"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if "no floor notes found" not in proc.stderr:
            print("FAIL: no-floor-notes skip was not announced. Output:\n{}".format(proc.stderr)); ok = False
        if "no floor notes found" in proc.stdout:
            print("FAIL: no-floor-notes notice leaked to stdout"); ok = False

    with tempfile.TemporaryDirectory() as tmp:
        # Names load but no tiers: the TIER skip must fire (the `elif`), not
        # the no-floor-notes skip above (the `if`) — the two notices must be
        # told apart, not just "something got announced".
        root = Path(tmp)
        _write(root / "floors" / "Boredom.md",
               {"type": "floor", "floor_number": 9, "floor_name": "Boredom"})
        (root / "Meta").mkdir(parents=True, exist_ok=True)
        _write(root / "Journals" / "2026-01-01.md",
               {"creationDate": "2026-01-01", "floor": "Boredom", "floor_level": "low"})
        proc = subprocess.run(
            [sys.executable, str(builder), "--vault-root", str(root),
             "--journal-dir", "Journals", "--meta-dir", "Meta"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if "floor notes declare no tiers" not in proc.stderr:
            print("FAIL: tiers-only skip was not announced. Output:\n{}".format(proc.stderr)); ok = False
        if "no floor notes found" in proc.stderr:
            print("FAIL: no-floor-notes notice fired instead of the tiers-only notice"); ok = False
        if "floor notes declare no tiers" in proc.stdout:
            print("FAIL: tiers-only notice leaked to stdout"); ok = False

    if ok:
        print("OK: index builder reports contradictions and announces skips")
    return ok


def main() -> int:
    ok = test_english_names()
    ok = test_spanish_names() and ok
    ok = test_empty_vault() and ok
    ok = test_tiers() and ok
    ok = test_check() and ok
    ok = test_index_integration() and ok
    return 0 if ok else 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
