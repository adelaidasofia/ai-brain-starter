#!/usr/bin/env python3
"""Regression locks for the Windows install bugs reported 2026-07-31.

Two independent Windows 11 installs reported four defects between them. Each
one had the same shape: the install reported success and something silently did
not happen. These are the asserts that make each recurrence fail CI instead of a
user's machine.

  1. cp1252 decode of CHILD output (link-agent-memory never ran)
     install-hooks-user-level.py read its children with `text=True` and no
     encoding=, so the decode used the console code page. Every child prints the
     vault path, every vault path contains "⚙️ Meta", and U+FE0F's third byte
     0x8F is unmapped in cp1252 -> UnicodeDecodeError. The installer reported
     "could NOT link Claude Code memory into the vault" on machines where the
     linker was fine, and memory stayed in ~/.claude — the one outcome that
     script exists to prevent. This is the READ side of the #313 cp1252 bug
     whose WRITE side (sys.stdout.reconfigure) that file already carried.

  2. non-ASCII home path in generated hook commands (53 dead hooks)
     A Windows account named e.g. "JuanArturoGómez" put an accented character
     into every hook command. Windows hands those to a shell running a legacy
     code page, the path stops resolving, and the hooks fail on every prompt.
     The user cannot rename their account; the installer now emits the 8.3
     short path, which is ASCII by construction.

  3. hooks wired from ~/.claude/hooks/ that nothing deploys (never fired)
     hooks.json referenced three scripts by their home path behind a `[ -f ]`
     guard. Nothing copied them there, and verification skipped them because
     they were not ABS-owned. 11 references, 0 files, reported OK.

  4. the vault getting UNPATCHED scripts written over patched ones
     bootstrap auto-stashes local clone patches before pulling, then propagates
     the checkout into the vault's <meta>/scripts. In that window the checkout
     is pristine upstream, so the sync reverted the user's patched
     session-close-runner.sh and vault-safe-commit.sh. The .bak held the good
     version, which is why it went unnoticed: the run printed "Updated: 2".

Auto-discovered by scripts/ci.sh via the scripts/test_*.py glob.
Stdlib only. No network. Never writes outside a tempdir.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install-hooks-user-level.py"
GUARD = ROOT / "scripts" / "check-home-hook-deploy.py"
SYNC_SH = ROOT / "scripts" / "sync-vault-scripts.sh"

GEAR = "\u2699\ufe0f"  # the "⚙️" of "⚙️ Meta" — U+2699 U+FE0F, the exact crasher

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
    spec = importlib.util.spec_from_file_location("_abs_installer", INSTALLER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# 1. cp1252 decode of child output
# --------------------------------------------------------------------------
def test_child_output_decoded_as_utf8(ins) -> None:
    """The installer's subprocess calls must pin encoding, not inherit the locale."""
    kw = getattr(ins, "_TEXT_UTF8", None)
    if not isinstance(kw, dict) or kw.get("encoding") != "utf-8":
        bad("_TEXT_UTF8 pins encoding='utf-8'", repr(kw))
        return
    ok("_TEXT_UTF8 pins encoding='utf-8'")
    if kw.get("errors") != "replace":
        bad("_TEXT_UTF8 sets errors='replace' so a stray byte cannot crash the install")
    else:
        ok("_TEXT_UTF8 sets errors='replace'")

    # End-to-end: a child printing the gear emoji must round-trip, whatever the
    # parent's locale is. This is the literal reported payload.
    child = f"import sys; sys.stdout.reconfigure(encoding='utf-8'); print(r'C:\\v\\{GEAR} Meta\\scripts')"
    try:
        proc = subprocess.run([sys.executable, "-c", child],
                              capture_output=True, timeout=60, **kw)
    except UnicodeDecodeError as e:
        bad("child stdout containing the gear emoji decodes", str(e))
        return
    if GEAR in proc.stdout:
        ok("child stdout containing the gear emoji decodes intact")
    else:
        bad("child stdout containing the gear emoji decodes intact", repr(proc.stdout))

    # The two call sites must actually USE it. A helper nobody calls fixes
    # nothing, and this is precisely how the bug survived: the file already had
    # the cp1252 fix for its own stdout.
    src = INSTALLER.read_text(encoding="utf-8")
    naked = [ln.strip() for ln in src.splitlines()
             if "subprocess.run(" in ln and "text=True" in ln and "encoding=" not in ln]
    if naked:
        bad("no subprocess.run(text=True) without an explicit encoding remains",
            "; ".join(naked))
    else:
        ok("no subprocess.run(text=True) without an explicit encoding remains")


