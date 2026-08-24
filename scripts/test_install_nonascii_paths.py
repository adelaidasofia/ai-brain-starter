#!/usr/bin/env python3
"""
test_install_nonascii_paths.py - the non-ASCII profile fix, and how it degrades
when its precondition is absent (MYC-3878, ai-brain-starter#397).

Run: python3 scripts/test_install_nonascii_paths.py
No pytest dependency. Exits non-zero on any failure. Writes nothing.

WHAT #397 FIXED. A Windows account like "JuanArturoGomez" with an accent in the
profile name put non-ASCII into every hook command. Claude Code hands those to a
shell on a legacy code page (cp1252), the accented byte is mangled, the path stops
resolving, and EVERY hook fails -- which blocks the session outright.

HOW it was fixed: _ascii_safe_win_path() rewrites the path to its 8.3 short name
(C:\\Users\\JUANAR~1), ASCII by construction.

WHY THIS FILE EXISTS: that fix has a PRECONDITION nobody was checking. 8.3 name
creation is per-volume (NtfsDisable8dot3NameCreation=2 is the Win10/11 default)
and corporate GPOs routinely disable it. When it is off, _win_short_path() returns
None and the fix silently degrades -- the installer writes a command carrying a
literal accent, which is the ORIGINAL #397 failure, for exactly the population the
fix was written for.

CI cannot catch this: the Windows runner job runs under an ASCII profile on a
volume with 8.3 enabled, so it exercises neither leg. These asserts force the
8.3-unavailable branch by stubbing _win_short_path, which is the only way to reach
it without a specially-provisioned volume.
"""
from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    path = ROOT / "scripts" / "install-hooks-user-level.py"
    spec = importlib.util.spec_from_file_location("_abs_nonascii_test", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


inst = _load()
FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    if not cond:
        FAILS.append(name)


ASCII_PATH = r"C:\Users\dev\.claude\skills\abs\hooks\x.py"
ACCENT_PATH = "C:\\Users\\Jos\u00e9\\.claude\\skills\\abs\\hooks\\x.py"

# --- an ASCII path must be returned byte-identical -------------------------
# The overwhelmingly common case. If this ever churns, every existing install
# rewrites its whole hook table on the next run for no reason.
check("ascii-path-unchanged", inst._ascii_safe_win_path(ASCII_PATH) == ASCII_PATH)

# --- 8.3 AVAILABLE: the accented path is made ASCII ------------------------
_real_short = inst._win_short_path
try:
    inst._win_short_path = lambda p: (  # type: ignore[assignment]
        p.replace("Jos\u00e9", "JOS~1") if "Jos\u00e9" in p else None)
    got = inst._ascii_safe_win_path(ACCENT_PATH)
    check("shortened-path-is-ascii", got.isascii())
    check("shortened-path-has-short-name", "JOS~1" in got)
finally:
    inst._win_short_path = _real_short  # type: ignore[assignment]

# --- 8.3 UNAVAILABLE: degrade honestly, never silently ---------------------
# This is the corporate-GPO case. _ascii_safe_win_path must hand the path BACK
# unchanged (documented contract) so the caller can detect and warn, rather than
# emitting a silently-wrong command.
try:
    inst._win_short_path = lambda p: None  # type: ignore[assignment]
    got = inst._ascii_safe_win_path(ACCENT_PATH)
    check("no-8dot3-returns-path-unchanged", got == ACCENT_PATH)
    check("no-8dot3-path-is-still-non-ascii", not got.isascii())
finally:
    inst._win_short_path = _real_short  # type: ignore[assignment]

# --- and the caller must SAY so --------------------------------------------
# The contract is only useful if platformize_for_windows actually reports it. The
# symptom otherwise is "every hook errors on every prompt" with no legible cause,
# and the user cannot rename their Windows account.
template = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": f'python3 "{ACCENT_PATH}"'}]}]}}

_real_launcher = inst._windows_launcher
try:
    inst._win_short_path = lambda p: None            # type: ignore[assignment]
    inst._windows_launcher = lambda: ["py", "-3"]    # type: ignore[assignment]
    buf = io.StringIO()
    with redirect_stderr(buf):
        inst.platformize_template_for_windows(template)
    err = buf.getvalue()
    check("warns-when-8dot3-unavailable", "could" in err and "NOT be shortened" in err)
    check("warning-names-the-cause", "8.3" in err)
    check("warning-names-a-remedy",
          "fsutil" in err or "all-ASCII" in err)
    check("warning-names-the-offending-path", "x.py" in err)
finally:
    inst._win_short_path = _real_short               # type: ignore[assignment]
    inst._windows_launcher = _real_launcher          # type: ignore[assignment]

# --- silence is the bug, so prove it is NOT silent on the happy path -------
# A shortened (ASCII) path must produce NO warning, or the warning becomes noise
# every install and stops being read.
try:
    inst._win_short_path = lambda p: (  # type: ignore[assignment]
        p.replace("Jos\u00e9", "JOS~1") if "Jos\u00e9" in p else None)
    inst._windows_launcher = lambda: ["py", "-3"]    # type: ignore[assignment]
    buf = io.StringIO()
    with redirect_stderr(buf):
        inst.platformize_template_for_windows(template)
    check("no-warning-when-shortening-worked",
          "NOT be shortened" not in buf.getvalue())
finally:
    inst._win_short_path = _real_short               # type: ignore[assignment]
    inst._windows_launcher = _real_launcher          # type: ignore[assignment]

if FAILS:
    print("FAIL: " + ", ".join(FAILS), file=sys.stderr)
    sys.exit(1)
print("test_install_nonascii_paths OK: 8.3 shortening works, and its absence "
      "degrades loudly rather than silently")
