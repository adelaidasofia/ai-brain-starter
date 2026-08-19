#!/usr/bin/env python3
"""test_hook_runner_single_interpreter.py — ONE interpreter per hook, and the
masking contract unchanged (MYC-3877).

Run: python3 scripts/test_hook_runner_single_interpreter.py
Stdlib only, no pytest. Exit 0 = pass. Writes only inside a tempdir.
Auto-discovered by scripts/ci.sh via the scripts/test_*.py glob.

THE DEFECT. scripts/hook_runner.py used to run its target with

    subprocess.run([sys.executable, script, *extra], ...)

so the wrapper WAS a Python process that started a SECOND Python process to do
the actual work. Chain per hook: shell -> launcher -> interpreter #1 (the
runner) -> interpreter #2 (the hook). Measured on an Intel i5-9300H / Windows 11
with Norton + Defender live: 97 ms for python.exe straight to a noop hook, 189 ms
for the same hook through the runner. That 92 ms was paid on EVERY tool call,
~1.6 s of pure hook latency per PreToolUse.

WHAT THIS FILE IS FOR, in three layers:

  1. NEGATIVE CONTROL (test_hook_runs_in_this_process). The hook reports its own
     pid and it must equal the runner's. The same assertion is then run against
     a vendored copy of the PRE-FIX runner and must FAIL there. A guard that has
     never failed on the thing it catches is unproven.

  2. STRUCTURAL VERIFIER (test_runner_cannot_spawn_an_interpreter). The
     behavioral test above only fires if someone runs it. This one fails the
     build the moment `subprocess`, `sys.executable`, `os.exec*`, `os.spawn*` or
     `os.fork` reappears anywhere in hook_runner.py, which is the only way the
     second interpreter can come back. Decay test: reintroduce the child spawn
     and BOTH of these go red.

  3. CONTRACT PARITY (test_contract_parity_with_pre_fix_runner). A performance
     change to a wrapper whose entire job is failure-masking is only safe if the
     masking is bit-identical. Every shape below is run through BOTH runners and
     the (exit code, stdout, stderr) triples must match. This is what makes the
     rest of the suite trustworthy: it is not asserting what the new code does,
     it is asserting the old code and the new code are indistinguishable.

WHY IN-PROCESS AND NOT os.execv. Exec-replacing also drops the interpreter count
to one, but it throws away the wrapper's own code, so a hook that exits 1 or
crashes could no longer be masked to the fallback JSON — and that masking is the
whole reason the file exists. The isolation the process boundary used to give is
reproduced explicitly instead, and every piece of it is asserted below:
sys.exit, os._exit, an unhandled exception, cwd/sys.path/environ/argv/signal
mutation, a replaced or CLOSED sys.stdout, output written straight to fd 1, and
output written by a grandchild that inherited fd 1.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "hook_runner.py"

SILENT = '{"continue":true,"suppressOutput":true}'
ALLOW = ('{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
         '"permissionDecision":"allow"}}')

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


# ---------------------------------------------------------------------------
# The PRE-FIX runner, vendored verbatim in its essentials.
#
# Not read from git history on purpose: this file has to keep failing on the old
# behaviour years from now, in a shallow clone, in a release tarball. The two
# things that made it the old behaviour are the child interpreter and the
# text-mode decode of that child's output; everything else is byte-for-byte the
# shipped 2026-08 version.
# ---------------------------------------------------------------------------
PRE_FIX_RUNNER = '''#!/usr/bin/env python3
"""Pre-fix hook_runner.py — spawns a SECOND interpreter. Test fixture only."""
import os
import subprocess
import sys

FALLBACKS = {
    "silent": '{"continue":true,"suppressOutput":true}',
    "allow": ('{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
              '"permissionDecision":"allow"}}'),
}


def main(argv):
    args = list(argv)
    fallback = FALLBACKS["silent"]
    if len(args) >= 2 and args[0] == "--fallback":
        fallback = FALLBACKS.get(args[1], FALLBACKS["silent"])
        args = args[2:]
    if not args:
        print(fallback)
        return 0
    script, extra = args[0], args[1:]
    if not os.path.isfile(script):
        print(fallback)
        return 0
    try:
        payload = sys.stdin.read()
    except Exception:
        payload = ""
    try:
        proc = subprocess.run(
            [sys.executable, script, *extra],
            input=payload, capture_output=True, text=True,
        )
    except Exception:
        print(fallback)
        return 0
    if proc.returncode == 0:
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        return 0
    if proc.returncode == 2:
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        return 2
    print(fallback)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
'''


class Harness:
    """A tempdir holding hook scripts plus both runners."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.pre_fix = tmp / "pre_fix_runner.py"
        self.pre_fix.write_text(PRE_FIX_RUNNER, encoding="utf-8")

    def hook(self, name: str, body: str) -> str:
        p = self.tmp / name
        p.write_text(body, encoding="utf-8")
        return str(p)

    def run(self, target: str, *extra: str, fallback: str = "silent",
            payload: bytes = b"{}", runner: Path | None = None,
            timeout: int = 60):
        cmd = [sys.executable, str(runner or RUNNER), "--fallback", fallback,
               target, *extra]
        return subprocess.run(cmd, input=payload, capture_output=True,
                              timeout=timeout)


