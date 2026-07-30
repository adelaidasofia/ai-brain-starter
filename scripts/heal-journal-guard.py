#!/usr/bin/env python3
"""heal-journal-guard.py - SessionStart self-heal for the /journal Step-0 context guard.

WHY THIS EXISTS
  The ENFORCEMENT of daily-journal Step 0 is three pieces of PER-ACCOUNT state under
  ~/.claude, not the vault:
    - the guard hook `warn-journal-saved-without-context.py` on disk,
    - its registration in ~/.claude/settings.json PreToolUse under BOTH matchers,
    - the vault-side `journal-preflight.py` in <vault>/<meta>/scripts/.
  If an account's auto-sync never ran `install-hooks-user-level.py`, the guard is
  unregistered and /journal can silently ship an entry with ZERO context again - the
  2026-07-07 incident this whole layer exists to kill. Instructions in a repo are not
  durable across accounts; only infrastructure is. This SessionStart check re-derives
  the guard from the vault-synced substrate on EVERY session and repairs it if missing,
  so a stale/unprotected account self-heals with no manual per-account update.

WHAT IT VERIFIES + REPAIRS  (idempotent, FAIL-OPEN, fast - never a corpus walk)
  1. REGISTRATION: the guard is wired in ~/.claude/settings.json PreToolUse under every
     matcher hooks.json declares (Bash and Write|Edit|MultiEdit today) AND each such
     command points at a guard script that exists on disk. Repair: merge the CANONICAL
     guard entries straight out of the clone's hooks.json using
     install-hooks-user-level.py's OWN merge/interpreter/platformize/write helpers (the
     existing hook-add mechanism), so what lands is byte-identical to a normal install
     and dedups cleanly against a later installer run.
  2. PREFLIGHT: `journal-preflight.py` is present in the vault's <meta>/scripts/. Repair:
     invoke the existing sync-vault-scripts.sh (sync-vault-scripts.ps1 on Windows) -
     REUSE, never reimplement the vault-script sync.

  If it repairs anything it appends to ~/.claude/heal-journal-guard.log and surfaces ONE
  SessionStart line so the operator learns that account had been unprotected.

FAIL-OPEN: any ambiguity / error -> emit a silent continue and exit 0. It must NEVER
block or slow a healthy session start. A repair subprocess is gated behind a cooldown so
a persistently-broken box cannot re-spawn the installer every single session.

Registered on SessionStart via hooks.json; OWNED + verified by install-hooks-user-level.py
(ARTIFACT-WITHOUT-ACTIVATION: a self-heal the installer does not register is dormant).
Bypass: HEAL_JOURNAL_GUARD_BYPASS=1 (env).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

GUARD_BASENAME = "warn-journal-saved-without-context.py"
GUARD_TOKEN = "warn-journal-saved-without-context"
PREFLIGHT_BASENAME = "journal-preflight.py"
# Fallback only if the clone's hooks.json is unreadable; the live source of truth is
# always the matchers hooks.json actually registers the guard under.
FALLBACK_MATCHERS = ("Bash", "Write|Edit|MultiEdit")
HEAL_COOLDOWN_SECONDS = 6 * 3600
# A guard command names its script as a quoted or bare ABSOLUTE path ending in the guard
# basename. Covers POSIX (`/...`, `~/...`) AND Windows drive paths (`C:\...`), quoted or
# bare, with or without spaces in the path. Tolerates both the POSIX
# `python3 <path> 2>/dev/null || echo ...` fallback-chain form and the Windows
# `py -3 "<hook_runner>" --fallback allow "<guard>"` form the installer writes.
#
# WHY the platform branch matters: install-hooks-user-level.py writes Windows ABS paths
# (`"C:\Users\...\warn-journal-saved-without-context.py"`). A POSIX-only pattern never
# matches them, so _guard_script_exists() returns False for a guard that IS correctly
# registered and DOES exist -> every matcher reads as missing -> this hook nags on every
# session forever, and the install command it tells the user to run cannot ever clear it.
_ABS_START = r"(?:~|/|[A-Za-z]:[\\/])"
_GUARD_PATH_RE = re.compile(
    r"\"(" + _ABS_START + r"[^\"]*?" + re.escape(GUARD_BASENAME) + r")\""
    r"|'(" + _ABS_START + r"[^']*?" + re.escape(GUARD_BASENAME) + r")'"
    # Bare form must begin at an argument boundary, else the `/` inside a RELATIVE path
    # (`hooks/warn-...py`) matches and a relative command passes as absolute.
    r"|(?:^|(?<=[\s=]))(" + _ABS_START + r"[^\s'\"|&;]*?"
    + re.escape(GUARD_BASENAME) + r")"
)
# The installed vault hooks embed the absolute vault path right before the meta folder;
# same shape sync-vault-scripts.(sh|ps1) uses, so all three resolve the identical root.
# Separator is [\\/] and the drive-letter form is accepted for the same reason as above.
_VAULT_FROM_CMD_RE = re.compile(
    r"((?:/|[A-Za-z]:[\\/])[^'\"]+?)[\\/](?:⚙️ Meta|Meta)[\\/]scripts[\\/]"
)


# --------------------------------------------------------------------------- helpers
def clone_root() -> Path:
    """The ai-brain-starter checkout this script ships in (scripts/.. == clone root)."""
    return Path(__file__).resolve().parents[1]


def _read_json(path: Path) -> dict:
    """Parsed JSON, or {} when the file is absent OR unreadable. Callers that only READ
    (diagnose) are happy with {}; a caller that WRITES the result back must first call
    _settings_unreadable() - see the refusal in repair_registration."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _settings_unreadable(path: Path) -> bool:
    """True iff the file EXISTS but does not parse as a JSON object.

    The distinction matters only on the WRITE path. _read_json flattens "absent" and
    "corrupt" to {}, and merging into {} yields a settings.json containing ONLY our guard
    entries - which, written back over a corrupt-but-recoverable file, DESTROYS the user's
    env, permissions and every custom hook. Absent is safe to treat as {}; corrupt is not.
    The real installer refuses outright on unparseable settings; the self-heal must too.
    Fail CLOSED: an unreadable file means do nothing and say so, never rewrite it."""
    if not path.is_file():
        return False  # absent -> a fresh {} is correct, not destructive
    try:
        return not isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    except Exception:
        return True


