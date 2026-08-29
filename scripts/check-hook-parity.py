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
basename, arguments) sets. Any difference not present in
scripts/hook-parity-allowlist.json FAILS. Every allowlist entry must carry a
non-empty reason.

WHY ARGUMENTS ARE PART OF THE IDENTITY. A hook is not a script; it is a script
plus what it is invoked with. hooks.json wires lint-claude-settings.py TWICE on
SessionStart - once plain, once `--test` for the self-test that asserts its
guards still bite - and until #504 the Windows rewrite discarded everything after
the script path, so both became the same argument-less command and the
installer's dedup collapsed the pair. The linter ran; its negative control
silently did not, on every Windows install, for as long as the rewrite existed.
Keyed on (event, basename) alone that is invisible: a SET puts both entries on
one element, so both legs agree and this gate reports parity whether the --test
registration survived or not. With arguments in the key the POSIX leg has two
elements and the Windows leg one, and the difference is named for what it is - a
registration present on one platform and absent on the other.

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
# exit-contract: ENFORCING


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
    # Deliberately not in REQUIRED_ATTRS: this one is NEWER than the gate's other
    # needs, so a base predating it deserves a message that says so rather than
    # the misleading "no longer exports".
    if not hasattr(mod, "_hook_script_args"):
        raise Unevaluated(
            f"{INSTALLER.name} does not export _hook_script_args (added in #504). "
            "This gate compares hook ARGUMENTS, not just script names, so it "
            "cannot run against a base that predates it - rebase onto main.")
    return mod


def _pairs(template: dict, drop_basenames: set[str],
           hook_args) -> set[tuple[str, str, tuple[str, ...]]]:
    """{(event, hook basename, arguments)} wired by this template.

    Per EVENT, not a flat basename set: the same hook wired on SessionStart on
    one platform and on Stop on the other is a real behavioural difference, and
    a flat set would call it parity.

    With ARGUMENTS, not the basename alone: one script wired twice on one event
    under different arguments is two registrations, and a basename-keyed set
    collapses them onto a single element that both legs then always agree on
    (see WHY ARGUMENTS ARE PART OF THE IDENTITY above).

    `hook_args` is the installer's OWN extractor, passed in for the same reason
    the rest of this gate imports rather than reimplements: a private copy of the
    argument rule would agree on the day it was written and drift silently after.

    Arguments belong to the COMMAND, so every basename a command names carries
    that command's arguments. A fallback chain naming one basename twice still
    collapses to one element, exactly as before."""
    out: set[tuple[str, str, tuple[str, ...]]] = set()
    for event, blocks in (template.get("hooks") or {}).items():
        for block in blocks or []:
            for hook in (block.get("hooks") or []):
                command = hook.get("command", "") or ""
                args = tuple(hook_args(command))
                for match in _SCRIPT_RE.findall(command):
                    basename = os.path.basename(match)
                    if basename in drop_basenames:
                        continue
                    out.add((event, basename, args))
    return out


def wired_sets(mod, hooks_json: Path) -> tuple[
        set[tuple[str, str, tuple[str, ...]]],
        set[tuple[str, str, tuple[str, ...]]], list[str]]:
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
            return _pairs(template, launcher_only, mod._hook_script_args), list(skipped)
        return _pairs(template, set(), mod._hook_script_args), []

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