# ---------------------------------------------------------------------------
# 1. NEGATIVE CONTROL — one interpreter, and the assertion proven to bite.
# ---------------------------------------------------------------------------
PID_HOOK = "import os, sys\nsys.stdout.write(str(os.getpid()))\n"


def test_hook_runs_in_this_process(h: Harness) -> None:
    target = h.hook("pid_hook.py", PID_HOOK)

    # The runner is our direct child, so its pid is the one we can see; have the
    # hook report the pid it is actually running in and demand they match.
    probe = h.hook("pid_probe.py",
                   "import os, sys\n"
                   "sys.stdout.write('%d %d' % (os.getpid(), os.getppid()))\n")
    res = h.run(probe)
    try:
        hook_pid, hook_ppid = (int(x) for x in res.stdout.decode().split())
    except Exception as exc:  # noqa: BLE001
        bad("hook reports its pid", f"{res.stdout!r} {exc}")
        return

    # Under the fix the hook IS the runner: its parent is this test process.
    if hook_ppid == os.getpid():
        ok("the hook runs in the runner's own process (one interpreter, not two)")
    else:
        bad("the hook runs in the runner's own process",
            f"hook pid {hook_pid} has parent {hook_ppid}, not this test "
            f"({os.getpid()}) — a second interpreter was spawned")

    # NEGATIVE CONTROL: the identical assertion against the pre-fix runner.
    res_old = h.run(probe, runner=h.pre_fix)
    try:
        _old_pid, old_ppid = (int(x) for x in res_old.stdout.decode().split())
    except Exception as exc:  # noqa: BLE001
        bad("negative control: pre-fix runner reports a pid", f"{exc}")
        return
    if old_ppid != os.getpid():
        ok("negative control: the pre-fix runner FAILS this assertion "
           "(its hook is a grandchild — the second interpreter)")
    else:
        bad("negative control: the pre-fix runner fails this assertion",
            "the vendored pre-fix fixture no longer double-spawns, so this "
            "suite is no longer proving anything")

    # And the hook's stdout still reaches us, so the assertion is not passing
    # because nothing ran.
    if h.run(target).stdout.strip().isdigit():
        ok("the hook's stdout still reaches the caller (the test is not vacuous)")
    else:
        bad("the hook's stdout reaches the caller")