def guard_registrations_from_template(clone: Path) -> "list[tuple]":
    """Every (matcher, command) pair for the guard in the clone's hooks.json PreToolUse."""
    tmpl = _read_json(clone / "hooks.json")
    out = []
    for group in (tmpl.get("hooks") or {}).get("PreToolUse", []) or []:
        matcher = group.get("matcher")
        for h in group.get("hooks", []) or []:
            cmd = h.get("command", "") or ""
            if GUARD_TOKEN in cmd:
                out.append((matcher, cmd))
    return out


def wanted_matchers(clone: Path) -> "set":
    """Matchers the guard MUST be registered under - read from hooks.json, so this hook
    follows the template if the canonical matcher set ever changes (zero drift)."""
    regs = guard_registrations_from_template(clone)
    matchers = {m for (m, _c) in regs if m}
    return matchers or set(FALLBACK_MATCHERS)


def _guard_script_exists(cmd: str) -> bool:
    """True if the guard script named in this command resolves to a real file on disk."""
    m = _GUARD_PATH_RE.search(cmd)
    if not m:
        return False
    # One group per quoting form (double / single / bare); exactly one is populated.
    path = m.group(1) or m.group(2) or m.group(3)
    return Path(os.path.expanduser(path)).is_file()


def healthy_matchers(settings: dict) -> "set":
    """Matchers under which the guard is registered AND its script exists on disk.

    Folding script-existence into the per-matcher health means a registration that
    points at a MISSING script (a stale path from a moved clone) counts as a gap and
    gets re-registered to the canonical hooks.json form, not silently trusted."""
    ok = set()
    for group in (settings.get("hooks") or {}).get("PreToolUse", []) or []:
        matcher = group.get("matcher")
        for h in group.get("hooks", []) or []:
            cmd = h.get("command", "") or ""
            if GUARD_TOKEN in cmd and _guard_script_exists(cmd):
                ok.add(matcher)
    return ok


