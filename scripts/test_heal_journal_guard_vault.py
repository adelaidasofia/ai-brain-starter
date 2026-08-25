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
# Run against a fake HOME so the developer's real home cannot influence the
# result. ORDER MATTERS in this block and is load-bearing -- see the hermeticity
# assertion below.
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

        # HERMETICITY. resolve_vault() must NOT go looking on its own; the
        # self-test's 'no-vault' controls depend on it, and folding discovery in
        # made them depend on the developer's real home instead.
        #
        # This assertion MUST run while exactly ONE vault exists. Asserted after a
        # second vault is created it is VACUOUS: discovery returns "" under
        # ambiguity, so `resolve_vault(...) == ""` holds whether or not discovery
        # is wired in, and the check can never fail. A check that cannot fail is
        # the exact defect this whole file exists to prevent.
        check("resolve-vault-never-discovers-on-its-own",
              heal.resolve_vault(WINDOWS_SETTINGS) == "")

        # Canonicalisation: one vault reachable under TWO names is ONE vault.
        # Without resolve() a junction/mirrored copy read as ambiguous and
        # switched discovery off entirely (a normal Windows layout).
        link = fake_home / "VaultTwin"
        made_link = False
        try:
            link.symlink_to(fake_home / "OnlyVault", target_is_directory=True)
            made_link = True
        except (OSError, NotImplementedError):
            pass  # unprivileged Windows without Developer Mode; skip, don't fail
        if made_link:
            check("same-vault-under-two-names-is-not-ambiguous",
                  heal._discover_vault() == str(fake_home / "OnlyVault"))
            link.unlink()

        # Two GENUINELY distinct candidates -> refuse to choose. Healing the
        # wrong vault would write a preflight into someone else's notes.
        make_vault(fake_home / "SecondVault")
        check("discovery-refuses-when-ambiguous", heal._discover_vault() == "")
        check("ambiguous-candidates-are-still-enumerable",
              len(heal._vault_candidates()) == 2)

        # One un-stat-able child must not abort the whole scan. Previously the
        # try/except wrapped the entire loop, so a single unreadable entry (an
        # ACL we cannot stat, a stale network mount) turned discovery off for the
        # whole account.
        #
        # Wording note: keep hook BASENAMES out of prose in this file.
        # check-hook-negative-control.py decides a hook is covered when its
        # basename appears anywhere in the test corpus, so merely naming one in a
        # COMMENT makes an untested hook look tested and trips the ratchet that
        # asks for its removal from NO_TEST_BASELINE. Prose must not be able to
        # manufacture coverage -- that is the failure this whole file guards
        # against, and the check is substring-based, so it cannot tell the
        # difference between a test and a sentence.
        import shutil as _shutil
        _shutil.rmtree(fake_home / "SecondVault")
        _real_is_dir = Path.is_dir
        boom = fake_home / "Exploding"
        boom.mkdir()

        def _is_dir_that_explodes(self):
            if self.name == "Exploding":
                raise PermissionError("simulated unreadable child")
            return _real_is_dir(self)

        Path.is_dir = _is_dir_that_explodes  # type: ignore[assignment]
        try:
            check("one-unreadable-child-does-not-abort-the-scan",
                  heal._discover_vault() == str(fake_home / "OnlyVault"))
        finally:
            Path.is_dir = _real_is_dir  # type: ignore[assignment]
    finally:
        heal.Path.home = _real_home  # type: ignore[assignment]

# --- WIRING: the production path must actually USE discovery -----------------
# Everything above tests the helpers in isolation. None of it notices if the four
# lines in run_session_start() that call discovery are deleted -- the feature can
# be unplugged and every assertion above still passes. That is the same
# artifact-without-activation gap MYC-3875 is about, so lock the call site
# itself: drive run_session_start() end to end and assert it SPEAKS.
import io  # noqa: E402
import json as _json  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402