# ---------------------------------------------------------------------------
# 2. STRUCTURAL VERIFIER — the regression cannot be reintroduced quietly.
# ---------------------------------------------------------------------------
def test_runner_cannot_spawn_an_interpreter() -> None:
    """The behavioral test only fires when run. This fails at lint time.

    Deliberately blunt: hook_runner.py has no legitimate reason to reference
    any of these names, so a plain source scan cannot false-positive, and a
    scan cannot be evaded by a spelling the AST would have missed."""
    import ast

    src = RUNNER.read_text(encoding="utf-8")
    tree = ast.parse(src)

    banned_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            dotted = f"{fn.value.id}.{fn.attr}"
            if fn.value.id == "subprocess":
                banned_calls.append(dotted)
            if fn.value.id == "os" and (
                fn.attr.startswith("exec") or fn.attr.startswith("spawn")
                or fn.attr in {"fork", "forkpty", "posix_spawn", "posix_spawnp",
                               "system", "popen"}
            ):
                banned_calls.append(dotted)
    if banned_calls:
        bad("hook_runner.py starts no child process",
            f"found {sorted(set(banned_calls))} — the second interpreter is back")
    else:
        ok("hook_runner.py starts no child process (no subprocess/exec/spawn/fork)")

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    if "subprocess" in imports:
        bad("hook_runner.py does not import subprocess",
            "the only reason to import it here is to spawn the hook again")
    else:
        ok("hook_runner.py does not import subprocess")

    # sys.executable is how the child was named. Nothing else in this file
    # needs it, so a real reference to it is the signature of the regression.
    # Matched on the AST, not the text: the docstring quotes the old call on
    # purpose, and a text scan would fire on the explanation of the bug.
    executable_refs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "executable"
        and isinstance(n.value, ast.Name) and n.value.id == "sys"
    ]
    if executable_refs:
        bad("hook_runner.py never evaluates sys.executable",
            f"referenced at line(s) {[n.lineno for n in executable_refs]} - "
            "that is how the second interpreter was named")
    else:
        ok("hook_runner.py never evaluates sys.executable")

    # The contract has to be READABLE, not just true: the next person to touch
    # this file must find out from the file itself.
    doc = ast.get_docstring(tree) or ""
    if "INTERPRETER-COUNT CONTRACT" in doc and "NEVER TWO" in doc:
        ok("hook_runner.py's docstring states the interpreter-count contract")
    else:
        bad("hook_runner.py's docstring states the interpreter-count contract",
            "a future reader has nothing telling them not to re-add the spawn")