def resolve_vault(settings: dict) -> "str":
    """Vault root from $VAULT_ROOT, else parsed out of an installed vault-hook command.
    Empty string when no vault can be resolved (a box with no vault set up yet)."""
    env = os.environ.get("VAULT_ROOT")
    if env and Path(env).is_dir():
        return env
    for _ev, groups in (settings.get("hooks") or {}).items():
        for g in groups or []:
            for h in g.get("hooks", []) or []:
                m = _VAULT_FROM_CMD_RE.search(h.get("command", "") or "")
                if m and Path(m.group(1)).is_dir():
                    return m.group(1)
    return ""


def preflight_state(settings: dict) -> str:
    """'ok' | 'missing' | 'no-vault' for journal-preflight.py in the vault's meta scripts."""
    vault = resolve_vault(settings)
    if not vault:
        return "no-vault"
    for meta in ("⚙️ Meta", "Meta"):
        if (Path(vault) / meta / "scripts" / PREFLIGHT_BASENAME).is_file():
            return "ok"
    return "missing"


def diagnose(settings: dict, clone: Path) -> dict:
    """Pure. Returns the gap report; no side effects, no subprocess, no filesystem walk."""
    want = wanted_matchers(clone)
    have = healthy_matchers(settings)
    return {
        "missing_matchers": sorted(want - have),
        "want_matchers": sorted(want),
        "preflight": preflight_state(settings),
        "vault": resolve_vault(settings),
    }


def has_gap(report: dict) -> bool:
    return bool(report["missing_matchers"]) or report["preflight"] == "missing"


