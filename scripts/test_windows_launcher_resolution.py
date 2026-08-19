#!/usr/bin/env python3
"""test_windows_launcher_resolution.py — the Windows launcher token, resolved
once at install and PROVEN before it is written (MYC-3877).

Run: python3 scripts/test_windows_launcher_resolution.py
Stdlib only, no pytest. Exit 0 = pass. Writes only inside a tempdir.
Auto-discovered by scripts/ci.sh via the scripts/test_*.py glob.

THE DEFECT. Every Windows hook command started with `py -3`. py.exe is a
LAUNCHER STUB: it starts, re-reads the PEP 514 registry to find a Python 3, and
only then starts the interpreter. Measured on an Intel i5-9300H / Windows 11
with live AV: 211 ms per hook through `py -3` vs 189 ms through python.exe — 21
ms, about 11% of the per-hook bill, paid on every hook of every tool call to
re-derive an answer that does not change between installs.

WHY THIS IS NOT A ONE-LINE CHANGE, and what these asserts are really about.
`py` was not chosen for convenience. It is a BARE, UNQUOTED, space-free, ASCII
token, and PORTABILITY.md section 5 records that as the one command shape
PowerShell 5.1, PowerShell 7, cmd.exe and Git Bash all parse identically. An
absolute path is a DIFFERENT shape, so it inherits none of that proof:

  * A backslash is an ESCAPE in Git Bash. test_bash_word_splitting below runs
    real bash and shows `C:\\Users\\x\\python.exe` arriving as
    `C:Usersxpython.exe` — every hook would fail on every prompt, which is the
    exact outage this whole subsystem exists to prevent.
  * Quoting is not available as a fix: test_powershell_tokenizer runs the real
    PowerShell parser and shows a quoted first token classified as a String,
    not a Command. That is PORTABILITY.md's claim, mechanically confirmed.
  * A path with a SPACE therefore cannot be written at all in its long form.
    The 8.3 short name is space-free by construction, which is how this
    installer already ASCII-safes hook paths.
  * Whether cmd.exe accepts the forward-slash spelling is a fact about the
    user's machine that cannot be settled from a POSIX CI runner. So the
    installer settles it AT INSTALL TIME by executing the exact token it is
    about to write, in every shell present on that machine, and keeps `py -3`
    if anything refuses. The unproven case loses.

So the proof is in two halves, and both are asserted here:
  LOCAL, portable, every CI run - real bash and (where present) the real
    PowerShell parser adjudicate the token shape.
  PER MACHINE, at install time - _token_parses_in_every_shell() runs it in
    cmd.exe / PowerShell / pwsh / Git Bash, and the fail-safe direction is
    tested here with a shell that refuses.
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
    spec = importlib.util.spec_from_file_location("_abs_launcher_test", INSTALLER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A realistic Windows interpreter path with a space in it, and the 8.3 short
# name Windows would hand back for it. Both are literals so this test asserts
# the same thing on every platform.
SPACEY = r"C:\Program Files\Python312\python.exe"
SHORT_8_3 = r"C:\PROGRA~1\PYTHO~1\python.exe"


# ---------------------------------------------------------------------------
# 1. Interpreter probe: a real CPython 3 answers, a Store stub does not.
# ---------------------------------------------------------------------------
def test_probe_interpreter(ins, tmp: Path) -> None:
    got = ins._probe_interpreter([sys.executable])
    if got and os.path.samefile(got, sys.executable):
        ok("_probe_interpreter resolves a real interpreter to its own absolute "
           "path (this is the value `py -3` costs 21 ms per spawn to re-derive)")
    else:
        bad("_probe_interpreter resolves a real interpreter", repr(got))

    # The Microsoft Store `python` alias stub: it is on PATH, it runs, and it
    # does NOT execute your code. The pre-fix probe accepted anything that
    # exited 0, so a stub that exits quietly would have passed it.
    stub = tmp / ("store_stub.bat" if os.name == "nt" else "store_stub")
    if os.name == "nt":
        stub.write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")
    else:
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        stub.chmod(0o755)
    if ins._probe_interpreter([str(stub)]) is None:
        ok("_probe_interpreter REJECTS a stub that exits 0 without running the "
           "code (the Microsoft Store `python` alias shape)")
    else:
        bad("_probe_interpreter rejects the Store alias stub shape")

    # ...and a candidate that answers with the wrong major version.
    py2ish = tmp / ("py2ish.bat" if os.name == "nt" else "py2ish")
    if os.name == "nt":
        py2ish.write_text("@echo off\r\necho ABSPY2;C:\\py2.exe\r\n", encoding="ascii")
    else:
        py2ish.write_text("#!/bin/sh\nprintf 'ABSPY2;/usr/bin/python2'\n",
                          encoding="ascii")
        py2ish.chmod(0o755)
    if ins._probe_interpreter([str(py2ish)]) is None:
        ok("_probe_interpreter REJECTS an interpreter that is not Python 3")
    else:
        bad("_probe_interpreter rejects a non-Python-3 candidate")

    if ins._probe_interpreter([str(tmp / "does-not-exist")]) is None:
        ok("_probe_interpreter returns None for a candidate that cannot run")
    else:
        bad("_probe_interpreter returns None for an unrunnable candidate")


# ---------------------------------------------------------------------------
# 2. Token shaping: space-free, ASCII, forward slashes first.
# ---------------------------------------------------------------------------
def test_token_candidates(ins) -> None:
    real_short = ins._win_short_path
    try:
        # A machine where 8.3 names ARE available (the normal Windows case).
        ins._win_short_path = lambda p: (  # type: ignore[assignment]
            SHORT_8_3.replace("\\", "/") if "/" in p else SHORT_8_3)

        cands = ins._win_bare_token_candidates(SPACEY)
        if cands and all(" " not in c for c in cands):
            ok("a spaced interpreter path is never offered with its space "
               "(8.3-shortened first)")
        else:
            bad("a spaced path is shortened, never offered raw", str(cands))
        if cands and "/" in cands[0] and "\\" not in cands[0]:
            ok("the forward-slash spelling is offered FIRST (a backslash is an "
               "escape in Git Bash)")
        else:
            bad("forward slashes come first", str(cands))

        # A space-free path needs no shortening and keeps both spellings.
        plain = r"C:\Users\dev\AppData\Local\Programs\Python\Python312\python.exe"
        cands = ins._win_bare_token_candidates(plain)
        if cands[:1] == [plain.replace("\\", "/")] and plain in cands:
            ok("a space-free path offers the forward-slash form first and the "
               "native form as the fallback spelling")
        else:
            bad("space-free path candidates", str(cands))

        # 8.3 UNAVAILABLE (fsutil 8dot3name disabled) -> nothing is offered.
        ins._win_short_path = lambda p: None  # type: ignore[assignment]
        if ins._win_bare_token_candidates(SPACEY) == []:
            ok("with no 8.3 name available, a spaced path yields NO candidate "
               "(the installer keeps `py -3` rather than write a broken token)")
        else:
            bad("a spaced path with no 8.3 name yields no candidate",
                str(ins._win_bare_token_candidates(SPACEY)))

        accented = "C:\\Users\\Jos\u00e9\\Python312\\python.exe"
        if ins._win_bare_token_candidates(accented) == []:
            ok("with no 8.3 name available, a non-ASCII path yields NO "
               "candidate (a legacy code page would mangle it)")
        else:
            bad("non-ASCII path with no 8.3 name yields no candidate",
                str(ins._win_bare_token_candidates(accented)))

        if ins._win_bare_token_candidates("") == []:
            ok("an empty interpreter path yields no candidate")
        else:
            bad("empty path yields no candidate")
    finally:
        ins._win_short_path = real_short  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 3. THE PROOF, half one: real shells, portable, on every CI run.
# ---------------------------------------------------------------------------
def test_bash_word_splitting(ins) -> None:
    """Git Bash is bash. Run real bash and look at the words it produces.

    This is the whole reason the forward-slash spelling is tried first, and it
    is asserted rather than reasoned about because getting it wrong means every
    hook fails on every prompt on a Git-Bash install."""
    def words(token: str) -> list[str]:
        script = 'for w in %s -V; do printf "[%%s]" "$w"; done' % token
        res = subprocess.run(["bash", "-c", script], capture_output=True,
                             timeout=60, text=True, encoding="utf-8",
                             errors="replace")
        return res.stdout.strip().strip("[]").split("][") if res.stdout else []

    if not shutil.which("bash"):
        bad("bash is available to prove the Git Bash leg",
            "no bash on PATH — this proof cannot be skipped silently")
        return

    fwd = SHORT_8_3.replace("\\", "/")
    if words(fwd) == [fwd, "-V"]:
        ok(f"real bash parses {fwd} as ONE unmangled word (Git Bash leg proven)")
    else:
        bad("bash keeps the forward-slash 8.3 token intact", str(words(fwd)))

    # NEGATIVE CONTROL: the native spelling is destroyed, which is exactly what
    # the install-time probe is there to catch on a machine that has bash.
    got = words(SHORT_8_3)
    if got and got[0] != SHORT_8_3 and "\\" not in got[0]:
        ok(f"negative control: bash collapses the backslash form to {got[0]} — "
           "so an unproven native-spelling token would kill every hook")
    else:
        bad("negative control: bash mangles the backslash form", str(got))


def test_powershell_tokenizer(ins, tmp: Path) -> None:
    """Ask the real PowerShell parser how it classifies the first token.

    PowerShell is the shell whose rule forces the whole design: a QUOTED first
    token is a string literal, not an invocation, so quoting is not available
    to us and the token must survive bare. Both halves are asserted."""
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        ok("PowerShell tokenizer check skipped (no pwsh/powershell on this box; "
           "the install-time probe covers this shell on the user's machine)")
        return

    # The line is assembled INSIDE PowerShell from a bare -Token argument: a
    # command line carrying its own double quotes cannot survive being passed
    # through -File as one, which is the same class of problem this whole test
    # is about.
    script = tmp / "tokenize.ps1"
    script.write_text(
        "param([string]$Token, [switch]$Quoted)\n"
        "$line = if ($Quoted) { '\"' + $Token + '\" -V' } else { \"$Token -V\" }\n"
        "$errs = $null\n"
        "$toks = [System.Management.Automation.PSParser]::Tokenize("
        "$line, [ref]$errs)\n"
        "if ($errs -and $errs.Count -gt 0) { "
        "Write-Output ('PARSE-ERROR|' + $errs[0].Message); exit 1 }\n"
        "Write-Output (\"$($toks[0].Type)|$($toks[0].Content)\")\n",
        encoding="utf-8")

    def first_token(token: str, quoted: bool = False) -> str:
        argv = [pwsh, "-NoProfile", "-File", str(script), "-Token", token]
        if quoted:
            argv.append("-Quoted")
        res = subprocess.run(argv, capture_output=True, timeout=120,
                             text=True, encoding="utf-8", errors="replace")
        return (res.stdout or "").strip()

    fwd = SHORT_8_3.replace("\\", "/")
    got = first_token(fwd)
    if got == f"Command|{fwd}":
        ok(f"the real PowerShell parser reads bare {fwd} as a Command token")
    else:
        bad("PowerShell reads the bare token as a Command", got)

    # NEGATIVE CONTROL. Quoting the first token is the obvious way to carry a
    # path with a space, and PowerShell forbids it: the line stops being a
    # command at all. Either verdict proves it (a String token, or a hard parse
    # error), and pwsh 7 gives the harder one.
    got = first_token(fwd, quoted=True)
    if got.startswith("String|") or got.startswith("PARSE-ERROR|"):
        ok("negative control: PowerShell refuses a QUOTED first token as an "
           f"invocation ({got.split('|')[0]}) — which is why the launcher can "
           "never be quoted, and why a spaced path must be 8.3-shortened")
    else:
        bad("PowerShell refuses a quoted first token as an invocation", got)


# ---------------------------------------------------------------------------
# 4. THE PROOF, half two: the install-time gate, and its fail-safe direction.
# ---------------------------------------------------------------------------
def test_install_time_shell_gate(ins) -> None:
    if ins._token_parses_in_every_shell(sys.executable):
        ok("_token_parses_in_every_shell accepts a token every present shell "
           "can actually run")
    else:
        bad("_token_parses_in_every_shell accepts a runnable token",
            f"{sys.executable} was refused")

    # A token no shell can run must be refused. This is the direction that
    # matters: the gate exists to make the ambiguous case LOSE.
    if not ins._token_parses_in_every_shell("/nonexistent/interpreter-xyz"):
        ok("_token_parses_in_every_shell REFUSES a token the shells cannot run")
    else:
        bad("_token_parses_in_every_shell refuses an unrunnable token")

    # The mangling case, end to end: a backslash token through real bash.
    real_prefixes = ins._shell_probe_prefixes
    try:
        ins._shell_probe_prefixes = lambda: [  # type: ignore[assignment]
            ("bash", ["bash", "-c"])]
        if not ins._token_parses_in_every_shell(SHORT_8_3):
            ok("the gate refuses the backslash spelling when bash is present "
               "(the Git Bash mangling, caught before anything is written)")
        else:
            bad("the gate refuses the backslash spelling under bash")

        # No shell present proves NOTHING, and nothing is not a pass.
        ins._shell_probe_prefixes = lambda: []  # type: ignore[assignment]
        if not ins._token_parses_in_every_shell(sys.executable):
            ok("with no shell to ask, the gate proves nothing and says no")
        else:
            bad("with no shell present the gate must not claim proof")
    finally:
        ins._shell_probe_prefixes = real_prefixes  # type: ignore[assignment]

    # Git Bash's bash.exe is usually NOT on PATH even where git.exe is; the
    # discovery has to look beside git or it silently skips the strictest shell.
    src = INSTALLER.read_text(encoding="utf-8")
    if 'shutil.which("git")' in src and "bash.exe" in src:
        ok("shell discovery looks for Git Bash beside git.exe, not only on PATH")
    else:
        bad("shell discovery finds Git Bash beside git.exe")


# ---------------------------------------------------------------------------
# 5. _windows_launcher: what actually gets written, and the escape hatches.
# ---------------------------------------------------------------------------
def test_windows_launcher(ins) -> None:
    real_probe = ins._probe_interpreter
    real_gate = ins._token_parses_in_every_shell
    real_cands = ins._win_bare_token_candidates
    env_keys = ("ABS_WIN_LAUNCHER", "ABS_WIN_ABS_INTERPRETER",
                "ABS_WIN_UTF8_MODE")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    for k in env_keys:
        os.environ.pop(k, None)
    try:
        ins._probe_interpreter = lambda argv: SPACEY  # type: ignore[assignment]
        ins._win_bare_token_candidates = lambda exe: [  # type: ignore[assignment]
            SHORT_8_3.replace("\\", "/")]

        ins._token_parses_in_every_shell = lambda t: True  # type: ignore[assignment]
        got = ins._windows_launcher()
        if got == [SHORT_8_3.replace("\\", "/"), "-X", "utf8"]:
            ok("a PROVEN absolute interpreter is what gets written (the `py -3` "
               "launcher process is gone from every hook)")
        else:
            bad("a proven absolute interpreter is written", str(got))
        if got and " " not in got[0] and got[0].isascii():
            ok("the written launcher's FIRST token is a bare space-free ASCII "
               "token, the shape PORTABILITY.md section 5 requires")
        else:
            bad("the written launcher keeps the required token shape", str(got))
        # UTF-8 Mode moved onto the command line when the hook stopped being a
        # child process: PYTHONUTF8 in a child env has nothing left to act on,
        # and without it a hook's open().read() decodes with the console code
        # page (PR #446). Every added token must be bare ASCII too.
        if got[1:] == ["-X", "utf8"] and all(
                t.isascii() and " " not in t for t in got[1:]):
            ok("the launcher turns on PEP 540 UTF-8 Mode for the interpreter "
               "the hook itself now runs in, in bare ASCII tokens")
        else:
            bad("the launcher enables UTF-8 Mode", str(got))

        # The FULL prefix is what gets proven, not just its first token.
        seen: list = []
        ins._token_parses_in_every_shell = lambda t: (  # type: ignore[assignment]
            seen.append(t), True)[1]
        ins._windows_launcher()
        if any(not isinstance(t, str) and list(t)[-2:] == ["-X", "utf8"]
               for t in seen):
            ok("the whole prefix goes back through the shell gate before it is "
               "written (bare flags are proven, not assumed)")
        else:
            bad("the whole prefix is re-proven with the utf8 flags", str(seen))

        # Unproven extra tokens -> the prefix WITHOUT them, never a guess.
        ins._token_parses_in_every_shell = (  # type: ignore[assignment]
            lambda t: isinstance(t, str))
        got = ins._windows_launcher()
        if got == [SHORT_8_3.replace("\\", "/")]:
            ok("a prefix the shells will not run loses the extra flags and "
               "keeps the proven token (fail-safe in both directions)")
        else:
            bad("an unproven prefix drops back to the proven token", str(got))

        # The opt-out may only turn UTF-8 Mode off.
        ins._token_parses_in_every_shell = lambda t: True  # type: ignore[assignment]
        os.environ["ABS_WIN_UTF8_MODE"] = "0"
        got = ins._windows_launcher()
        del os.environ["ABS_WIN_UTF8_MODE"]
        if got == [SHORT_8_3.replace("\\", "/")]:
            ok("ABS_WIN_UTF8_MODE=0 drops `-X utf8` for anyone who needs the "
               "interpreter's own locale")
        else:
            bad("ABS_WIN_UTF8_MODE=0 drops the utf8 flags", str(got))

        # UNPROVEN -> the status quo, never a guess.
        ins._token_parses_in_every_shell = lambda t: False  # type: ignore[assignment]
        got = ins._windows_launcher()
        if got and got[0] in ("py", "python", "python3"):
            ok("an UNPROVEN token falls back to the bare launcher (fail-safe: "
               "the optimization is skipped, the install is not risked)")
        else:
            bad("an unproven token falls back to the bare launcher", str(got))

        # The opt-out may only turn the optimization OFF.
        ins._token_parses_in_every_shell = lambda t: True  # type: ignore[assignment]
        os.environ["ABS_WIN_ABS_INTERPRETER"] = "0"
        got = ins._windows_launcher()
        if got and got[0] in ("py", "python", "python3"):
            ok("ABS_WIN_ABS_INTERPRETER=0 restores the bare launcher for anyone "
               "who wants hooks to follow PATH")
        else:
            bad("ABS_WIN_ABS_INTERPRETER=0 restores the bare launcher", str(got))
        del os.environ["ABS_WIN_ABS_INTERPRETER"]

        os.environ["ABS_WIN_LAUNCHER"] = "py -3"
        if ins._windows_launcher() == ["py", "-3"]:
            ok("ABS_WIN_LAUNCHER still overrides everything (hermetic tests)")
        else:
            bad("ABS_WIN_LAUNCHER overrides everything")
    finally:
        ins._probe_interpreter = real_probe  # type: ignore[assignment]
        ins._token_parses_in_every_shell = real_gate  # type: ignore[assignment]
        ins._win_bare_token_candidates = real_cands  # type: ignore[assignment]
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# 6. The whole command, and the migration of an install that already exists.
# ---------------------------------------------------------------------------
def _commands(settings: dict) -> list[str]:
    return [h.get("command", "")
            for groups in (settings.get("hooks") or {}).values()
            for g in groups for h in g.get("hooks", [])]


def test_hook_identity_ignores_the_launcher(ins) -> None:
    """The launcher is a machine fact, not part of a hook's identity.

    9 of the template's 60 Windows commands are wired but not installer-OWNED
    (no ABS fingerprint, no owned basename), so their identity falls through to
    a literal text comparison. Before _without_launcher() that comparison read a
    launcher change as a brand-new hook, and every existing Windows install
    would have grown a duplicate of those 9 on its next update."""
    runner = "C:/U/.claude/skills/ai-brain-starter/scripts/hook_runner.py"
    target = "C:/U/.claude/skills/ai-brain-starter/hooks/some-sample-hook.py"
    old = f'py -3 "{runner}" --fallback silent "{target}"'
    new = f'{SHORT_8_3.replace(chr(92), "/")} "{runner}" --fallback silent "{target}"'
    if ins.is_same_command(old, new):
        ok("the same hook under two different launchers is ONE hook "
           "(so an upgrade rewrites it instead of duplicating it)")
    else:
        bad("launcher change does not create a new hook identity")

    # ...but a different TARGET, or different ARGUMENTS, still is not the same.
    other = new.replace("some-sample-hook.py", "some-other-hook.py")
    if not ins.is_same_command(old, other):
        ok("two different targets are still two different hooks")
    else:
        bad("different targets stay distinct")
    if not ins.is_same_command(old, new + " --test"):
        ok("the same target with different arguments is still not the same hook")
    else:
        bad("differing arguments stay distinct")


def test_generated_commands_keep_their_shape(ins) -> None:
    token = SHORT_8_3.replace("\\", "/")
    real_launcher = ins._windows_launcher
    try:
        ins._windows_launcher = lambda: [token]  # type: ignore[assignment]
        tpl = json.loads((ROOT / "hooks.json").read_text(encoding="utf-8"))
        tpl = ins.normalize_path_substitutions(tpl, str(Path.home() / "vault"))
        tpl = ins.substitute_python_interpreter(tpl)
        win, _skipped = ins.platformize_template_for_windows(tpl)
        cmds = _commands(win)
        if not cmds:
            bad("the platformized template has commands")
            return

        wrong_first = [c for c in cmds if not c.startswith(token + " ")]
        if not wrong_first:
            ok(f"all {len(cmds)} Windows commands start with the bare resolved "
               "interpreter, unquoted")
        else:
            bad("every command starts with the bare token",
                f"{len(wrong_first)}: {wrong_first[:2]}")

        unquoted_paths = [c for c in cmds if 'hook_runner.py"' not in c]
        if not unquoted_paths:
            ok("the runner path after it is still double-quoted (the shape all "
               "four shells parse: bare command, quoted arguments)")
        else:
            bad("path arguments stay quoted", str(unquoted_paths[:2]))

        isms = [c for c in cmds
                if any(s in c for s in ("||", "&&", "[ -f", "2>/dev/null"))]
        if not isms:
            ok("no POSIX shell-isms survive into a Windows command")
        else:
            bad("no shell-isms survive", str(isms[:2]))

        # The brief's explicit requirement, restated as an assert: nothing with
        # a space is ever emitted as the first token.
        spacey_first = [c for c in cmds if " " in c.split(" ")[0]]
        if not spacey_first:
            ok("no command's first token contains a space (a spaced "
               "interpreter path is 8.3-shortened or the bare launcher is kept)")
        else:
            bad("no first token contains a space", str(spacey_first[:2]))
    finally:
        ins._windows_launcher = real_launcher  # type: ignore[assignment]


def test_existing_install_migrates_without_duplicating(ins) -> None:
    """Users are already on `py -3`. Re-running must MOVE them, not double them."""
    real_launcher = ins._windows_launcher
    token = SHORT_8_3.replace("\\", "/")
    try:
        tpl = json.loads((ROOT / "hooks.json").read_text(encoding="utf-8"))
        tpl = ins.normalize_path_substitutions(tpl, str(Path.home() / "vault"))
        tpl = ins.substitute_python_interpreter(tpl)

        ins._windows_launcher = lambda: ["py", "-3"]  # type: ignore[assignment]
        old_win, _ = ins.platformize_template_for_windows(tpl)
        old_install, _ = ins.merge_hooks({}, old_win)
        old_install, _ = ins.dedupe_owned_hooks(old_install)
        old_install, _ = ins.dedupe_identical_hooks(old_install)
        before = len(_commands(old_install))

        ins._windows_launcher = lambda: [token]  # type: ignore[assignment]
        new_win, _ = ins.platformize_template_for_windows(tpl)
        migrated, _ = ins.merge_hooks(old_install, new_win)
        migrated, _ = ins.dedupe_owned_hooks(migrated)
        migrated, _ = ins.dedupe_identical_hooks(migrated)
        cmds = _commands(migrated)

        if len(cmds) == before:
            ok(f"upgrading a `py -3` install rewrites all {before} commands in "
               "place — no duplicates")
        else:
            bad("upgrade does not duplicate", f"{before} -> {len(cmds)}")
        leftovers = [c for c in cmds if c.startswith("py -3 ")]
        if not leftovers:
            ok("no `py -3` command survives the upgrade")
        else:
            bad("no `py -3` command survives", f"{len(leftovers)} left")

        again, _ = ins.merge_hooks(migrated, new_win)
        again, _ = ins.dedupe_owned_hooks(again)
        again, _ = ins.dedupe_identical_hooks(again)
        if _commands(again) == cmds:
            ok("a second run is a no-op (idempotent, so auto-update cannot churn "
               "settings.json every session)")
        else:
            bad("second run is idempotent",
                f"{len(_commands(again))} vs {len(cmds)}")
    finally:
        ins._windows_launcher = real_launcher  # type: ignore[assignment]


def main() -> int:
    ins = load_installer()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_probe_interpreter(ins, tmp)
        test_token_candidates(ins)
        test_bash_word_splitting(ins)
        test_powershell_tokenizer(ins, tmp)
        test_install_time_shell_gate(ins)
        test_windows_launcher(ins)
        test_hook_identity_ignores_the_launcher(ins)
        test_generated_commands_keep_their_shape(ins)
        test_existing_install_migrates_without_duplicating(ins)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): failure details echo paths, and one
    # case deliberately carries an accented one.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