def compare(posix: set[tuple], windows: set[tuple],
            allow_posix: dict[tuple[str, str], str],
            allow_windows: dict[tuple[str, str], str]) -> dict:
    """Pure verdict. Separated from the installer plumbing so --self-test can
    drive it with synthetic sets and prove it BITES."""
    posix_only = posix - windows
    windows_only = windows - posix

    unreviewed, reasonless, stale = [], [], []
    # Allowlist keys stay (event, hook): ONE reviewed decision covers every
    # argument variant of that hook on that event. So adding arguments to the
    # comparison invalidates not a single committed allowlist entry - the
    # reviewed-asymmetry file keeps its exact meaning, and only the DIFF gets
    # sharper. (`pair[:2]` also lets --self-test keep driving compare() with
    # plain (event, hook) fixtures, which is why those controls still read the
    # same below.)
    for pair in sorted(posix_only):
        if pair[:2] not in allow_posix:
            unreviewed.append(("posix_only", pair))
        elif not allow_posix[pair[:2]]:
            reasonless.append(("posix_only", pair))
    for pair in sorted(windows_only):
        if pair[:2] not in allow_windows:
            unreviewed.append(("windows_only", pair))
        elif not allow_windows[pair[:2]]:
            reasonless.append(("windows_only", pair))
    # A stale entry is a fixed drop whose excuse survived. Left alone, the
    # allowlist decays into a list nobody re-reads, which is how a reviewed
    # decision quietly becomes an unreviewed one again.
    posix_only_keys = {p[:2] for p in posix_only}
    windows_only_keys = {p[:2] for p in windows_only}
    for pair in sorted(allow_posix):
        if pair not in posix_only_keys:
            stale.append(("posix_only", pair))
    for pair in sorted(allow_windows):
        if pair not in windows_only_keys:
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

    # ---- arguments are part of the identity (the #504 class) ----------------
    # One script, one event, wired twice under DIFFERENT arguments - the shape
    # hooks.json actually ships for lint-claude-settings.py. The Windows leg
    # keeps only the argument-less registration, which is precisely what the
    # pre-#504 rewrite produced.
    two_variants = {("SessionStart", "lint.py", ()),
                    ("SessionStart", "lint.py", ("--test",))}
    collapsed = {("SessionStart", "lint.py", ())}

    # PREMISE, asserted rather than asserted-in-a-comment: with arguments
    # stripped these two fixtures are the SAME set. So the old (event, basename)
    # key could not have caught this, and the control below is not a duplicate
    # of the drop controls above. If this ever stops holding, the fixture has
    # drifted into testing something easier than the bug it stands for.
    if {q[:2] for q in two_variants} != {q[:2] for q in collapsed}:
        fails.append("arg-collapse fixture is distinguishable WITHOUT arguments; "
                     "it no longer exercises the #504 class")

    check("same script+event, one ARGUMENT variant dropped on Windows",
          compare(two_variants, collapsed, {}, {}), False)
    check("both argument variants survive on both platforms",
          compare(two_variants, set(two_variants), {}, {}), True)
    check("an argument variant dropped, allowlisted by (event, hook)",
          compare(two_variants, collapsed,
                  {("SessionStart", "lint.py"): "no Windows port for the self-test"},
                  {}), True)
    check("an argument variant dropped, allowlisted with an EMPTY reason",
          compare(two_variants, collapsed, {("SessionStart", "lint.py"): ""}, {}), False)
    # A Windows-only ARGUMENT is a difference in the other direction: the same
    # script gaining a flag no POSIX install passes is not parity either.
    check("Windows-only argument variant, NOT allowlisted",
          compare(collapsed, two_variants, {}, {}), False)
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
        if ("Stop", "synthetic-drop.sh", ()) not in result["posix_only"]:
            fails.append("end-to-end: a bash-only Stop hook was NOT reported as "
                         f"dropped on Windows (posix_only={result['posix_only']}). "
                         "The gate would not see a real drop either.")
        if (("Stop", "survivor.py", ()) not in posix
                or ("Stop", "survivor.py", ()) not in windows):
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
          "a reasonless allowlist entry, a wrong-event entry, a per-event move, a "
          "stale entry, and a dropped ARGUMENT variant of a script that survives "
          "under its other arguments; and it still detects a real bash-hook drop "
          "end-to-end.")
    return 0


def _fmt(pair) -> str:
    """'SessionStart: lint-claude-settings.py --test'. The arguments are part of
    the identity, so they have to be part of what a failure NAMES - otherwise the
    report says one hook differs and shows two lines that look identical."""
    args = pair[2] if len(pair) > 2 else ()
    return f"{pair[0]}: {pair[1]}" + (" " + " ".join(args) if args else "")


def _jpair(pair) -> list:
    """(event, hook, args) as JSON. Tolerates a 2-tuple allowlist key."""
    return [pair[0], pair[1], list(pair[2]) if len(pair) > 2 else []]


def _print_report(result: dict, allow_posix: dict, allow_windows: dict) -> None:
    print(f"POSIX wires {result['posix_hooks']} (event, hook, args) "
          f"registration(s); Windows wires {result['windows_hooks']}.")
    if result["posix_only"]:
        print("\nDropped on Windows (wired on POSIX only):")
        for pair in result["posix_only"]:
            reason = allow_posix.get(pair[:2])
            tag = "reviewed" if reason else "UNREVIEWED"
            print(f"  [{tag}] {_fmt(pair)}")
            if reason:
                print(f"            {reason}")
    if result["windows_only"]:
        print("\nWired on Windows only:")
        for pair in result["windows_only"]:
            reason = allow_windows.get(pair[:2])
            tag = "reviewed" if reason else "UNREVIEWED"
            print(f"  [{tag}] {_fmt(pair)}")
            if reason:
                print(f"            {reason}")

    if result["unreviewed"]:
        print(f"\nFAIL: {len(result['unreviewed'])} platform difference(s) are not in "
              f"{ALLOWLIST.name}:")
        for side, pair in result["unreviewed"]:
            print(f"  {side}  {_fmt(pair)}")
        print("\nA hook wired on one platform and not the other is a FEATURE that\n"
              "only some users have. Either wire it on both, or add it to\n"
              f"{ALLOWLIST.name} with a reason and the consequence the users on\n"
              "the losing platform will live with. The allowlist is the review.")
    if result["reasonless"]:
        print(f"\nFAIL: {len(result['reasonless'])} allowlist entry/entries carry no reason:")
        for side, pair in result["reasonless"]:
            print(f"  {side}  {_fmt(pair)}")
        print("A bare entry records that someone noticed, not that anyone decided.")
    if result["stale"]:
        print(f"\nFAIL: {len(result['stale'])} allowlist entry/entries no longer "
              f"describe a real difference:")
        for side, pair in result["stale"]:
            print(f"  {side}  {_fmt(pair)}")
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
            "posix_only": [_jpair(p) for p in result["posix_only"]],
            "windows_only": [_jpair(p) for p in result["windows_only"]],
            "unreviewed": [[s] + _jpair(p) for s, p in result["unreviewed"]],
            "reasonless": [[s] + _jpair(p) for s, p in result["reasonless"]],
            "stale": [[s] + _jpair(p) for s, p in result["stale"]],
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