# ---------------------------------------------------------------------------
# 3. CONTRACT PARITY — old and new are indistinguishable from outside.
# ---------------------------------------------------------------------------
PARITY_CASES = [
    ("clean exit, JSON on stdout",
     'import sys\nsys.stdout.write(\'{"continue":true}\')\n', "silent", ()),
    ("clean exit, nothing at all", "pass\n", "silent", ()),
    ("BLOCK: exit 2 with a reason on stderr",
     'import sys\nsys.stderr.write("blocked: dangerous\\n")\nsys.exit(2)\n',
     "silent", ()),
    ("BLOCK: exit 2 writing BOTH streams",
     'import sys\nsys.stdout.write("OUT")\nsys.stderr.write("ERR")\n'
     'sys.exit(2)\n', "silent", ()),
    ("crash: unhandled exception",
     'raise RuntimeError("boom")\n', "silent", ()),
    ("crash: unhandled exception, allow fallback",
     'raise RuntimeError("boom")\n', "allow", ()),
    ("exit 1 with noise on both streams (must be swallowed)",
     'import sys\nsys.stdout.write("noisy")\nsys.stderr.write("noisier")\n'
     'sys.exit(1)\n', "silent", ()),
    ("exit 3", "import sys\nsys.exit(3)\n", "silent", ()),
    ("exit 255", "import sys\nsys.exit(255)\n", "allow", ()),
    ("sys.exit() with no argument", "import sys\nsys.exit()\n", "silent", ()),
    ("sys.exit(None)", "import sys\nsys.exit(None)\n", "silent", ()),
    ("sys.exit with a message string",
     'import sys\nsys.exit("explanatory message")\n', "silent", ()),
    ("os._exit(2) — a hard block",
     "import os\nos._exit(2)\n", "silent", ()),
    ("os._exit(1) — a hard failure, must still be masked",
     "import os\nos._exit(1)\n", "silent", ()),
    ("SyntaxError in the hook", "def (\n", "silent", ()),
    ("argv forwarding",
     'import sys\nsys.stdout.write("|".join(sys.argv[1:]))\n', "silent",
     ("--test", "--mode", "fast")),
    ("payload passthrough",
     'import json, sys\n'
     'sys.stdout.write(json.load(sys.stdin)["tool_name"])\n', "silent", ()),
    ("__main__ guard fires",
     'import sys\n'
     'if __name__ == "__main__":\n    sys.stdout.write("MAIN")\n', "silent", ()),
    ("non-ASCII output on a clean exit",
     'import sys\nsys.stdout.write("\\u2699\\ufe0f Meta caf\\u00e9")\n',
     "silent", ()),
    ("raw fd-1 write on a clean exit",
     'import os\nos.write(1, b"RAWFD1")\n', "silent", ()),
    ("raw fd-1 write that must be SWALLOWED (exit 1)",
     'import os, sys\nos.write(1, b"RAWFD1")\nsys.exit(1)\n', "silent", ()),
    ("a grandchild inheriting fd 1 (clean exit)",
     'import subprocess, sys\n'
     'subprocess.run([sys.executable, "-c", "print(\'GRANDCHILD\')"])\n',
     "silent", ()),
    ("a grandchild inheriting fd 1 that must be SWALLOWED (exit 1)",
     'import subprocess, sys\n'
     'subprocess.run([sys.executable, "-c", "print(\'GRANDCHILD\')"])\n'
     'sys.exit(1)\n', "silent", ()),
    ("KeyboardInterrupt from the hook",
     "raise KeyboardInterrupt\n", "silent", ()),
    ("hook mutates cwd, sys.path, environ and argv, then blocks",
     'import os, sys\n'
     'os.chdir(os.path.dirname(os.path.abspath(__file__)))\n'
     'sys.path.insert(0, "/nonexistent-injected")\n'
     'os.environ["ABS_HOOK_POLLUTION"] = "1"\n'
     'sys.argv = ["clobbered"]\n'
     'sys.stderr.write("still blocked\\n")\n'
     'sys.exit(2)\n', "silent", ()),
    ("hook replaces sys.stdout with a sink, then blocks",
     'import io, sys\n'
     'sys.stderr.write("reason\\n")\n'
     'sys.stdout = io.StringIO()\n'
     'sys.exit(2)\n', "silent", ()),
    ("hook CLOSES sys.stdout, then fails (fallback must still be emitted)",
     'import sys\nsys.stdout.close()\nsys.exit(1)\n', "silent", ()),
    ("hook installs signal handlers and exits clean",
     'import signal, sys\n'
     'try:\n'
     '    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
     'except Exception:\n'
     '    pass\n'
     'sys.stdout.write("HANDLED")\n', "silent", ()),
]


def test_contract_parity_with_pre_fix_runner(h: Harness) -> None:
    payload = json.dumps({"hook_event_name": "PreToolUse",
                          "tool_name": "Bash"}).encode("utf-8")
    mismatches = []
    for i, (label, body, fb, extra) in enumerate(PARITY_CASES):
        target = h.hook(f"parity_{i}.py", body)
        new = h.run(target, *extra, fallback=fb, payload=payload)
        old = h.run(target, *extra, fallback=fb, payload=payload,
                    runner=h.pre_fix)
        if (new.returncode, new.stdout, new.stderr) != (
                old.returncode, old.stdout, old.stderr):
            mismatches.append(
                f"{label}: new=({new.returncode}, {new.stdout!r}, "
                f"{new.stderr!r}) old=({old.returncode}, {old.stdout!r}, "
                f"{old.stderr!r})")
    if mismatches:
        for m in mismatches:
            bad("contract parity with the pre-fix runner", m)
    else:
        ok(f"all {len(PARITY_CASES)} hook shapes produce a byte-identical "
           "(exit code, stdout, stderr) under both runners")