# --------------------------------------------------------------------------
# 2. non-ASCII Windows paths
# --------------------------------------------------------------------------
def test_ascii_safe_win_path(ins) -> None:
    plain = str(Path(tempfile.gettempdir()) / "plain" / "hook.py")
    if ins._ascii_safe_win_path(plain) == plain:
        ok("an ASCII path is returned byte-identical (no churn for the common case)")
    else:
        bad("an ASCII path is returned byte-identical")

    if os.name != "nt":
        print("SKIP  8.3 shortening (Windows-only API)")
        return

    with tempfile.TemporaryDirectory() as td:
        accented = Path(td) / "JuanArturoGómez"
        try:
            accented.mkdir()
        except OSError as e:
            print(f"SKIP  8.3 shortening (cannot create accented dir: {e})")
            return
        # The reported shape: an accented ancestor, a not-yet-created hook file.
        target = str(accented / ".claude" / "hooks" / "pre-write-settings-lint.py")
        got = ins._ascii_safe_win_path(target)
        if got.isascii():
            ok(f"non-ASCII path shortened to ASCII ({got})")
            # Shortening is only useful if it still points at the same place.
            if Path(got).parents[2] == accented or Path(os.path.realpath(
                    str(Path(got).parents[2]))) == Path(os.path.realpath(str(accented))):
                ok("the shortened path still resolves to the same directory")
            else:
                bad("the shortened path still resolves to the same directory",
                    f"{got} vs {accented}")
        elif ins._win_short_path(str(accented)) is None:
            # 8.3 disabled on this volume: degrading unchanged is the contract,
            # and platformize_template_for_windows warns loudly about it.
            ok("8.3 unavailable on this volume -> path returned unchanged (warn path)")
        else:
            bad("non-ASCII path shortened to ASCII", got)


# --------------------------------------------------------------------------
# 3. home-hook deploy
# --------------------------------------------------------------------------
def test_home_hooks_deployed(ins) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / ".claude"
        cfg.mkdir()
        (cfg / "settings.json").write_text("{}", encoding="utf-8")
        rc = subprocess.run(
            [sys.executable, str(INSTALLER),
             "--hooks-source", str(ROOT / "hooks.json"),
             "--settings", str(cfg / "settings.json"), "--quiet"],
            capture_output=True, timeout=180, **ins._TEXT_UTF8)
        missing = [n for n in ins.HOME_HOOKS_INSTALLER_DEPLOYS
                   if not (cfg / "hooks" / n).is_file()]
        if missing:
            bad("every HOME_HOOKS_INSTALLER_DEPLOYS hook lands on disk",
                f"missing {missing}; rc={rc.returncode} {rc.stderr[:200]}")
        else:
            ok(f"all {len(ins.HOME_HOOKS_INSTALLER_DEPLOYS)} installer-deployed "
               "home hooks land on disk")

        # Byte-identical to the repo copy, else the deployed hook is not the
        # hook that was reviewed.
        drifted = [n for n in ins.HOME_HOOKS_INSTALLER_DEPLOYS
                   if (cfg / "hooks" / n).is_file()
                   and (cfg / "hooks" / n).read_bytes() != (ROOT / "hooks" / n).read_bytes()]
        if drifted:
            bad("deployed copies are byte-identical to the repo", str(drifted))
        else:
            ok("deployed copies are byte-identical to the repo")

        # Idempotent: a second run must not churn or write .bak files.
        subprocess.run(
            [sys.executable, str(INSTALLER),
             "--hooks-source", str(ROOT / "hooks.json"),
             "--settings", str(cfg / "settings.json"), "--quiet"],
            capture_output=True, timeout=180, **ins._TEXT_UTF8)
        baks = list((cfg / "hooks").glob("*.bak-*"))
        if baks:
            bad("re-running does not back up unchanged hooks", str([b.name for b in baks]))
        else:
            ok("re-running is idempotent (no spurious .bak files)")


