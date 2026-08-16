#!/usr/bin/env python3
"""
test_heal_journal_guard_vault.py - the preflight check must actually RUN on Windows
(MYC-3875 / MYC-3362).

Run: python3 scripts/test_heal_journal_guard_vault.py
No pytest dependency. Exits non-zero on any failure. Operates entirely in a temp
dir - never touches the real ~/.claude or any real vault.

THE BUG. preflight_state() -> resolve_vault(settings) had two sources and BOTH are
unavailable on a real Windows install:

  1. the command parse wants a command containing `<vault>/<meta>/scripts/`, but
     the ONLY commands of that shape are the POSIX `bash` vault hooks, and
     platformize_for_windows() drops every bash hook before settings.json is
     written. Measured on real Windows installs (fresh AND long-lived): ZERO
     matching commands.
  2. $VAULT_ROOT was unset in-process, unset at User and Machine scope, and
     settings.json carried no `env` block to export it.

So resolve_vault() returned "" -> preflight_state() returned 'no-vault' -> and
has_gap() does not count 'no-vault'. The check and its repair were a SILENT no-op:
an account missing journal-preflight.py read as perfectly healthy.

#485 widened _VAULT_FROM_CMD_RE to accept drive letters and backslashes. That was
correct but insufficient -- a widened pattern cannot match input that is no longer
present. The existing self-test missed it because its vault controls feed the
regex SYNTHETIC strings that already contain the vault path; it never ran
resolve_vault() against a Windows-platformized settings.json.

The centerpiece here is test "deliberate deletion is DETECTED" -- the exact
scenario MYC-3874 step 3 demanded, which reported OK before this fix.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    path = ROOT / "scripts" / "heal-journal-guard.py"
    spec = importlib.util.spec_from_file_location("_heal_test", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


heal = _load()
FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    if not cond:
        FAILS.append(name)


# A settings.json in the shape platformize_for_windows() actually writes: the
# bash vault hooks are GONE, so nothing carries a vault path.
WINDOWS_SETTINGS = {"hooks": {"SessionStart": [{"hooks": [{
    "type": "command",
    "command": ('py -3 "C:\\u\\.claude\\skills\\abs\\scripts\\hook_runner.py" '
                '--fallback silent "C:\\u\\.claude\\skills\\abs\\hooks\\x.py"'),
}]}]}}


def make_vault(root: Path, meta: str = "⚙️ Meta", with_preflight: bool = True) -> Path:
    scripts = root / meta / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    if with_preflight:
        (scripts / heal.PREFLIGHT_BASENAME).write_text("# stub\n", encoding="utf-8")
    return root


_saved_vault_root = os.environ.pop("VAULT_ROOT", None)

with tempfile.TemporaryDirectory() as td:
    root = Path(td)

    # --- premise: the Windows shape really does defeat the command parse -------
    # If this ever starts matching, the bug shape changed and the rest is moot.
    hits = [h["command"] for g in WINDOWS_SETTINGS["hooks"]["SessionStart"]
            for h in g["hooks"]
            if heal._VAULT_FROM_CMD_RE.search(h["command"])]
    check("premise-windows-settings-carry-no-vault-command", hits == [])
    check("premise-resolve-vault-is-empty-without-help",
          heal.resolve_vault(WINDOWS_SETTINGS) == "")

    # --- explicit --vault revives the check -----------------------------------
    vault = make_vault(root / "MyVault")
    check("explicit-vault-resolves",
          heal.resolve_vault(WINDOWS_SETTINGS, str(vault)) == str(vault))
    check("explicit-vault-preflight-ok",
          heal.preflight_state(WINDOWS_SETTINGS, str(vault)) == "ok")

    # THE CENTERPIECE: MYC-3874 step 3. Delete journal-preflight.py; the state must
    # become 'missing' (a real gap that has_gap() counts), NOT 'no-vault'.
    (vault / "⚙️ Meta" / "scripts" / heal.PREFLIGHT_BASENAME).unlink()
    state = heal.preflight_state(WINDOWS_SETTINGS, str(vault))
    check("deliberate-deletion-is-DETECTED", state == "missing")
    check("deletion-is-a-real-gap",
          heal.has_gap({"missing_matchers": [], "preflight": state}))
    # ...and specifically NOT the silent no-op the pre-fix code produced.
    check("deletion-is-not-silently-no-vault", state != "no-vault")

    # A bad --vault must not be trusted into a false 'missing'.
    check("nonexistent-explicit-vault-is-ignored",
          heal.preflight_state(WINDOWS_SETTINGS, str(root / "nope")) == "no-vault")

    # --- $VAULT_ROOT still works and outranks discovery ------------------------
    v2 = make_vault(root / "EnvVault")
    os.environ["VAULT_ROOT"] = str(v2)
    try:
        check("env-vault-root-honored",
              heal.resolve_vault(WINDOWS_SETTINGS) == str(v2))
        check("explicit-outranks-env",
              heal.resolve_vault(WINDOWS_SETTINGS, str(vault)) == str(vault))
    finally:
        os.environ.pop("VAULT_ROOT", None)

    # --- the non-emoji "Meta" fallback (MYC-3362 asked for this explicitly) ----
    plain = make_vault(root / "PlainVault", meta="Meta")
    check("plain-Meta-fallback-resolves",
          heal.preflight_state(WINDOWS_SETTINGS, str(plain)) == "ok")

# --- discovery: only when unambiguous ---------------------------------------
# Run against a fake HOME so the developer's real home cannot influence the result.
with tempfile.TemporaryDirectory() as td2:
    fake_home = Path(td2)
    _real_home = heal.Path.home

    heal.Path.home = staticmethod(lambda: fake_home)  # type: ignore[assignment]
    try:
        check("discovery-finds-nothing-when-no-vault-exists",
              heal._discover_vault() == "")

        make_vault(fake_home / "OnlyVault")
        check("discovery-finds-the-single-vault",
              heal._discover_vault() == str(fake_home / "OnlyVault"))
        # Feeding the discovered vault back in is what run_session_start() does.
        check("discovered-vault-revives-preflight",
              heal.preflight_state(WINDOWS_SETTINGS, heal._discover_vault()) == "ok")

        # Two candidates -> refuse to choose. Healing the wrong vault would write a
        # preflight into someone else's notes.
        make_vault(fake_home / "SecondVault")
        check("discovery-refuses-when-ambiguous", heal._discover_vault() == "")

        # resolve_vault() itself must stay HERMETIC: it must NOT go looking. The
        # self-test's 'no-vault' controls depend on this, and folding discovery in
        # made them depend on the developer's real home instead.
        check("resolve-vault-never-discovers-on-its-own",
              heal.resolve_vault(WINDOWS_SETTINGS) == "")
    finally:
        heal.Path.home = _real_home  # type: ignore[assignment]

if _saved_vault_root is not None:
    os.environ["VAULT_ROOT"] = _saved_vault_root

if FAILS:
    print("FAIL: " + ", ".join(FAILS), file=sys.stderr)
    sys.exit(1)
print("test_heal_journal_guard_vault OK: the preflight check runs on the Windows "
      "settings shape, and a deliberate deletion is detected")