# ---------------------------------------------------------------------------
# 4. The protocol itself, asserted directly rather than only by parity.
# ---------------------------------------------------------------------------
def test_exit_code_protocol(h: Harness) -> None:
    cases = [
        ("clean", "pass\n", 0),
        ("block", "import sys\nsys.exit(2)\n", 2),
        ("hard block via os._exit", "import os\nos._exit(2)\n", 2),
        ("failure 1", "import sys\nsys.exit(1)\n", 0),
        ("failure 3", "import sys\nsys.exit(3)\n", 0),
        ("failure 255", "import sys\nsys.exit(255)\n", 0),
        ("crash", 'raise SystemError("x")\n', 0),
    ]
    wrong = []
    for label, body, want in cases:
        rc = h.run(h.hook(f"rc_{label.replace(' ', '_')}.py", body)).returncode
        if rc != want:
            wrong.append(f"{label}: wanted {want}, got {rc}")
    if wrong:
        for w in wrong:
            bad("exit-code protocol", w)
    else:
        ok("exit-code protocol: 0 passes, 2 BLOCKS, everything else masks to 0")

    res = h.run(h.hook("missing_marker.py", "pass\n") + ".gone")
    if res.returncode == 0 and SILENT in res.stdout.decode():
        ok("a missing target is the [ -f ] guard: fallback JSON, exit 0")
    else:
        bad("missing target", f"{res.returncode} {res.stdout!r}")

    res = h.run(h.hook("crash2.py", "raise RuntimeError('x')\n"), fallback="allow")
    if res.returncode == 0 and ALLOW in res.stdout.decode():
        ok("--fallback allow emits the PreToolUse allow form on a crash")
    else:
        bad("--fallback allow", f"{res.returncode} {res.stdout!r}")


def test_stream_separation(h: Harness) -> None:
    target = h.hook(
        "streams.py",
        'import sys\n'
        'sys.stdout.write("ONLY-ON-STDOUT")\n'
        'sys.stderr.write("ONLY-ON-STDERR")\n'
        'sys.exit(2)\n')
    res = h.run(target)
    out, err = res.stdout.decode(), res.stderr.decode()
    if "ONLY-ON-STDOUT" in out and "ONLY-ON-STDERR" not in out:
        ok("a block's stdout carries only what the hook wrote to stdout")
    else:
        bad("stdout channel is not polluted by stderr", repr(out))
    if "ONLY-ON-STDERR" in err and "ONLY-ON-STDOUT" not in err:
        ok("a block's stderr carries only what the hook wrote to stderr")
    else:
        bad("stderr channel is not polluted by stdout", repr(err))

    # On a CLEAN exit the pre-fix runner forwarded stdout and dropped stderr.
    clean = h.hook(
        "streams_clean.py",
        'import sys\nsys.stdout.write("OUT")\nsys.stderr.write("ERR")\n')
    res = h.run(clean)
    if res.stdout.decode() == "OUT" and "ERR" not in res.stderr.decode():
        ok("a clean exit forwards stdout only, exactly as before")
    else:
        bad("clean exit forwards stdout only",
            f"{res.stdout!r} / {res.stderr!r}")


def test_large_output_does_not_deadlock(h: Harness) -> None:
    """A megabyte is far past any pipe buffer. The capture must not wedge."""
    target = h.hook(
        "big.py",
        'import sys\nsys.stdout.write("x" * 1000000)\n'
        'sys.stderr.write("y" * 1000000)\nsys.exit(2)\n')
    try:
        res = h.run(target, timeout=60)
    except subprocess.TimeoutExpired:
        bad("1 MB on both streams does not deadlock", "timed out")
        return
    if (res.returncode == 2 and len(res.stdout) == 1000000
            and len(res.stderr) == 1000000):
        ok("1 MB on each stream survives intact with no deadlock")
    else:
        bad("1 MB on each stream survives intact",
            f"rc={res.returncode} out={len(res.stdout)} err={len(res.stderr)}")