def test_verification_sees_home_hooks(ins) -> None:
    """The owned set must cover the lint hooks, or verification stays blind.

    This is the assert that would have caught the original report: 11 wired
    references, 0 files on disk, and a clean bill of health.
    """
    for name in ("pre-write-settings-lint.py", "lint-claude-settings.py"):
        cmd = f"[ -f ~/.claude/hooks/{name} ] && python3 ~/.claude/hooks/{name} || true"
        if ins.is_abs_owned(cmd):
            ok(f"{name} is ABS-owned (verification can see it)")
        else:
            bad(f"{name} is ABS-owned (verification can see it)")

    settings = {"hooks": {"SessionStart": [{"hooks": [{
        "type": "command",
        "command": "py -3 \"C:\\r\\hook_runner.py\" --fallback silent "
                   "\"C:\\Users\\x\\.claude\\hooks\\lint-claude-settings.py\"",
    }]}]}}
    missing_required, _optional = ins.verify_paths_on_disk(settings)
    if any("lint-claude-settings.py" in p for _e, p, _c in missing_required):
        ok("an undeployed home hook is reported REQUIRED-missing, not skipped")
    else:
        bad("an undeployed home hook is reported REQUIRED-missing, not skipped")


def test_bash_only_hook_not_owned(ins) -> None:
    """check-claude-code-version.sh must stay UNowned.

    Windows deliberately does not wire bash-only hooks. Owning one makes
    hooks/surface-deployed-hooks-behind.py diff it as permanently missing, so
    every Windows user gets a 'background helper is not active' nag that no
    action can clear. Deploying it does not require owning it.
    """
    if "check-claude-code-version.sh" in ins.ABS_OWNED_BASENAMES:
        bad("check-claude-code-version.sh stays unowned (bash-only, skipped on Windows)")
    else:
        ok("check-claude-code-version.sh stays unowned (no false drift nag on Windows)")
    if "check-claude-code-version.sh" in ins.HOME_HOOKS_INSTALLER_DEPLOYS:
        ok("check-claude-code-version.sh is still deployed (POSIX installs get it)")
    else:
        bad("check-claude-code-version.sh is still deployed")


def test_deploy_guard_bites() -> None:
    """Negative control: the CI guard must FAIL on the pre-fix arrangement.

    Built as a synthetic repo so the assert is about the guard's logic, not
    about this checkout happening to be healthy.
    """
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "repo"
        (fake / "scripts").mkdir(parents=True)
        (fake / "hooks").mkdir()
        (fake / "phases").mkdir()
        shutil.copyfile(GUARD, fake / "scripts" / "check-home-hook-deploy.py")
        (fake / "hooks" / "orphan-hook.py").write_text("#\n", encoding="utf-8")
        (fake / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command":
                        "[ -f ~/.claude/hooks/orphan-hook.py ] && "
                        "python3 ~/.claude/hooks/orphan-hook.py || true"}]}]}}),
            encoding="utf-8")

        def run(manifest: set[str]) -> subprocess.CompletedProcess:
            (fake / "scripts" / "install-hooks-user-level.py").write_text(
                f"HOME_HOOKS_INSTALLER_DEPLOYS = {manifest!r}\n", encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(fake / "scripts" / "check-home-hook-deploy.py")],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60)

        pre_fix = run(set())            # the shipped state: referenced, deployed by nobody
        if pre_fix.returncode != 0 and "orphan-hook.py" in pre_fix.stderr:
            ok("guard FAILS on a referenced-but-never-deployed hook (negative control)")
        else:
            bad("guard FAILS on a referenced-but-never-deployed hook",
                f"rc={pre_fix.returncode} {pre_fix.stdout}{pre_fix.stderr}")

        fixed = run({"orphan-hook.py"})  # adopted by the installer
        if fixed.returncode == 0:
            ok("guard PASSES once the installer adopts the hook (positive control)")
        else:
            bad("guard PASSES once the installer adopts the hook",
                f"rc={fixed.returncode} {fixed.stdout}{fixed.stderr}")


