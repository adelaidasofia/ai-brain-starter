#!/usr/bin/env python3
"""Regression lock for #397 — settings.json must be code-page-proof on disk.

THE DEFECT. write_settings_with_verify() serialized with ensure_ascii=False, so
every non-ASCII character in a hook path landed on disk as its raw UTF-8 bytes.
The file itself was valid UTF-8; the problem is who reads it. Claude Code hands
hook commands to a Windows shell running a legacy code page, and a settings.json
carrying raw UTF-8 gets decoded there as cp1252/cp437: an account directory named
"Ñoño" is read back as "Ã‘oÃ±o", the absolute hook path stops resolving, and
EVERY hook fails — which blocks Bash and Grep for the whole session. The reporter
saw 47, then 132, then 104 dead paths across three reinstalls.

WHY THE 8.3 SHORT-PATH FIX (#430) IS NOT ENOUGH. _ascii_safe_win_path() rewrites
Windows hook paths to their 8.3 form, which is ASCII by construction. But 8.3
name creation is DISABLED by default on many modern volumes, and on those the
helper degrades by returning the path unchanged (it only warns). It also covers
the Windows hook-command rewrite alone — the vault path substituted into POSIX
commands, the user's own pre-existing settings entries, and every non-Windows
consumer are all still written raw. The byte-level fix belongs at the one place
that turns settings into bytes.

THE INVARIANT. settings.json is written with JSON's default ensure_ascii=True, so
the file is pure ASCII: non-ASCII is escaped to \\uXXXX before it ever reaches
the disk. Pure-ASCII bytes decode identically under UTF-8, cp1252, cp437 and
latin-1, so no consumer's code page can corrupt a path. JSON semantics are
unchanged — "\\u00f3" and "ó" parse to the same string — so nothing downstream
sees a different value. The same escaping is already relied on elsewhere in this
repo as cp1252 protection (see the SEV-4-json-encoded class in
scripts/utf8-stdout-baseline.txt).

These asserts fail against ensure_ascii=False and pass against the fix.

Auto-discovered by scripts/ci.sh via the scripts/test_*.py glob.
Stdlib only. No network. Never writes outside a tempdir.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install-hooks-user-level.py"

# A neutral non-ASCII profile directory of the reported shape (accented Latin
# letters in the Windows account name). Never a real reporter's name.
WIN_HOME = "C:\\Users\\Usuario Prueba \u00d1o\u00f1o"
GEAR = "\u2699\ufe0f"  # the "⚙️" of "⚙️ Meta" — in every vault path we substitute
# The "💼" of "💼 Work", also a real vault folder, and NON-BMP: ensure_ascii
# escapes it to a SURROGATE PAIR (\ud83d\udcbc), a different code path from the
# single \uXXXX a BMP character gets. A round trip that only ever exercised BMP
# characters would not prove the pair is reassembled on read.
BRIEFCASE = "\U0001f4bc"

# The legacy code pages a Windows consumer of settings.json actually uses.
# latin-1 is included because it decodes ANY byte: it never raises, it just
# silently returns the wrong characters, which is the exact failure shape here.
LEGACY_CODEPAGES = ("cp1252", "cp437", "latin-1")

PASS = 0
FAIL = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {msg}")


def bad(msg: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {msg}" + (f" :: {detail}" if detail else ""))


def load_installer():
    spec = importlib.util.spec_from_file_location("_abs_installer_397", INSTALLER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _settings_fixture() -> dict:
    """The production shape: a Windows hook_runner command whose absolute paths
    sit under a non-ASCII profile dir, plus a POSIX command carrying a vault path
    with the gear emoji, plus an unowned user entry we must not disturb."""
    runner = f"{WIN_HOME}\\.claude\\skills\\ai-brain-starter\\scripts\\hook_runner.py"
    target = f"{WIN_HOME}\\.claude\\hooks\\session-start-context.py"
    vault_hook = f"/home/u/vault/{GEAR} Meta/{BRIEFCASE} Work/scripts/graph-context-hook.sh"
    return {
        "hooks": {
            "PreToolUse": [{
                "matcher": "Bash",
                "hooks": [{
                    "type": "command",
                    "command": f'py -3 "{runner}" --fallback silent "{target}"',
                }],
            }],
            "SessionStart": [{
                "hooks": [
                    {"type": "command", "command": f"bash '{vault_hook}'"},
                    {"type": "command", "command": "echo mine"},  # unowned user entry
                ],
            }],
        },
    }


def _write(ins, path: Path, settings: dict) -> bool:
    return bool(ins.write_settings_with_verify(path, settings, None))


# --------------------------------------------------------------------------
# 1. the file's BYTES must survive any code page
# --------------------------------------------------------------------------
def test_settings_bytes_are_pure_ascii(ins) -> None:
    settings = _settings_fixture()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        if not _write(ins, path, settings):
            bad("write_settings_with_verify succeeds on a non-ASCII settings dict")
            return
        raw = path.read_bytes()
        if raw.isascii():
            ok("settings.json is written as pure-ASCII bytes (no code page can corrupt it)")
        else:
            offenders = sorted({b for b in raw if b > 0x7F})
            bad("settings.json is written as pure-ASCII bytes",
                f"{len(offenders)} non-ASCII byte value(s), e.g. {offenders[:4]}")


# --------------------------------------------------------------------------
# 2. the ROUND TRIP a Windows consumer actually performs
# --------------------------------------------------------------------------
def _commands(doc: dict) -> list:
    out = []
    for groups in (doc.get("hooks") or {}).values():
        for g in groups:
            for h in g.get("hooks", []):
                out.append(h.get("command", ""))
    return out


def test_paths_round_trip_through_legacy_codepages(ins) -> None:
    """Written by us as UTF-8, read back by a consumer using the console code
    page. Every hook command must come back byte-identical; the reported bug is
    exactly this comparison failing with "Ñoño" -> "Ã‘oÃ±o"."""
    settings = _settings_fixture()
    expected = _commands(settings)
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        if not _write(ins, path, settings):
            bad("write_settings_with_verify succeeds on a non-ASCII settings dict")
            return
        raw = path.read_bytes()
        for codec in LEGACY_CODEPAGES:
            try:
                doc = json.loads(raw.decode(codec))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                bad(f"settings.json parses when decoded as {codec}",
                    f"{type(e).__name__}: {e}")
                continue
            got = _commands(doc)
            if got == expected:
                ok(f"every hook command round-trips identically through {codec}")
            else:
                diff = next((f"{a!r} != {b!r}" for a, b in zip(expected, got) if a != b), "")
                bad(f"every hook command round-trips identically through {codec}", diff)


def test_locale_default_reader_sees_the_same_paths(ins) -> None:
    """The same round trip through open()'s TEXT layer rather than raw bytes —
    a consumer that simply omits encoding= and inherits the locale, which is
    cp1252 on the affected machines."""
    settings = _settings_fixture()
    expected = _commands(settings)
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        if not _write(ins, path, settings):
            bad("write_settings_with_verify succeeds on a non-ASCII settings dict")
            return
        for codec in LEGACY_CODEPAGES:
            try:
                with path.open("r", encoding=codec) as f:
                    doc = json.load(f)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                bad(f"a reader whose default encoding is {codec} parses settings.json",
                    f"{type(e).__name__}: {e}")
                continue
            if _commands(doc) == expected:
                ok(f"a reader whose default encoding is {codec} sees the real paths")
            else:
                bad(f"a reader whose default encoding is {codec} sees the real paths")


# --------------------------------------------------------------------------
# 3. escaping must not change what anything reads back (no-regression)
# --------------------------------------------------------------------------
def test_utf8_reader_semantics_unchanged(ins) -> None:
    """ASCII-escaping is a byte-level change only. The installer's own reader
    (read_text(encoding='utf-8') -> json.loads) must get the identical dict, or
    the fix would break merge/dedup/uninstall, which all match on these strings."""
    settings = _settings_fixture()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        if not _write(ins, path, settings):
            bad("write_settings_with_verify succeeds on a non-ASCII settings dict")
            return
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc == settings:
            ok("a UTF-8 reader gets the identical settings dict (semantics unchanged)")
        else:
            bad("a UTF-8 reader gets the identical settings dict", repr(doc)[:200])
        cmds = _commands(doc)
        if any(WIN_HOME in c for c in cmds) and any(GEAR in c for c in cmds):
            ok("the non-ASCII home path and the gear vault path both decode back intact")
        else:
            bad("the non-ASCII home path and the gear vault path both decode back intact")
        # Non-BMP: escaped as a surrogate PAIR, so this proves the pair is
        # reassembled into one character rather than left as two lone halves.
        if any(BRIEFCASE in c for c in cmds):
            ok("a non-BMP vault folder survives the surrogate-pair escape round trip")
        else:
            bad("a non-BMP vault folder survives the surrogate-pair escape round trip",
                next((c for c in cmds if "Work" in c), ""))


def test_write_is_idempotent_across_a_reinstall(ins) -> None:
    """The reporter's install regressed on EVERY reinstall, so the second write
    matters as much as the first: read what we wrote, write it again, and the
    bytes must be identical. A file that drifts on re-serialization is how a
    mojibake path gets baked in permanently."""
    settings = _settings_fixture()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        if not _write(ins, path, settings):
            bad("write_settings_with_verify succeeds on a non-ASCII settings dict")
            return
        first = path.read_bytes()
        reread = json.loads(path.read_text(encoding="utf-8"))
        if not _write(ins, path, reread):
            bad("a reinstall re-writes settings.json successfully")
            return
        if path.read_bytes() == first:
            ok("a reinstall writes byte-identical settings.json (no drift on re-serialization)")
        else:
            bad("a reinstall writes byte-identical settings.json")


def _run(label, fn, *a) -> None:
    """Run one check; an exception is a FAILURE, not an abort. Each check locks
    an independent property, so letting the first raise kill the process would
    hide every later result — the false-green shape these tests prevent."""
    try:
        fn(*a)
    except Exception as e:  # noqa: BLE001 — a raising check is a failing check
        bad(label, f"{type(e).__name__}: {e}")


def main() -> int:
    try:
        ins = load_installer()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  install-hooks-user-level.py does not import :: {e}")
        return 1
    print("=== 1. settings.json bytes are code-page-proof ===")
    _run("settings bytes are pure ascii", test_settings_bytes_are_pure_ascii, ins)
    print("=== 2. hook paths round-trip through a legacy code page ===")
    _run("round trip through legacy codepages",
         test_paths_round_trip_through_legacy_codepages, ins)
    _run("locale-default reader sees the same paths",
         test_locale_default_reader_sees_the_same_paths, ins)
    print("=== 3. escaping changes bytes only, never semantics ===")
    _run("utf8 reader semantics unchanged", test_utf8_reader_semantics_unchanged, ins)
    _run("reinstall is byte-idempotent", test_write_is_idempotent_across_a_reinstall, ins)
    print(f"\n=== summary: {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
