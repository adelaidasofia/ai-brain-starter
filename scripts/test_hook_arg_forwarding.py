#!/usr/bin/env python3
"""
test_hook_arg_forwarding.py — a hook's ARGUMENTS must survive the install.

Run: python3 scripts/test_hook_arg_forwarding.py
No pytest dependency. Exits non-zero on any failure. Writes nothing, reads no
settings.json, never shells out to an installer.

THE BUG. platformize_template_for_windows() rebuilt every hook command from the
LAST .py path it found and nothing else:

    " ".join([*launcher, f'"{runner}"', "--fallback", fb, f'"{target}"'])

so anything the POSIX command passed AFTER the script was discarded. hooks.json
wires lint-claude-settings.py twice on SessionStart — once plain, once `--test`
for the self-test that asserts its guards still bite. Both rewrote to the SAME
argument-less command, dedup collapsed the pair, and Windows installs kept one:
the linter ran, its negative control silently did not. hook_runner.py was never
the limitation — its argv contract is `[--fallback MODE] <target> [extra...]`
and it forwards `extra` — the installer simply never passed anything.

The same collapse reached POSIX by a second route: is_same_command() and
dedupe_owned_hooks() keyed on the script BASENAME, so the pair read as
duplicates there too, and merge_hooks() (which REPLACES on a match) kept only
the `--test` one — the mirror image of the Windows outcome. Both halves are
locked below.

WHAT MAKES THIS HARD, and what these asserts are really about: the tail of a
template command is mostly NOT arguments. Every entry carries POSIX masking
noise — `2>/dev/null`, `|| true`, `|| echo '{...}'`, a trailing `; else ...; fi`
— and forwarding any of that to a hook as argv is worse than dropping `--test`.
So the extraction has to keep real arguments and stop at shell syntax, and the
table in test_arg_extraction() is that boundary, shape by shape.

Same silent-no-op family as the MYC-3875/3876/3879/3880 guards: the install
reports success and one thing quietly never runs.

Auto-discovered by scripts/ci.sh via the scripts/test_*.py glob.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Fixed, fake, and never touched on disk: _runner_path() prefers this over any
# installed copy, so the generated commands are identical on every machine.
os.environ["ABS_HOOK_RUNNER"] = "/abs-test/hook_runner.py"

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
    path = ROOT / "scripts" / "install-hooks-user-level.py"
    spec = importlib.util.spec_from_file_location("_abs_argfwd_test", str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._windows_launcher = lambda: ["py", "-3"]  # type: ignore[assignment]
    return mod


def windows_template(ins, template: dict) -> dict:
    """The template as a Windows install writes it (platformize is POSIX-safe)."""
    out, _skipped = ins.platformize_template_for_windows(template)
    return out


def resolved_template(ins, *, windows: bool) -> dict:
    """The REAL hooks.json, through the real pipeline, for one platform.

    Deliberately not a fixture: if someone adds a second argument-bearing hook,
    or drops `--test`, these asserts must see it."""
    t = json.loads((ROOT / "hooks.json").read_text(encoding="utf-8"))
    t = ins.normalize_path_substitutions(t, str(Path.home() / "vault"))
    t = ins.substitute_python_interpreter(t)
    return windows_template(ins, t) if windows else t


def installed(ins, template: dict, existing: dict | None = None) -> dict:
    """Merge + both dedup passes — every step that can drop an entry."""
    merged, _ = ins.merge_hooks(existing if existing is not None else {}, template)
    merged, _ = ins.dedupe_owned_hooks(merged)
    merged, _ = ins.dedupe_identical_hooks(merged)
    return merged


def lint_entries(settings: dict) -> list[str]:
    return [h.get("command", "")
            for g in (settings.get("hooks") or {}).get("SessionStart", [])
            for h in g.get("hooks", [])
            if "lint-claude-settings.py" in h.get("command", "")]


def drop_args(cmd: str) -> str:
    """A Windows command as the PRE-FIX installer wrote it: everything past the
    quoted target dropped. The target is the last QUOTED token, so truncating at
    the final double quote reproduces the old output exactly."""
    end = cmd.rfind('"')
    return cmd[:end + 1] if end != -1 else cmd


# --------------------------------------------------------------------------
# 1. Real arguments are kept; POSIX masking noise is not.
# --------------------------------------------------------------------------
def test_arg_extraction(ins) -> None:
    cases = [
        # (command, expected args)  — every shape hooks.json actually ships
        ("python3 ~/x.py 2>/dev/null || echo '{\"continue\":true}'", []),
        ("[ -f ~/x.py ] && python3 ~/x.py || true", []),
        ("[ -f ~/x.py ] && python3 ~/x.py || echo '{\"a\":1}'", []),
        ("if [ -f ~/x.py ]; then python3 ~/x.py; else echo '{\"a\":1}'; fi", []),
        ("python3 '/v/a.py' 2>/dev/null || python3 ~/.claude/a.py 2>/dev/null "
         "|| echo '{}'", []),
        # ...and the ones that DO carry arguments
        ("[ -f ~/x.py ] && python3 ~/x.py --test || true", ["--test"]),
        ("python3 '/v/a.py' --mode fast 2>/dev/null || echo '{}'", ["--mode", "fast"]),
        ("python3 ~/x.py --path 'a b' || true", ["--path", "a b"]),
        # a Windows-form command must read back the same way it was written
        ('py -3 "C:\\R\\hook_runner.py" --fallback silent "C:\\U\\x.py"', []),
        ('py -3 "C:\\R\\hook_runner.py" --fallback silent "C:\\U\\x.py" --test',
         ["--test"]),
        # a backslash is a path separator on Windows, never an escape
        ("python3 ~/x.py --cfg C:\\a\\b.json || true", ["--cfg", "C:\\a\\b.json"]),
        # unparseable tail -> claim nothing rather than guess
        ("python3 ~/x.py --bad 'unbalanced", []),
        ("bash ~/x.sh || true", []),
    ]
    wrong = [(c, want, ins._hook_script_args(c))
             for c, want in cases if ins._hook_script_args(c) != want]
    if wrong:
        for c, want, got in wrong:
            bad("argument extraction", f"{c[:58]!r} wanted {want} got {got}")
    else:
        ok(f"all {len(cases)} command shapes extract the right arguments "
           "(masking noise never forwarded)")


# --------------------------------------------------------------------------
# 2. A POSIX command with arguments round-trips to a Windows command that
#    still carries them.
# --------------------------------------------------------------------------
def test_posix_args_round_trip(ins) -> None:
    posix = ("[ -f ~/.claude/hooks/lint-claude-settings.py ] && "
             "python3 ~/.claude/hooks/lint-claude-settings.py --test || true")
    tpl = {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": posix}]}]}}
    got = windows_template(ins, tpl)["hooks"]["SessionStart"][0]["hooks"][0]["command"]

    if got.endswith(" --test"):
        ok("a POSIX command's argument survives the Windows rewrite")
    else:
        bad("a POSIX command's argument survives the Windows rewrite", got)
        return

    # It has to land where hook_runner reads argv `[--fallback MODE] <target>
    # [extra...]` — i.e. AFTER the quoted target, not before it.
    if got.index('lint-claude-settings.py') < got.index('--test'):
        ok("the argument follows the target, where the runner forwards it")
    else:
        bad("the argument follows the target", got)

    # Round-trip: reading the rewritten command back yields the same arguments.
    if ins._hook_script_args(got) == ins._hook_script_args(posix) == ["--test"]:
        ok("POSIX and Windows forms of one hook report identical arguments")
    else:
        bad("POSIX and Windows forms report identical arguments",
            f"{ins._hook_script_args(posix)} vs {ins._hook_script_args(got)}")

    # And the noise around it is still gone.
    for noise in ("2>/dev/null", "|| true", "||", "; fi", "echo"):
        if noise in got:
            bad("POSIX masking noise is not forwarded as an argument",
                f"{noise!r} in {got}")
            return
    ok("POSIX masking noise is not forwarded as an argument")

    # No template hook needs this today, so assert it directly rather than
    # leaving the quoting rule to be exercised by a hook that may never exist:
    # an argument with a space has to come back out as ONE argument.
    spaced = ("[ -f ~/.claude/hooks/x.py ] && "
              "python3 ~/.claude/hooks/x.py --note 'two words' 2>/dev/null || true")
    tpl = {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": spaced}]}]}}
    got = windows_template(ins, tpl)["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    if ins._hook_script_args(got) == ["--note", "two words"]:
        ok("a multi-word argument survives the rewrite as one argument")
    else:
        bad("a multi-word argument survives the rewrite as one argument",
            f"{ins._hook_script_args(got)} from {got}")


# --------------------------------------------------------------------------
# 3. The reported symptom: the two lint-claude-settings entries stay two.
# --------------------------------------------------------------------------
def test_lint_pair_survives_install(ins) -> None:
    for label, windows in (("POSIX", False), ("Windows", True)):
        tpl = resolved_template(ins, windows=windows)
        if len(lint_entries(tpl)) != 2:
            bad(f"[{label}] hooks.json still ships the lint pair",
                f"{len(lint_entries(tpl))} entries in the template")
            continue

        entries = lint_entries(installed(ins, tpl))
        if len(entries) != 2:
            bad(f"[{label}] both lint-claude-settings entries survive the install",
                f"{len(entries)}: {entries}")
            continue
        ok(f"[{label}] both lint-claude-settings entries survive the install")

        with_test = [c for c in entries if "--test" in c]
        if len(with_test) == 1:
            ok(f"[{label}] exactly one survivor runs the linter's --test self-test")
        else:
            bad(f"[{label}] exactly one survivor runs --test", str(entries))

        # A second install must not churn: no third copy, no lost entry.
        again = lint_entries(installed(ins, tpl, existing=installed(ins, tpl)))
        if again == entries:
            ok(f"[{label}] re-running the install is idempotent for the pair")
        else:
            bad(f"[{label}] re-running the install is idempotent", str(again))


def test_upgrade_from_collapsed_install(ins) -> None:
    """The population that HAS the bug: settings.json already holds one entry.

    An upgrade has to notice the missing sibling and add it — a fix that only
    works on fresh installs leaves every affected machine exactly as it was."""
    tpl = resolved_template(ins, windows=True)
    plain = [c for c in lint_entries(tpl) if "--test" not in c]
    if not plain:
        bad("the collapsed-install fixture reflects the shipped template")
        return
    collapsed = {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": plain[0]}]}]}}

    entries = lint_entries(installed(ins, tpl, existing=collapsed))
    if len(entries) == 2 and sum("--test" in c for c in entries) == 1:
        ok("upgrading a collapsed install restores the missing --test entry")
    else:
        bad("upgrading a collapsed install restores the missing --test entry",
            str(entries))


# --------------------------------------------------------------------------
# 4. Negative control — the asserts above must fail on the PRE-FIX code.
# --------------------------------------------------------------------------
def test_pre_fix_collapses(ins) -> None:
    """Restore the old behaviour and prove the pair collapses under it.

    Without this the tests above could pass for reasons unrelated to the fix
    (e.g. a template edit), and nobody would learn that the guard went blind."""
    def pre_fix_same(a: str, b: str) -> bool:      # no argument comparison
        for fp in ins.ABS_FINGERPRINTS:
            if fp in a and fp in b:
                return True
        if ins._owned_basenames(a) & ins._owned_basenames(b):
            return True
        return a.strip() == b.strip()

    def pre_fix_key(cmd: str):                     # keyed on basename alone
        return frozenset(ins._owned_basenames(cmd)) or None

    real_same, real_key = ins.is_same_command, ins._owned_hook_key
    try:
        ins.is_same_command = pre_fix_same          # type: ignore[assignment]
        ins._owned_hook_key = pre_fix_key           # type: ignore[assignment]

        # (a) Windows: pre-fix rewrite made the pair byte-identical.
        tpl = resolved_template(ins, windows=True)
        for group in tpl["hooks"]["SessionStart"]:
            for h in group["hooks"]:
                h["command"] = drop_args(h["command"])
        entries = lint_entries(installed(ins, tpl))
        if len(entries) == 1 and "--test" not in entries[0]:
            ok("negative control: pre-fix Windows install collapsed the pair "
               "(the --test self-test never ran)")
        else:
            bad("negative control: pre-fix Windows install collapsed the pair",
                f"got {len(entries)}: {entries}")

        # (b) POSIX: basename-only identity collapsed it there too, the other way.
        entries = lint_entries(installed(ins, resolved_template(ins, windows=False)))
        if len(entries) == 1:
            ok("negative control: pre-fix POSIX install collapsed the pair too")
        else:
            bad("negative control: pre-fix POSIX install collapsed the pair",
                f"got {len(entries)}: {entries}")
    finally:
        ins.is_same_command = real_same              # type: ignore[assignment]
        ins._owned_hook_key = real_key               # type: ignore[assignment]


# --------------------------------------------------------------------------
# 5. Arguments APPEND. Nothing else in the table may change.
# --------------------------------------------------------------------------
def test_no_churn_for_argless_hooks(ins) -> None:
    """Every argument-less hook must rewrite byte-identically to before.

    settings.json is rewritten on every install and auto-update; a change in
    shape here rewrites the whole hook table on machines where nothing moved."""
    tpl = resolved_template(ins, windows=True)
    commands = [h.get("command", "")
                for groups in tpl["hooks"].values()
                for g in groups for h in g.get("hooks", [])]
    changed = [c for c in commands if drop_args(c) != c]
    if len(changed) == 1 and changed[0].endswith(" --test"):
        ok(f"exactly 1 of {len(commands)} Windows hook commands gained arguments")
    else:
        bad("only the argument-bearing hook changes shape",
            f"{len(changed)} changed: {[c[-60:] for c in changed]}")

    # Whatever arguments DO get forwarded must survive a shell that splits on
    # whitespace: a bare token, or a quoted one.
    unquoted = [(c, a) for c in changed for a in ins._hook_script_args(c)
                if any(ch.isspace() for ch in a) and f'"{a}"' not in c]
    if unquoted:
        bad("a forwarded argument containing a space is quoted", str(unquoted))
    else:
        ok("forwarded arguments are quoted only when they need it")


def main() -> int:
    ins = load_installer()
    test_arg_extraction(ins)
    test_posix_args_round_trip(ins)
    test_lint_pair_survives_install(ins)
    test_upgrade_from_collapsed_install(ins)
    test_pre_fix_collapses(ins)
    test_no_churn_for_argless_hooks(ins)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