# --------------------------------------------------------------------------
# 4. --verify-only writes nothing
# --------------------------------------------------------------------------
def test_verify_only_is_read_only(ins) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / ".claude"
        cfg.mkdir()
        settings = cfg / "settings.json"
        settings.write_text('{"hooks": {}}', encoding="utf-8")
        before = settings.read_bytes()
        proc = subprocess.run(
            [sys.executable, str(INSTALLER), "--settings", str(settings), "--verify-only"],
            capture_output=True, timeout=120, **ins._TEXT_UTF8)
        if settings.read_bytes() != before:
            bad("--verify-only leaves settings.json byte-identical")
        elif list(cfg.glob("settings.json.bak-*")):
            bad("--verify-only writes no backup file")
        elif (cfg / "hooks").exists():
            bad("--verify-only deploys nothing")
        elif "--- Verification ---" not in proc.stdout:
            bad("--verify-only still prints the verification report", proc.stdout[:200])
        else:
            ok("--verify-only writes nothing and still reports")


# --------------------------------------------------------------------------
# 5. the vault-sync refusal
# --------------------------------------------------------------------------
def test_vault_sync_refuses_stashed_clone() -> None:
    bash = shutil.which("bash")
    if not bash:
        print("SKIP  vault-sync refusal (no bash on PATH)")
        return
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        # A vault whose meta dir carries the real gear emoji, and a patched copy
        # of the exact script the reporter lost.
        meta = td_p / "vault" / f"{GEAR} Meta"
        (meta / "scripts").mkdir(parents=True)
        patched = meta / "scripts" / "session-close-runner.sh"
        patched.write_text("#!/bin/bash\n# LOCAL PATCH — must survive\n", encoding="utf-8")

        # A starter checkout holding the UNPATCHED upstream copy.
        starter = td_p / "starter"
        (starter / "scripts").mkdir(parents=True)
        for name in ("sync-vault-scripts.sh", "_meta_resolver.py"):
            shutil.copyfile(ROOT / "scripts" / name, starter / "scripts" / name)
        (starter / "scripts" / "session-close-runner.sh").write_text(
            "#!/bin/bash\n# pristine upstream\n", encoding="utf-8")

        env = dict(os.environ)
        env["STARTER_DIR"] = str(starter)
        env["VAULT_ROOT"] = str(td_p / "vault")
        env["ABS_CLONE_PATCHES_STASHED"] = "bootstrap auto-stash 2026-07-31-1200"
        proc = subprocess.run([bash, str(starter / "scripts" / "sync-vault-scripts.sh"),
                               "--quiet"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=120)

        if "LOCAL PATCH" not in patched.read_text(encoding="utf-8"):
            bad("a stashed clone does NOT overwrite the patched vault script",
                proc.stdout + proc.stderr)
        else:
            ok("a stashed clone does NOT overwrite the patched vault script")
        if proc.returncode != 0:
            bad("the refusal is non-fatal (exit 0)", f"rc={proc.returncode}")
        else:
            ok("the refusal is non-fatal (exit 0)")
        # Must speak up even under --quiet: bootstrap always calls it that way,
        # and a silent skip is how the original regression hid.
        if "SKIPPED" in proc.stdout and "git stash" in proc.stdout:
            ok("the refusal is announced under --quiet, naming the stash")
        else:
            bad("the refusal is announced under --quiet, naming the stash",
                repr(proc.stdout[:300]))

        # Positive control: without the flag it must still do its job, else this
        # test would pass on a script that simply never syncs anything.
        del env["ABS_CLONE_PATCHES_STASHED"]
        subprocess.run([bash, str(starter / "scripts" / "sync-vault-scripts.sh"), "--quiet"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=120)
        if "pristine upstream" in patched.read_text(encoding="utf-8"):
            ok("without the flag the sync still propagates (positive control)")
        else:
            bad("without the flag the sync still propagates (positive control)")


def test_memory_link_falls_back_to_junction() -> None:
    """os.symlink is privileged on Windows; the memory link must survive that.

    WinError 1314 ("a required privilege is not held by the client") is what a
    normal, non-elevated Windows account gets without Developer Mode. That is
    the step which makes "your brain lives in your vault" true, so failing it
    strands every future memory in ~/.claude, invisible in Obsidian. Forced
    here rather than waiting for an unprivileged runner, so the fallback is
    exercised on every box.
    """
    if os.name != "nt":
        print("SKIP  junction fallback (Windows-only)")
        return
    spec = importlib.util.spec_from_file_location(
        "_abs_linker", ROOT / "scripts" / "link-agent-memory.py")
    linker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(linker)

    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        (vault / ".obsidian").mkdir(parents=True)
        home = Path(td) / "home"
        home.mkdir()

        real_symlink_to = Path.symlink_to

        def refuse(self, *a, **k):
            raise OSError(1314, "A required privilege is not held by the client")

        env_home = dict(os.environ)
        Path.symlink_to = refuse  # simulate an unprivileged Windows account
        try:
            os.environ["USERPROFILE"] = str(home)
            os.environ["HOME"] = str(home)
            mem = linker.link_agent_memory(str(vault), quiet=True)
        except SystemExit as e:
            bad("memory link falls back to a junction when symlink is refused", str(e))
            return
        finally:
            Path.symlink_to = real_symlink_to
            os.environ.clear()
            os.environ.update(env_home)

        agent_mem = vault / f"{GEAR} Meta" / "Agent Memory"
        if not linker._same_target(mem, agent_mem):
            bad("the junction resolves to the vault's Agent Memory dir")
            return
        ok("memory link falls back to a junction when symlink is refused")

        (agent_mem / "MEMORY.md").write_text("brain", encoding="utf-8")
        if (mem / "MEMORY.md").read_text(encoding="utf-8") == "brain":
            ok("memory written in the vault is readable through the junction")
        else:
            bad("memory written in the vault is readable through the junction")

        # Idempotency: a junction is not a symlink to Python, so a re-run that
        # did not recognise it would "migrate" the vault into itself and rename
        # the link aside — silently unlinking the brain on every update.
        Path.symlink_to = refuse
        try:
            os.environ["USERPROFILE"] = str(home)
            os.environ["HOME"] = str(home)
            linker.link_agent_memory(str(vault), quiet=True)
        finally:
            Path.symlink_to = real_symlink_to
            os.environ.clear()
            os.environ.update(env_home)
        if linker._same_target(mem, agent_mem) and not list(
                mem.parent.glob("memory.pre-link-backup*")):
            ok("re-running over an existing junction is a clean no-op")
        else:
            bad("re-running over an existing junction is a clean no-op",
                str(list(mem.parent.glob('*'))))


def test_bootstraps_set_the_flag() -> None:
    """Both bootstraps must export the flag, or the guard is unreachable."""
    for name, needle in (("bootstrap.sh", 'export ABS_CLONE_PATCHES_STASHED='),
                         ("bootstrap.ps1", '$env:ABS_CLONE_PATCHES_STASHED =')):
        text = (ROOT / name).read_text(encoding="utf-8")
        if needle in text:
            ok(f"{name} sets ABS_CLONE_PATCHES_STASHED after an auto-stash")
        else:
            bad(f"{name} sets ABS_CLONE_PATCHES_STASHED after an auto-stash")


def _run(label, fn, *a) -> None:
    """Run one check; an exception is a FAILURE, not an abort.

    Each check locks an INDEPENDENT defect. Letting the first missing symbol
    (AttributeError against an older installer) kill the process would hide
    every later result — the false-green shape these tests exist to prevent.
    """
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
    print("=== 1. cp1252 decode of child output ===")
    _run("child output decoded as utf-8", test_child_output_decoded_as_utf8, ins)
    print("=== 2. non-ASCII Windows paths ===")
    _run("ascii-safe windows paths", test_ascii_safe_win_path, ins)
    print("=== 3. home-hook deploy ===")
    _run("home hooks deployed", test_home_hooks_deployed, ins)
    _run("verification sees home hooks", test_verification_sees_home_hooks, ins)
    _run("bash-only hook not owned", test_bash_only_hook_not_owned, ins)
    _run("deploy guard bites", test_deploy_guard_bites)
    print("=== 4. --verify-only is read-only ===")
    _run("--verify-only is read-only", test_verify_only_is_read_only, ins)
    print("=== 5. vault-sync refuses a stashed clone ===")
    _run("vault sync refuses a stashed clone", test_vault_sync_refuses_stashed_clone)
    _run("bootstraps set the flag", test_bootstraps_set_the_flag)
    print("=== 6. memory link survives an unprivileged Windows account ===")
    _run("memory link falls back to a junction", test_memory_link_falls_back_to_junction)
    print(f"\n=== summary: {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
