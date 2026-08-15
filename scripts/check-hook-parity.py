#!/usr/bin/env python3
"""check-hook-parity.py - a hook dropped on one platform must be a REVIEWED
decision, never a silent one.

THE BUG CLASS. hooks.json is written in POSIX shell. On native Windows the
installer rewrites every command through hook_runner.py
(platformize_template_for_windows), and any command it cannot rewrite - anything
that runs a `bash` script - is DROPPED. Not warned about at install time in a way
anyone reads, not recorded, not compared. The Windows user simply never has that
feature, and every check still passes:

  * the installer exits 0 (it installed what it could)
  * `--verify` passes (it verifies what is WIRED, and the dropped hook is not)
  * check-hook-activation.py passes (it reads hooks.json, the POSIX source)
  * CI passes (the Windows job asserts the install SUCCEEDS)

So a Windows job and a macOS job can differ by three whole features and both
stay green. Today that is exactly what happens: four bash hooks are dropped, and
three of them - write-hook.sh, session-end-hook.sh, graph-context-hook.sh - have
NO Windows equivalent at all. Meeting-note extraction, the entire session-close
cascade, and graph-context routing are POSIX-only, and nothing in the repo said
so out loud until this file existed.

WHAT THIS DOES. Runs the installer's OWN pipeline twice over hooks.json - once
as POSIX, once forced to the Windows leg - and diffs the resulting (event, hook
basename) sets. Any difference not present in scripts/hook-parity-allowlist.json
FAILS. Every allowlist entry must carry a non-empty reason.

Why the installer's own functions and not a reimplementation: a private copy of
the drop rule would agree with the installer on the day it was written and
diverge silently after. This gate imports platformize_template_for_windows and
asks it what it actually does. If the installer stops exporting what this needs,
the gate exits 2 (UNEVALUATED) rather than passing on an empty comparison - the
same rule scripts/audit-guard-activation-roots.py applies.

WHAT THE ALLOWLIST IS FOR. It is not an exception mechanism; it IS the point.
Seeded green with today's four drops, each carrying a written reason and its
user-visible consequence. The gate converts "a bash hook silently vanished on
Windows" into "someone had to add a line to a committed file saying which
platform loses which feature, and why". A new drop cannot land unreviewed; a
drop that gets FIXED must have its entry removed (a stale entry also fails), so
the file cannot rot into a list of excuses nobody re-reads.

WHAT THIS DELIBERATELY DOES NOT DO. It does not require the sets to be equal and
it does not try to port anything. Genuine platform asymmetry is legitimate; an
UNRECORDED asymmetry is not.

Usage:
    python3 scripts/check-hook-parity.py            # the gate
    python3 scripts/check-hook-parity.py --json     # machine-readable
    python3 scripts/check-hook-parity.py --self-test  # negative controls

Exit codes:
    0  the POSIX and Windows wired sets differ only where the allowlist says so
    1  an unreviewed difference, a stale allowlist entry, or a reasonless entry
    2  UNEVALUATED - the installer/hooks.json/allowlist could not be loaded, or
       a leg produced no hooks at all. Never 0: nothing compared is not parity.

Provenance: MYC-3879. Sibling gate: scripts/check-hook-activation.py (which
asks whether a hook is wired AT ALL; this asks whether it is wired on BOTH
platforms).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "scripts" / "install-hooks-user-level.py"
HOOKS_JSON = REPO / "hooks.json"
ALLOWLIST = REPO / "scripts" / "hook-parity-allowlist.json"

# Hermetic pins for the installer's environment probes. Without these the two
# legs would depend on what happens to be on PATH, so the same tree could pass
# on one machine and fail on another.
FAKE_VAULT = "/abs-hook-parity-vault"
FAKE_POSIX_PYTHON = "/usr/bin/python3"
FAKE_WIN_LAUNCHER = "py -3"

# Every function this gate calls on the installer. Named up front so a rename
# there produces "UNEVALUATED: installer no longer exports X" instead of an
# AttributeError traceback or, worse, a skipped comparison that reads as pass.
REQUIRED_ATTRS = (
    "load_hooks_template",
    "normalize_path_substitutions",
    "substitute_python_interpreter",
    "platformize_template_for_windows",
    "_is_windows",
)

_SCRIPT_RE = re.compile(r"([\w.\-]+\.(?:py|sh|bash))")


class Unevaluated(Exception):
    """The comparison could not be performed. Distinct from "no differences"."""


def _load_installer():
    if not INSTALLER.is_file():
        raise Unevaluated(f"installer not found: {INSTALLER}")
    spec = importlib.util.spec_from_file_location("abs_installer_for_parity", INSTALLER)
    if spec is None or spec.loader is None:
        raise Unevaluated(f"could not build an import spec for {INSTALLER}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001 - any import failure is UNEVALUATED
        raise Unevaluated(f"importing {INSTALLER.name} failed: {e}") from e
    missing = [a for a in REQUIRED_ATTRS if not hasattr(mod, a)]
    if missing:
        raise Unevaluated(
            f"{INSTALLER.name} no longer exports: {', '.join(missing)}. This gate "
            "compares the installer's REAL behaviour, so it cannot run against a "
            "renamed pipeline - re-point it rather than deleting it.")
    return mod


def _pairs(template: dict, drop_basenames: set[str]) -> set[tuple[str, str]]:
    """{(event, hook basename)} wired by this template.

    Per EVENT, not a flat basename set: the same hook wired on SessionStart on
    one platform and on Stop on the other is a real behavioural difference, and
    a flat set would call it parity."""
    out: set[tuple[str, str]] = set()
    for event, blocks in (template.get("hooks") or {}).items():
        for block in blocks or []:
            for hook in (block.get("hooks") or []):
                for match in _SCRIPT_RE.findall(hook.get("command", "") or ""):
                    basename = os.path.basename(match)
                    if basename in drop_basenames:
                        continue
                    out.add((event, basename))
    return out


def wired_sets(mod, hooks_json: Path) -> tuple[set[tuple[str, str]], set[tuple[str, str]], list[str]]:
    """(posix_pairs, windows_pairs, installer-reported skips).

    Runs the installer's real pipeline in both directions. `_is_windows` is
    patched rather than driven through ABS_FORCE_WINDOWS because this process
    must produce BOTH legs, and on a native-Windows checkout os.name=='nt'
    makes the POSIX leg otherwise unreachable."""
    if not hooks_json.is_file():
        raise Unevaluated(f"hooks template not found: {hooks_json}")

    runner = str(REPO / "scripts" / "hook_runner.py")
    pins = {"ABS_POSIX_PYTHON": FAKE_POSIX_PYTHON,
            "ABS_WIN_LAUNCHER": FAKE_WIN_LAUNCHER,
            "ABS_HOOK_RUNNER": runner}
    saved = {k: os.environ.get(k) for k in pins}
    os.environ.update(pins)
    # hook_runner.py is the Windows LAUNCHER shim, not a hook: every rewritten
    # command names it, so leaving it in would report it as a Windows-only
    # "extra" on every event. Derived from the installer's own runner path so a
    # rename follows automatically instead of silently re-appearing as a diff.
    launcher_only = {Path(runner).name}

    original_is_windows = mod._is_windows

    def leg(force_windows: bool):
        mod._is_windows = (lambda: True) if force_windows else (lambda: False)
        template = mod.load_hooks_template(hooks_json)
        template = mod.normalize_path_substitutions(template, FAKE_VAULT)
        template = mod.substitute_python_interpreter(template)
        if force_windows:
            template, skipped = mod.platformize_template_for_windows(template)
            return _pairs(template, launcher_only), list(skipped)
        return _pairs(template, set()), []

    try:
        posix, _ = leg(False)
        windows, skipped = leg(True)
    finally:
        # Leave the module and the environment exactly as found: this function
        # is called twice by --self-test, and a leaked pin would make the second
        # call depend on the first.
        mod._is_windows = original_is_windows
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # PREMISE. An empty leg means the pipeline broke, not that the platforms
    # agree - and an empty-vs-empty comparison would otherwise pass silently.
    if not posix:
        raise Unevaluated("the POSIX leg wired ZERO hooks; the template or the "
                          "installer pipeline is broken, so nothing was compared")
    if not windows:
        raise Unevaluated("the Windows leg wired ZERO hooks; platformize dropped "
                          "everything (no launcher resolved?), so nothing was compared")
    return posix, windows, skipped


def load_allowlist(path: Path) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    """(posix_only, windows_only) -> {(event, hook): reason}. Raises Unevaluated
    if the file is missing or malformed: an unreadable allowlist must never be
    treated as an empty one, because an empty one makes the gate MORE strict and
    would look like it is working."""
    if not path.is_file():
        raise Unevaluated(f"allowlist not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Unevaluated(f"allowlist unreadable/invalid JSON: {e}") from e

    def section(key: str) -> dict[tuple[str, str], str]:
        out: dict[tuple[str, str], str] = {}
        for entry in (data.get(key) or []):
            if not isinstance(entry, dict):
                raise Unevaluated(f"allowlist {key}: entries must be objects, got {entry!r}")
            event, hook = entry.get("event"), entry.get("hook")
            if not event or not hook:
                raise Unevaluated(f"allowlist {key}: entry missing event/hook: {entry!r}")
            out[(event, hook)] = (entry.get("reason") or "").strip()
        return out

    return section("posix_only"), section("windows_only")


def compare(posix: set[tuple[str, str]], windows: set[tuple[str, str]],
            allow_posix: dict[tuple[str, str], str],
            allow_windows: dict[tuple[str, str], str]) -> dict:
    """Pure verdict. Separated from the installer plumbing so --self-test can
    drive it with synthetic sets and prove it BITES."""
    posix_only = posix - windows
    windows_only = windows - posix

    unreviewed, reasonless, stale = [], [], []
    for pair in sorted(posix_only):
        if pair not in allow_posix:
            unreviewed.append(("posix_only", pair))
        elif not allow_posix[pair]:
            reasonless.append(("posix_only", pair))
    for pair in sorted(windows_only):
        if pair not in allow_windows:
            unreviewed.append(("windows_only", pair))
        elif not allow_windows[pair]:
            reasonless.append(("windows_only", pair))
    # A stale entry is a fixed drop whose excuse survived. Left alone, the
    # allowlist decays into a list nobody re-reads, which is how a reviewed
    # decision quietly becomes an unreviewed one again.
    for pair in sorted(allow_posix):
        if pair not in posix_only:
            stale.append(("posix_only", pair))
    for pair in sorted(allow_windows):
        if pair not in windows_only:
            stale.append(("windows_only", pair))

    return {
        "posix_hooks": len(posix),
        "windows_hooks": len(windows),
        "posix_only": sorted(posix_only),
        "windows_only": sorted(windows_only),
        "unreviewed": unreviewed,
        "reasonless": reasonless,
        "stale": stale,
        "ok": not (unreviewed or reasonless or stale),
    }


# ---- self-test (negative controls: the gate must BITE) ------------------------
def _selftest_compare_cases() -> list[str]:
    fails: list[str] = []
    base = {("Stop", "a.py"), ("PreToolUse", "b.py")}

    def check(label: str, result: dict, want_ok: bool) -> None:
        if result["ok"] != want_ok:
            fails.append(f"{label}: wanted {'PASS' if want_ok else 'BITE'}, got "
                         f"{'PASS' if result['ok'] else 'BITE'} "
                         f"(unreviewed={result['unreviewed']}, "
                         f"reasonless={result['reasonless']}, stale={result['stale']})")

    check("identical sets, empty allowlist",
          compare(base, set(base), {}, {}), True)

    dropped = {("Stop", "a.py")}
    check("hook dropped on Windows, NOT allowlisted",
          compare(base, dropped, {}, {}), False)
    check("hook dropped on Windows, allowlisted WITH a reason",
          compare(base, dropped, {("PreToolUse", "b.py"): "bash-only, no port"}, {}), True)
    check("hook dropped on Windows, allowlisted with an EMPTY reason",
          compare(base, dropped, {("PreToolUse", "b.py"): ""}, {}), False)
    check("hook dropped on Windows, allowlisted under the WRONG event",
          compare(base, dropped, {("Stop", "b.py"): "wrong event"}, {}), False)

    extra = set(base) | {("SessionStart", "win-only.py")}
    check("Windows-only extra, NOT allowlisted",
          compare(base, extra, {}, {}), False)
    check("Windows-only extra, allowlisted WITH a reason",
          compare(base, extra, {}, {("SessionStart", "win-only.py"): "windows shim"}), True)

    # Same basename, different event: a flat basename comparison would call
    # this parity. It is not.
    moved = {("Stop", "a.py"), ("SessionStart", "b.py")}
    check("same hook wired on a DIFFERENT event per platform",
          compare(base, moved, {}, {}), False)

    check("stale allowlist entry (the drop was fixed, the excuse stayed)",
          compare(base, set(base), {("PreToolUse", "b.py"): "obsolete"}, {}), False)
    return fails


def _selftest_endtoend(mod) -> list[str]:
    """Prove the EXTRACTION bites too, not just the set math: a synthetic
    hooks.json whose only Stop hook is a bash script must come out as a
    posix_only difference after the real installer pipeline runs."""
    import tempfile

    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        j = Path(td) / "hooks.json"
        j.write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [
                {"type": "command", "command": "bash '[VAULT_PATH]/synthetic-drop.sh'"},
                {"type": "command", "command": "[PYTHON] ~/.claude/hooks/survivor.py"},
            ]}],
        }}), encoding="utf-8")
        try:
            posix, windows, _ = wired_sets(mod, j)
        except Unevaluated as e:
            return [f"end-to-end fixture could not be evaluated: {e}"]
        result = compare(posix, windows, {}, {})
        if ("Stop", "synthetic-drop.sh") not in result["posix_only"]:
            fails.append("end-to-end: a bash-only Stop hook was NOT reported as "
                         f"dropped on Windows (posix_only={result['posix_only']}). "
                         "The gate would not see a real drop either.")
        if ("Stop", "survivor.py") not in posix or ("Stop", "survivor.py") not in windows:
            fails.append("end-to-end: the .py hook that SHOULD survive both legs "
                         f"did not (posix={sorted(posix)}, windows={sorted(windows)}). "
                         "A gate that drops everything would pass this file vacuously.")
        if result["ok"]:
            fails.append("end-to-end: an unallowlisted synthetic drop was reported OK")
    return fails


def cmd_selftest() -> int:
    fails = _selftest_compare_cases()
    try:
        mod = _load_installer()
    except Unevaluated as e:
        print(f"SELF-TEST UNEVALUATED: {e}", file=sys.stderr)
        return 2
    fails += _selftest_endtoend(mod)
    if fails:
        print("SELF-TEST FAIL:")
        for f in fails:
            print("  - " + f)
        return 1
    print("SELF-TEST PASS: the gate bites an unreviewed drop, a Windows-only extra, "
          "a reasonless allowlist entry, a wrong-event entry, a per-event move and a "
          "stale entry; and it still detects a real bash-hook drop end-to-end.")
    return 0


def _print_report(result: dict, allow_posix: dict, allow_windows: dict) -> None:
    print(f"POSIX wires {result['posix_hooks']} (event, hook) pair(s); "
          f"Windows wires {result['windows_hooks']}.")
    if result["posix_only"]:
        print("\nDropped on Windows (wired on POSIX only):")
        for event, hook in result["posix_only"]:
            reason = allow_posix.get((event, hook))
            tag = "reviewed" if reason else "UNREVIEWED"
            print(f"  [{tag}] {event}: {hook}")
            if reason:
                print(f"            {reason}")
    if result["windows_only"]:
        print("\nWired on Windows only:")
        for event, hook in result["windows_only"]:
            reason = allow_windows.get((event, hook))
            tag = "reviewed" if reason else "UNREVIEWED"
            print(f"  [{tag}] {event}: {hook}")
            if reason:
                print(f"            {reason}")

    if result["unreviewed"]:
        print(f"\nFAIL: {len(result['unreviewed'])} platform difference(s) are not in "
              f"{ALLOWLIST.name}:")
        for side, (event, hook) in result["unreviewed"]:
            print(f"  {side}  {event}: {hook}")
        print("\nA hook wired on one platform and not the other is a FEATURE that\n"
              "only some users have. Either wire it on both, or add it to\n"
              f"{ALLOWLIST.name} with a reason and the consequence the users on\n"
              "the losing platform will live with. The allowlist is the review.")
    if result["reasonless"]:
        print(f"\nFAIL: {len(result['reasonless'])} allowlist entry/entries carry no reason:")
        for side, (event, hook) in result["reasonless"]:
            print(f"  {side}  {event}: {hook}")
        print("A bare entry records that someone noticed, not that anyone decided.")
    if result["stale"]:
        print(f"\nFAIL: {len(result['stale'])} allowlist entry/entries no longer "
              f"describe a real difference:")
        for side, (event, hook) in result["stale"]:
            print(f"  {side}  {event}: {hook}")
        print("The platforms now agree here. Delete the entry - a list of excuses\n"
              "for problems that were fixed is how the next real drop hides.")
    if result["ok"]:
        print("\nEvery POSIX/Windows difference is reviewed and current. OK.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                    help="negative controls: prove the gate still bites")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--hooks-json", default=str(HOOKS_JSON))
    ap.add_argument("--allowlist", default=str(ALLOWLIST))
    a = ap.parse_args()

    if a.self_test:
        return cmd_selftest()

    try:
        mod = _load_installer()
        allow_posix, allow_windows = load_allowlist(Path(a.allowlist))
        posix, windows, skipped = wired_sets(mod, Path(a.hooks_json))
    except Unevaluated as e:
        msg = (f"UNEVALUATED: {e}\nNothing was compared - that is not the same as "
               f"the platforms matching.")
        if a.json:
            print(json.dumps({"verdict": "UNEVALUATED", "error": str(e)}, indent=2))
        else:
            print(msg, file=sys.stderr)
        return 2

    result = compare(posix, windows, allow_posix, allow_windows)
    if a.json:
        print(json.dumps({
            "verdict": "PASS" if result["ok"] else "FAIL",
            "posix_hooks": result["posix_hooks"],
            "windows_hooks": result["windows_hooks"],
            "posix_only": [list(p) for p in result["posix_only"]],
            "windows_only": [list(p) for p in result["windows_only"]],
            "unreviewed": [[s, e, h] for s, (e, h) in result["unreviewed"]],
            "reasonless": [[s, e, h] for s, (e, h) in result["reasonless"]],
            "stale": [[s, e, h] for s, (e, h) in result["stale"]],
            "installer_reported_skips": skipped,
        }, indent=2))
        return 0 if result["ok"] else 1

    _print_report(result, allow_posix, allow_windows)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII hook path
    # in a report cannot crash the gate before it prints its finding.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