# --------------------------------------------------------------------------- repair
def _load_installer(clone: Path):
    """Import install-hooks-user-level.py (its filename is not importable by name)."""
    path = clone / "scripts" / "install-hooks-user-level.py"
    spec = importlib.util.spec_from_file_location("_abs_installer", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def repair_registration(settings_path: Path, clone: Path) -> bool:
    """Merge the canonical guard entries from the clone's hooks.json into settings.json,
    reusing install-hooks-user-level.py's own helpers so the write is byte-identical to a
    normal install. Returns True if a change was written. Fail-open: any error -> False."""
    regs = guard_registrations_from_template(clone)
    if not regs:
        return False
    # REFUSE on a settings.json that exists but does not parse. Merging into the {} that
    # _read_json returns for a corrupt file would write back ONLY our guard entries and
    # silently destroy the user's env / permissions / custom hooks. A guard that wrecks
    # the config it was meant to protect is worse than the gap it closes.
    if _settings_unreadable(settings_path):
        return False
    try:
        ihul = _load_installer(clone)
    except Exception:
        return False

    groups = []
    for matcher, cmd in regs:
        group = {"hooks": [{"type": "command", "command": cmd}]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
    minimal = {"hooks": {"PreToolUse": groups}}

    try:
        minimal = ihul.substitute_python_interpreter(minimal)
        if ihul._is_windows():
            minimal, _skipped = ihul.platformize_template_for_windows(minimal)
        existing = _read_json(settings_path) if settings_path.is_file() else {}
        merged, summary = ihul.merge_hooks(existing, minimal)
        if not summary.get("added") and not summary.get("updated"):
            return False  # already in sync
        backup = ihul.backup_settings(settings_path)
        return bool(ihul.write_settings_with_verify(settings_path, merged, backup))
    except Exception:
        return False


def _preflight_present(vault: str) -> bool:
    """True if journal-preflight.py is in the vault's meta scripts. Success is measured on
    the TARGET, never the subprocess exit code."""
    resolved = vault or resolve_vault(_read_json(Path.home() / ".claude" / "settings.json"))
    if not resolved:
        return False
    for meta in ("⚙️ Meta", "Meta"):
        if (Path(resolved) / meta / "scripts" / PREFLIGHT_BASENAME).is_file():
            return True
    return False


def repair_preflight(clone: Path, vault: str) -> bool:
    """Invoke the existing sync-vault-scripts.(sh|ps1) to land journal-preflight.py in the
    vault - REUSE, never reimplement. Returns True if the preflight is present afterward.
    Fail-open on any error (bash/pwsh absent, spawn failure)."""
    env = dict(os.environ)
    if vault:
        env["VAULT_ROOT"] = vault  # the sync self-resolves the vault from this too
    try:
        if os.name == "nt":
            # PowerShell param style (-Quiet / -Vault), NOT the bash --flags. Prefer PS7
            # (pwsh); fall back to Windows PowerShell 5.1 (powershell). The .ps1 is
            # compatible with both.
            import shutil
            script = clone / "scripts" / "sync-vault-scripts.ps1"
            launcher = next((x for x in ("pwsh", "powershell") if shutil.which(x)), None)
            if not script.is_file() or launcher is None:
                return _preflight_present(vault)
            cmd = [launcher, "-NoProfile", "-File", str(script), "-Quiet"]
            if vault:
                cmd += ["-Vault", vault]
        else:
            script = clone / "scripts" / "sync-vault-scripts.sh"
            if not script.is_file():
                return _preflight_present(vault)
            cmd = ["bash", str(script), "--quiet"]
            if vault:
                cmd += ["--vault", vault]
        subprocess.run(cmd, capture_output=True, timeout=90, env=env)
    except Exception:
        return _preflight_present(vault)
    return _preflight_present(vault)


# --------------------------------------------------------------------------- cooldown + log
def _cooldown_stamp() -> Path:
    return Path.home() / ".claude" / ".heal-journal-guard-last"


def _cooldown_active() -> bool:
    if os.environ.get("HEAL_JOURNAL_GUARD_NO_COOLDOWN") == "1":
        return False
    stamp = _cooldown_stamp()
    try:
        last = float(stamp.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    return (time.time() - last) < HEAL_COOLDOWN_SECONDS


def _stamp_cooldown() -> None:
    try:
        _cooldown_stamp().write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass


def _log(message: str) -> None:
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with (Path.home() / ".claude" / "heal-journal-guard.log").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- outputs
def _emit_silent() -> None:
    print(json.dumps({"continue": True, "suppressOutput": True}))


def _emit_context(message: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": message}}))


# --------------------------------------------------------------------------- session-start
def run_session_start() -> None:
    """The wired path. Diagnose the real account; repair gaps; surface ONE line. Exit 0."""
    if os.environ.get("HEAL_JOURNAL_GUARD_BYPASS") == "1":
        _emit_silent()
        return
    try:
        clone = clone_root()
        settings_path = Path.home() / ".claude" / "settings.json"
        settings = _read_json(settings_path)
        report = diagnose(settings, clone)
    except Exception:
        _emit_silent()
        return

    if not has_gap(report):
        _emit_silent()
        return

    # An unparseable settings.json makes EVERY hook look unregistered, so the gap above is
    # not evidence of a missing guard - it is evidence of a broken config. Only a human can
    # safely repair JSON we cannot read, so say exactly that and touch nothing.
    if _settings_unreadable(settings_path):
        _emit_context(
            "[heal-journal-guard] Cannot read ~/.claude/settings.json (it exists but is "
            "not valid JSON), so the /journal Step-0 context guard cannot be verified and "
            "NOTHING was changed. Fix the JSON (a recent backup is at "
            "~/.claude/settings.json.bak-*), then run: python3 "
            "~/.claude/skills/ai-brain-starter/scripts/install-hooks-user-level.py"
        )
        return

    # A real gap. Repair is subprocess-bearing, so gate it behind a cooldown: a box that
    # keeps failing to heal must not re-spawn the installer every session. On cooldown we
    # still surface the unprotected state so it stays visible.
    if _cooldown_active():
        _emit_context(
            "[heal-journal-guard] This account's /journal Step-0 context guard is "
            "incomplete (registration or vault preflight) and a recent auto-repair has "
            "not taken. Journaling could skip context. Run: python3 "
            "~/.claude/skills/ai-brain-starter/scripts/install-hooks-user-level.py"
        )
        return

    _stamp_cooldown()
    healed = []
    try:
        if report["missing_matchers"]:
            if repair_registration(settings_path, clone):
                healed.append("registration")
        if report["preflight"] == "missing":
            if repair_preflight(clone, report["vault"]):
                healed.append("preflight")
    except Exception:
        pass

    if healed:
        _log("healed " + "+".join(healed) + f" (had gap: {report})")
        _emit_context(
            "[heal-journal-guard] This account was missing part of the /journal "
            "Step-0 context guard (" + " + ".join(healed) + "); restored it so "
            "journaling cannot silently skip context. Your ai-brain-starter auto-update "
            "may be stale - a full re-sync would prevent it recurring."
        )
    else:
        # A gap we could not close (e.g. installer import failed). Fail-open but visible.
        _emit_context(
            "[heal-journal-guard] The /journal Step-0 context guard is incomplete on "
            "this account and auto-repair could not complete. Run: python3 "
            "~/.claude/skills/ai-brain-starter/scripts/install-hooks-user-level.py"
        )


# --------------------------------------------------------------------------- test modes
def cmd_check_only(args) -> int:
    """Read-only diagnosis of a given (or the real) settings.json. Exit 1 on any gap.
    Used by the fresh-install smoke test's negative control."""
    clone = Path(args.clone).resolve() if args.clone else clone_root()
    settings_path = (
        Path(args.settings).expanduser() if args.settings
        else Path.home() / ".claude" / "settings.json"
    )
    report = diagnose(_read_json(settings_path), clone)
    gap = has_gap(report)
    print(
        "GAP" if gap else "OK",
        "missing_matchers=" + ",".join(report["missing_matchers"] or ["-"]),
        "preflight=" + report["preflight"],
    )
    return 1 if gap else 0


def cmd_heal_now(args) -> int:
    """Force the repair against a given (or the real) settings.json. Exit 0 always.
    Used by the integration test to prove the repair closes the gap."""
    clone = Path(args.clone).resolve() if args.clone else clone_root()
    settings_path = (
        Path(args.settings).expanduser() if args.settings
        else Path.home() / ".claude" / "settings.json"
    )
    settings = _read_json(settings_path)
    report = diagnose(settings, clone)
    if report["missing_matchers"]:
        repair_registration(settings_path, clone)
    if report["preflight"] == "missing":
        repair_preflight(clone, args.vault or report["vault"])
    return 0


def self_test() -> int:
    """Hermetic pos/neg controls proving diagnose distinguishes healthy from unprotected
    and that the registration repair actually wires the guard. Exit 0 iff all pass."""
    import shutil
    import tempfile

    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    # Hermetic: the real account's VAULT_ROOT must not leak into the no-vault control
    # (self-test runs as its own short-lived process, so a plain pop is enough).
    os.environ.pop("VAULT_ROOT", None)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        clone = root / "clone"
        (clone / "hooks").mkdir(parents=True)
        (clone / "scripts").mkdir()
        # A real-enough guard script on disk so the "script exists" health check passes.
        guard = clone / "hooks" / GUARD_BASENAME
        guard.write_text("#!/usr/bin/env python3\nprint('{}')\n", encoding="utf-8")
        # The installer is needed by the repair path; copy the real one beside a hooks.json.
        real_installer = clone_root() / "scripts" / "install-hooks-user-level.py"
        if real_installer.is_file():
            shutil.copy(str(real_installer), str(clone / "scripts" / "install-hooks-user-level.py"))
        guard_cmd = (
            f"[PYTHON] {guard} 2>/dev/null || echo "
            "'{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\","
            "\"permissionDecision\":\"allow\"}}'"
        )
        (clone / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": guard_cmd}]},
            {"matcher": "Write|Edit|MultiEdit",
             "hooks": [{"type": "command", "command": guard_cmd}]},
        ]}}), encoding="utf-8")

        # want_matchers is driven by the template.
        check("wanted-both-matchers",
              wanted_matchers(clone) == {"Bash", "Write|Edit|MultiEdit"})

        # POSITIVE control: a settings.json already carrying the guard under both matchers
        # (pointing at the existing guard script) shows NO gap.
        healthy = {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {guard}"}]},
            {"matcher": "Write|Edit|MultiEdit",
             "hooks": [{"type": "command", "command": f"python3 {guard}"}]},
        ]}}
        check("positive-healthy-no-gap", not diagnose(healthy, clone)["missing_matchers"])

        # NEGATIVE control: empty settings.json is missing the guard under BOTH matchers.
        empty_path = root / "settings_empty.json"
        empty_path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        neg = diagnose(_read_json(empty_path), clone)
        check("negative-absent-detects-both",
              set(neg["missing_matchers"]) == {"Bash", "Write|Edit|MultiEdit"})

        # NEGATIVE control 2: registered but the referenced SCRIPT is missing -> still a gap.
        stale = {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command",
             "command": f"python3 {root}/does-not-exist/{GUARD_BASENAME}"}]},
        ]}}
        check("negative-stale-script-is-gap",
              "Bash" in diagnose(stale, clone)["missing_matchers"])

        # PLATFORM-SHAPE controls: the path parser must recognise every command form the
        # installers actually write. These assert on the EXTRACTED path rather than on
        # is_file(), so they are hermetic and give identical results on POSIX and Windows.
        # Regression guarded: a POSIX-only pattern silently reports a correctly-registered
        # Windows guard as missing, which nags forever and no install command can clear.
        bs = chr(92)
        win_guard = "C:" + bs + "U" + bs + "JUAN DOE" + bs + "hooks" + bs + GUARD_BASENAME
        win_runner = "C:" + bs + "U" + bs + "JUAN DOE" + bs + "scripts" + bs + "runner.py"
        forms = (
            # Windows installer form: TWO quoted paths, spaces in them, guard is the 2nd.
            ("win-quoted",
             'py -3 "' + win_runner + '" --fallback allow "' + win_guard + '"', win_guard),
            ("posix-tilde-bare",
             "python3 ~/.claude/hooks/" + GUARD_BASENAME + " 2>/dev/null",
             "~/.claude/hooks/" + GUARD_BASENAME),
            ("posix-single-quoted",
             "python3 '/home/u/hooks/" + GUARD_BASENAME + "'",
             "/home/u/hooks/" + GUARD_BASENAME),
            ("posix-spaces-quoted",
             'python3 "/home/juan doe/hooks/' + GUARD_BASENAME + '"',
             "/home/juan doe/hooks/" + GUARD_BASENAME),
        )
        for label, cmd, want in forms:
            m = _GUARD_PATH_RE.search(cmd)
            got = (m.group(1) or m.group(2) or m.group(3)) if m else None
            check("guard-path-" + label, got == want)
        # A RELATIVE path must NOT pass as absolute: the `/` inside `hooks/` is not a root.
        check("guard-path-rejects-relative",
              _GUARD_PATH_RE.search("python3 hooks/" + GUARD_BASENAME) is None)

        # Vault extraction from an installed vault-hook command: both separators, both
        # meta-folder spellings.
        for label, cmd, want in (
            ("win",
             'bash "C:' + bs + "v" + bs + "my vault" + bs + "⚙️ Meta" + bs + "scripts"
             + bs + 'write-hook.sh"',
             "C:" + bs + "v" + bs + "my vault"),
            ("posix", "bash '/home/u/v/Meta/scripts/write-hook.sh'", "/home/u/v"),
        ):
            m = _VAULT_FROM_CMD_RE.search(cmd)
            check("vault-from-cmd-" + label, (m.group(1) if m else None) == want)

        # REPAIR closes the gap: run the real registration repair against the empty
        # settings and re-diagnose. Proves the guard IS wired under both matchers.
        if real_installer.is_file():
            wrote = repair_registration(empty_path, clone)
            after = diagnose(_read_json(empty_path), clone)
            check("repair-wrote-something", wrote)
            check("repair-closes-registration-gap", not after["missing_matchers"])

        # DATA-LOSS control: a settings.json that EXISTS but is corrupt JSON must be left
        # BYTE-FOR-BYTE untouched. Merging into the {} a failed parse yields would write
        # back only our guard entries and destroy the user's env / permissions / hooks.
        corrupt_path = root / "settings_corrupt.json"
        corrupt_raw = '{"env":{"K":"V"},"hooks":{"Stop":[{"hooks":[{"command":"USER"}]}]}'
        corrupt_path.write_text(corrupt_raw, encoding="utf-8")
        check("corrupt-detected", _settings_unreadable(corrupt_path))
        check("corrupt-repair-refuses", repair_registration(corrupt_path, clone) is False)
        check("corrupt-file-untouched",
              corrupt_path.read_text(encoding="utf-8") == corrupt_raw)
        # ...while a genuinely ABSENT file is NOT treated as corrupt (repair must still run).
        check("absent-not-corrupt", not _settings_unreadable(root / "no-such-settings.json"))

        # PREFLIGHT diagnose: no vault -> 'no-vault'; vault with the file -> 'ok'; without
        # -> 'missing'. (The subprocess repair is exercised end-to-end in the .sh test.)
        check("preflight-no-vault", preflight_state({"hooks": {}}) == "no-vault")
        vault = root / "vault"
        (vault / "⚙️ Meta" / "scripts").mkdir(parents=True)
        os.environ["VAULT_ROOT"] = str(vault)
        try:
            check("preflight-missing", preflight_state({"hooks": {}}) == "missing")
            (vault / "⚙️ Meta" / "scripts" / PREFLIGHT_BASENAME).write_text(
                "x", encoding="utf-8")
            check("preflight-ok", preflight_state({"hooks": {}}) == "ok")
        finally:
            os.environ.pop("VAULT_ROOT", None)

    if fails:
        print("self-test FAIL: " + ", ".join(fails))
        return 1
    print("self-test OK: registration + preflight diagnose/repair controls pass")
    return 0


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true", help="run hermetic pos/neg controls")
    ap.add_argument("--check-only", action="store_true",
                    help="read-only diagnosis; exit 1 on any gap")
    ap.add_argument("--heal-now", action="store_true",
                    help="force the repair against --settings (test helper)")
    ap.add_argument("--settings", help="settings.json path (default ~/.claude/settings.json)")
    ap.add_argument("--clone", help="ai-brain-starter clone root (default: this checkout)")
    ap.add_argument("--vault", help="vault root for the preflight repair")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.check_only:
        return cmd_check_only(args)
    if args.heal_now:
        return cmd_heal_now(args)

    # Default: the wired SessionStart path. Belt-and-suspenders fail-open.
    try:
        run_session_start()
    except Exception:
        try:
            _emit_silent()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313): force UTF-8 so a non-ASCII
    # print (a surfaced line, a path with the gear-Meta emoji) can't crash the hook.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