def _drive_session_start(home: Path, vaults: list, with_preflight: bool):
    """Run run_session_start() against a sandbox home; return its emitted JSON."""
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    # Registration healthy, so the ONLY possible gap is the vault preflight.
    guard = claude / heal.GUARD_BASENAME
    guard.write_text("#!/usr/bin/env python3\nprint('{}')\n", encoding="utf-8")
    groups = [{"matcher": m, "hooks": [{"type": "command",
                                        "command": f'py -3 "{guard}"'}]}
              for m in sorted(heal.wanted_matchers(heal.clone_root()))]
    (claude / "settings.json").write_text(
        _json.dumps({"hooks": {"PreToolUse": groups}}), encoding="utf-8")
    for v in vaults:
        make_vault(home / v, with_preflight=with_preflight)

    _home = heal.Path.home
    heal.Path.home = staticmethod(lambda: home)  # type: ignore[assignment]
    os.environ["HEAL_JOURNAL_GUARD_NO_COOLDOWN"] = "0"
    heal._stamp_cooldown()  # pre-stamp: no repair may spawn during a test
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            heal.run_session_start()
    finally:
        heal.Path.home = _home  # type: ignore[assignment]
        os.environ.pop("HEAL_JOURNAL_GUARD_NO_COOLDOWN", None)
    try:
        return _json.loads(buf.getvalue() or "{}")
    except _json.JSONDecodeError:
        return {}


def _context_of(payload):
    return ((payload.get("hookSpecificOutput") or {})
            .get("additionalContext", ""))


with tempfile.TemporaryDirectory() as td3:
    home = Path(td3) / "home"
    out = _drive_session_start(home, ["TheVault"], with_preflight=False)
    # Discovery must find TheVault, notice the missing preflight, and SAY so.
    # Unwire the discovery block in run_session_start() and this goes red.
    check("WIRING-session-start-uses-discovery",
          "incomplete" in _context_of(out).lower())
    check("WIRING-session-start-not-silent-on-a-real-gap",
          not out.get("suppressOutput"))

with tempfile.TemporaryDirectory() as td4:
    home = Path(td4) / "home"
    out = _drive_session_start(home, ["VaultA", "VaultB"], with_preflight=False)
    ctx = _context_of(out)
    # Ambiguity must refuse to heal, but must NOT be silent about refusing --
    # a silent refusal is the original bug wearing a different hat.
    check("WIRING-ambiguous-home-is-reported", "candidate vaults" in ctx)
    check("WIRING-ambiguous-report-names-the-fix", "VAULT_ROOT" in ctx)

with tempfile.TemporaryDirectory() as td5:
    home = Path(td5) / "home"
    out = _drive_session_start(home, ["TheVault"], with_preflight=True)
    check("WIRING-healthy-account-stays-silent", bool(out.get("suppressOutput")))

# --- WIRING: --vault must reach the DIAGNOSIS, not just the repair -----------
with tempfile.TemporaryDirectory() as td6:
    v = make_vault(Path(td6) / "V", with_preflight=False)

    class _Args:
        clone = None
        settings = None
        vault = str(v)

    args = _Args()
    args.settings = str(Path(td6) / "settings.json")
    Path(args.settings).write_text(_json.dumps(WINDOWS_SETTINGS), encoding="utf-8")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = heal.cmd_check_only(args)
    # Before the fix --vault reached only repair_preflight(), so the DIAGNOSIS
    # still answered 'no-vault' and, since that is not a gap, nothing ran.
    check("WIRING-check-only-threads-vault", "preflight=missing" in buf.getvalue())
    check("WIRING-check-only-exits-nonzero-on-a-real-gap", rc == 1)

if _saved_vault_root is not None:
    os.environ["VAULT_ROOT"] = _saved_vault_root

if FAILS:
    print("FAIL: " + ", ".join(FAILS), file=sys.stderr)
    sys.exit(1)
print("test_heal_journal_guard_vault OK: the preflight check runs on the Windows "
      "settings shape, and a deliberate deletion is detected")