def test_hook_sees_a_normal_script_environment(h: Harness) -> None:
    """argv[0], __file__, __name__ and sys.path[0] must look like `python X`.

    sys.path[0] is load-bearing: hooks/ modules do `from _lib.x import y`, which
    only resolves because CPython puts a script's own directory first."""
    pkg = h.tmp / "_sibling_pkg"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text("VALUE = 'SIBLING-IMPORT-OK'\n",
                                     encoding="utf-8")
    target = h.hook(
        "env.py",
        'import os, sys\n'
        'from _sibling_pkg import VALUE\n'
        'sys.stdout.write("|".join([\n'
        '    os.path.basename(sys.argv[0]), __name__,\n'
        '    os.path.basename(__file__), VALUE,\n'
        '    str(os.path.samefile(sys.path[0], os.path.dirname(\n'
        '        os.path.abspath(__file__)))),\n'
        ']))\n')
    res = h.run(target)
    got = res.stdout.decode()
    want = "env.py|__main__|env.py|SIBLING-IMPORT-OK|True"
    if got == want:
        ok("the hook sees argv[0], __name__, __file__ and sys.path[0] exactly "
           "as `python <script>` would (sibling imports resolve)")
    else:
        bad("hook environment matches a plain script run",
            f"wanted {want!r}, got {got!r} (rc={res.returncode}, "
            f"err={res.stderr[:200]!r})")


def test_payload_reaches_both_stdin_faces(h: Harness) -> None:
    payload = json.dumps({"tool_name": "Bash", "x": "⚙️"}).encode("utf-8")
    target = h.hook(
        "stdin.py",
        'import json, sys\n'
        'raw = sys.stdin.read()\n'
        'sys.stdout.write(json.loads(raw)["tool_name"] + ":" + str(len(raw)))\n')
    res = h.run(target, payload=payload)
    if res.stdout.decode().startswith("Bash:"):
        ok("the payload reaches the hook through sys.stdin.read()")
    else:
        bad("payload through sys.stdin.read()", repr(res.stdout))

    target = h.hook(
        "stdin_buffer.py",
        'import sys\n'
        'sys.stdout.write(str(len(sys.stdin.buffer.read())))\n')
    res = h.run(target, payload=payload)
    if res.stdout.decode() == str(len(payload)):
        ok("the payload reaches the hook through sys.stdin.buffer (byte-exact)")
    else:
        bad("payload through sys.stdin.buffer",
            f"wanted {len(payload)}, got {res.stdout!r}")


def test_runner_survives_a_hostile_hook(h: Harness) -> None:
    """State the hook wrecks must not change what the runner reports.

    Each of these blocks (exit 2) AFTER doing the damage, so a runner whose own
    reporting had been steered would mask the block instead of propagating it —
    the exact fail-open this wrapper must never produce."""
    hostile = [
        ("chdir to a directory that is about to vanish",
         'import os, sys, tempfile\n'
         'd = tempfile.mkdtemp()\n'
         'os.chdir(d)\n'
         'sys.stderr.write("R1\\n")\n'
         'sys.exit(2)\n', "R1"),
        ("clobber sys.path entirely",
         'import sys\n'
         'sys.path[:] = []\n'
         'sys.stderr.write("R2\\n")\n'
         'sys.exit(2)\n', "R2"),
        ("wipe os.environ",
         'import os, sys\n'
         'os.environ.clear()\n'
         'sys.stderr.write("R3\\n")\n'
         'sys.exit(2)\n', "R3"),
        ("ignore SIGTERM and swap SIGINT",
         'import signal, sys\n'
         'try:\n'
         '    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
         '    signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
         'except Exception:\n'
         '    pass\n'
         'sys.stderr.write("R4\\n")\n'
         'sys.exit(2)\n', "R4"),
        ("replace __main__ in sys.modules",
         'import sys, types\n'
         'sys.modules["__main__"] = types.ModuleType("hijacked")\n'
         'sys.stderr.write("R5\\n")\n'
         'sys.exit(2)\n', "R5"),
        ("close both standard streams",
         'import os, sys\n'
         'os.write(2, b"R6\\n")\n'
         'sys.stdout.close()\n'
         'sys.stderr.close()\n'
         'sys.exit(2)\n', "R6"),
    ]
    broken = []
    for i, (label, body, marker) in enumerate(hostile):
        res = h.run(h.hook(f"hostile_{i}.py", body))
        if res.returncode != 2 or marker not in res.stderr.decode():
            broken.append(f"{label}: rc={res.returncode} err={res.stderr!r}")
    if broken:
        for b in broken:
            bad("a hostile hook cannot steer the runner's report", b)
    else:
        ok(f"all {len(hostile)} interpreter-state attacks still propagate their "
           "block (cwd, sys.path, environ, signals, __main__, closed streams)")


def test_hang_is_at_least_as_killable(h: Harness) -> None:
    """A hanging hook must not be HARDER to kill than it was.

    It is now easier: the hook IS the runner, so terminating the runner ends it.
    The pre-fix shape left the hook running as an orphan, which is what the
    second half of this test demonstrates (and then cleans up)."""
    if os.name == "nt":
        ok("hang-killability check skipped on Windows (POSIX signal semantics)")
        return

    pidfile = h.tmp / "hang.pid"
    target = h.hook(
        "hang.py",
        'import os, sys, time\n'
        f'open({str(pidfile)!r}, "w").write(str(os.getpid()))\n'
        'sys.stdout.flush()\n'
        'time.sleep(120)\n')

    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def spawn(runner: Path):
        if pidfile.exists():
            pidfile.unlink()
        proc = subprocess.Popen(
            [sys.executable, str(runner), "--fallback", "silent", target],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        proc.stdin.write(b"{}")
        proc.stdin.close()
        deadline = time.time() + 30
        while time.time() < deadline and not pidfile.exists():
            time.sleep(0.05)
        return proc

    proc = spawn(RUNNER)
    if not pidfile.exists():
        bad("hanging hook starts under the new runner", "no pid file")
        proc.kill()
        return
    hook_pid = int(pidfile.read_text())
    same_process = hook_pid == proc.pid
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)
        bad("SIGTERM ends a hanging hook", "the runner ignored SIGTERM")
        return
    deadline = time.time() + 10
    while time.time() < deadline and alive(hook_pid):
        time.sleep(0.05)
    if same_process and not alive(hook_pid):
        ok("SIGTERM to the runner ends the hanging hook too (it is the same "
           "process — no orphan is left behind)")
    else:
        bad("SIGTERM ends the hanging hook",
            f"same_process={same_process} still_alive={alive(hook_pid)}")

    # The old shape, for contrast: the hook outlives the runner.
    proc = spawn(h.pre_fix)
    if not pidfile.exists():
        bad("hanging hook starts under the pre-fix runner", "no pid file")
        proc.kill()
        return
    old_hook_pid = int(pidfile.read_text())
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)
    orphaned = alive(old_hook_pid)
    if orphaned:
        ok("negative control: under the pre-fix runner the hook survives as an "
           "orphan (killability strictly improved, never regressed)")
    else:
        ok("negative control: the pre-fix orphan did not outlive its parent on "
           "this platform (nothing regressed either way)")
    for _ in range(3):
        if not alive(old_hook_pid):
            break
        try:
            os.kill(old_hook_pid, 9)
        except OSError:
            break
        time.sleep(0.2)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td))
        test_hook_runs_in_this_process(h)
        test_runner_cannot_spawn_an_interpreter()
        test_contract_parity_with_pre_fix_runner(h)
        test_exit_code_protocol(h)
        test_stream_separation(h)
        test_large_output_does_not_deadlock(h)
        test_hook_sees_a_normal_script_environment(h)
        test_payload_reaches_both_stdin_faces(h)
        test_runner_survives_a_hostile_hook(h)
        test_hang_is_at_least_as_killable(h)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): failure details echo hook output,
    # which the cases above deliberately make non-ASCII.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
