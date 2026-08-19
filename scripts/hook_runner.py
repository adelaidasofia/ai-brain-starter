#!/usr/bin/env python3
"""hook_runner.py — portable failure-masking wrapper for Claude Code hooks.

WHY THIS EXISTS: hooks.json masks hook failures with POSIX shell forms like

    python3 <script> 2>/dev/null || echo '{"continue":true,"suppressOutput":true}'
    [ -f <script> ] && python3 <script> || true

On native Windows, Claude Code executes hook commands under PowerShell 5.1,
PowerShell 7, cmd.exe, or Git Bash depending on version and configuration —
and NO single shell one-liner parses in all four (`||` is a parse error in
PowerShell 5.1, `[ -f ]` and `/dev/null` are POSIX-only, a quoted first token
is a string literal in PowerShell). The result before this wrapper existed:
every hook errored visibly on every prompt for Windows users.

The only command shape all four shells parse identically is a bare PATH
command followed by quoted arguments. So on Windows the installer
(install-hooks-user-level.py) wires every hook as:

    <interpreter> "<abs>/scripts/hook_runner.py" --fallback silent "<abs>/hooks/<hook>.py"

and this wrapper reproduces the POSIX masking semantics in Python:

  - target script missing        -> print fallback JSON, exit 0 (the [ -f ] guard)
  - target exits 0               -> forward its stdout verbatim, exit 0
  - target exits 2 (a BLOCK)     -> forward stderr, exit 2 (blocking semantics
                                    preserved — exit 2 is Claude Code's
                                    intentional block signal, never masked)
  - target exits anything else,
    crashes, or can't launch     -> print fallback JSON, exit 0 (the 2>/dev/null
                                    + || echo masking)

Fallback forms (--fallback):
  silent  {"continue": true, "suppressOutput": true}          (default)
  allow   {"hookSpecificOutput": {"hookEventName": "PreToolUse",
           "permissionDecision": "allow"}}   — for PreToolUse guards that must
           fail open rather than wedge every tool call.

stdin (the hook payload JSON) is passed through to the target unchanged.
Stdlib-only; usable on macOS/Linux too, though POSIX installs keep their
original shell forms to avoid churning working configs.

────────────────────────────────────────────────────────────────────────────
INTERPRETER-COUNT CONTRACT: ONE PYTHON PROCESS PER HOOK. NEVER TWO. (MYC-3877)
────────────────────────────────────────────────────────────────────────────
This wrapper USED TO run the hook with

    subprocess.run([sys.executable, script, *extra], ...)

which made the chain per hook  shell -> launcher -> interpreter #1 (this file)
-> interpreter #2 (the hook).  A second CPython startup costs ~92 ms on a
2019-class Windows laptop with live AV, measured on every single tool call
(PreToolUse alone ran ~1.6 s of pure hook latency). The wrapper's own work is
microseconds; the process was the entire bill.

The hook is now COMPILED AND EXECUTED IN THIS PROCESS (_execute below), as
`__main__`, so `if __name__ == "__main__":` still fires and argv/stdin/sys.path
look exactly like `python <script>`. The observable contract above is
unchanged — same exit codes, same two streams, same masking.

DO NOT REINTRODUCE A CHILD INTERPRETER. scripts/test_hook_runner_single_interpreter.py
fails on any `subprocess`/`os.exec*`/`os.spawn*`/`sys.executable` reference in
this file AND asserts behaviorally that the hook reports THIS process's pid.

Why in-process and not exec-replace (os.execv): exec throws this file's code
away, so a hook that exits 1 or crashes could no longer be masked — and masking
is the entire reason this wrapper exists. Exec drops the interpreter count to
one but drops the contract with it.

HOW ISOLATION IS PRESERVED WITHOUT A PROCESS BOUNDARY. This process runs exactly
ONE hook and then exits, so "corrupting a later hook" is not reachable; what has
to hold is that the RUNNER still reports the right thing after a hostile hook.

  * unhandled exception  -> caught, traceback goes to the CAPTURED stderr
                            (discarded by the mask, exactly as before), exit 1
                            -> masked.
  * sys.exit(N)          -> SystemExit caught, N routed through the same
                            protocol. A non-int argument prints and exits 1,
                            matching CPython.
  * os._exit(N)          -> os._exit is swapped for a guard that raises
                            SystemExit instead, so a hard exit is masked like
                            any other. The guard is PID-scoped: inside a fork
                            or a multiprocessing child it calls the real
                            os._exit, so nothing about child-process teardown
                            changes.
  * stdout / stderr      -> captured at the FILE-DESCRIPTOR level (dup2 onto two
                            temp files), which is what subprocess's
                            capture_output did: output written by a grandchild
                            that inherited fd 1, or by a raw os.write(1, ...),
                            is captured too. The captured BYTES are forwarded
                            unchanged, so no locale decode sits in the path
                            (that was the cp1252 crash class of #313).
  * cwd, sys.path, sys.argv, os.environ, sys.stdin/stdout/stderr, signal
    handlers, sys.modules["__main__"]
                         -> snapshotted before and restored in a finally, so
                            the runner's own reporting cannot be steered.
  * a hook that HANGS    -> strictly MORE killable than before. It is this
                            process now, so the signal that kills the runner
                            kills the hook; the old shape left the grandchild
                            running as an orphan holding the pipe.

KNOWN, ACCEPTED DIVERGENCES (all narrower than the bug they replace):
  * A hard crash of the interpreter itself (segfault via ctypes, os.abort) now
    takes the runner down instead of being masked. No stdlib-Python hook can
    reach that state.
  * A hook that installs an ignore-SIGTERM handler AND then hangs makes the
    runner ignore SIGTERM for that window too (before, the runner died and the
    hook was orphaned). Handlers are restored the moment the hook returns.
  * An atexit handler registered by the hook runs after the fds are restored,
    so its output reaches the real stream instead of the capture buffer.
  * sys.stdin is replaced with the payload; a hook reading raw fd 0 (nothing in
    this repo does) sees the already-drained descriptor.
"""

from __future__ import annotations

import os
import sys

FALLBACKS = {
    "silent": '{"continue":true,"suppressOutput":true}',
    "allow": ('{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
              '"permissionDecision":"allow"}}'),
}

# Claude Code's intentional-BLOCK signal. Never masked; see the docstring.
BLOCK_EXIT = 2


def _write_bytes(fd: int, data: bytes) -> None:
    """Put bytes on a real descriptor, tolerating short writes and a closed fd.

    Deliberately not sys.stdout.write: a hostile hook may have closed or
    replaced the stream objects, and the fallback JSON going missing is the one
    outcome this wrapper must never produce."""
    if not data:
        return
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    except (OSError, ValueError):
        pass


class _Capture:
    """A private scratch file to dup2 stdout/stderr onto, built from `os` alone.

    NOT tempfile: importing it drags in 25 modules (shutil, random, zlib, bz2,
    lzma, zstd) for ~4 ms on a warm Mac and more on the Windows box this change
    exists to speed up — a quarter of the saving, spent to hold two buffers.
    tempfile is still the fallback if the fast path cannot make a file, so a
    machine with an odd TMPDIR degrades instead of losing its hooks.

    Safety is the same as tempfile's: O_CREAT|O_EXCL (never opens an existing
    path, so a pre-planted symlink fails), O_NOFOLLOW where it exists, 0600, and
    a random name. The file is unlinked immediately on POSIX and opened
    O_TEMPORARY on Windows, so nothing survives the process either way."""

    __slots__ = ("fd", "_path", "_obj")

    def __init__(self) -> None:
        self.fd = -1
        self._path = None
        self._obj = None
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        for name in ("O_BINARY", "O_NOFOLLOW", "O_TEMPORARY", "O_NOINHERIT"):
            flags |= getattr(os, name, 0)
        base = (os.environ.get("TMPDIR") or os.environ.get("TEMP")
                or os.environ.get("TMP") or ("." if os.name == "nt" else "/tmp"))
        for _ in range(4):
            path = os.path.join(
                base, "abs-hook-%d-%s.tmp" % (os.getpid(), os.urandom(6).hex()))
            try:
                self.fd = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            except OSError:
                break
            if os.name == "nt":
                # O_TEMPORARY already deletes on close; if it was unavailable
                # the path has to be removed by hand at the end.
                self._path = None if getattr(os, "O_TEMPORARY", 0) else path
            else:
                try:
                    os.unlink(path)  # anonymous from here on
                except OSError:
                    self._path = path
            return
        import tempfile  # slow path only: the fast one could not make a file
        self._obj = tempfile.TemporaryFile()
        self.fd = self._obj.fileno()

    def read_all(self) -> bytes:
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
        except OSError:
            return b""
        chunks = []
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        if self._obj is not None:
            try:
                self._obj.close()
            except Exception:  # noqa: BLE001
                pass
        elif self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
        if self._path:
            try:
                os.unlink(self._path)
            except OSError:
                pass


def _snapshot_signals() -> dict:
    try:
        import signal
    except ImportError:  # pragma: no cover — signal is always present
        return {}
    snap = {}
    try:
        sigs = signal.valid_signals()
    except (AttributeError, ValueError):  # pragma: no cover
        return snap
    for sig in sigs:
        try:
            snap[sig] = signal.getsignal(sig)
        except (OSError, ValueError):
            continue
    return snap


def _restore_signals(snap: dict) -> None:
    if not snap:
        return
    try:
        import signal
    except ImportError:  # pragma: no cover
        return
    for sig, handler in snap.items():
        # None means "not set from Python" — signal.signal cannot express that,
        # so leave it rather than raise.
        if handler is None:
            continue
        try:
            if signal.getsignal(sig) is not handler:
                signal.signal(sig, handler)
        except (OSError, ValueError, RuntimeError, TypeError):
            continue


def _read_stdin_bytes() -> bytes:
    """Drain the payload. Draining is part of the contract: the writer on the
    other end may block on a full pipe if nobody reads, and the pre-fix wrapper
    always read stdin before handing it to the child."""
    try:
        buf = getattr(sys.stdin, "buffer", None)
        if buf is not None:
            return buf.read() or b""
    except Exception:  # noqa: BLE001 — any stdin failure means "no payload"
        pass
    try:
        return (sys.stdin.read() or "").encode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return b""


def _code_from_system_exit(exc: BaseException) -> int:
    """CPython's own rule for what sys.exit(x) means."""
    code = getattr(exc, "code", None)
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    try:
        print(code, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    return 1


def _execute(script: str, extra: list[str], payload: bytes) -> tuple[int, bytes, bytes]:
    """Run `script` as __main__ IN THIS PROCESS. Returns (rc, stdout, stderr).

    Raises only if the hook could not be set up at all (unreadable file, no
    descriptors to redirect) — main() masks that exactly like the old
    "subprocess could not launch" branch."""
    import builtins
    import io
    import types

    with open(script, "rb") as fh:
        source = fh.read()
    # compile() on BYTES honours a PEP 263 encoding cookie, same as CPython
    # reading the file itself. A SyntaxError here is a broken hook: it must be
    # masked, not raised, so it is handled with the runtime failures below.
    try:
        code = compile(source, script, "exec")
    except (SyntaxError, ValueError):
        return 1, b"", b""

    saved = {
        "argv": sys.argv,
        "path": list(sys.path),
        "env": dict(os.environ),
        "stdin": sys.stdin,
        "stdout": sys.stdout,
        "stderr": sys.stderr,
        "main": sys.modules.get("__main__"),
        "os_exit": os._exit,
        "signals": _snapshot_signals(),
    }
    try:
        saved["cwd"] = os.getcwd()
    except OSError:
        saved["cwd"] = None

    owner_pid = os.getpid()
    real_os_exit = saved["os_exit"]

    def _guarded_os_exit(status: int = 0) -> None:
        # PID-scoped: a forked/multiprocessing child must still hard-exit, or
        # its teardown would suddenly start running the parent's atexit hooks.
        if os.getpid() != owner_pid:
            real_os_exit(status)
        raise SystemExit(status)

    out_f = _Capture()
    err_f = _Capture()
    dup_out = dup_err = None
    rc = 1
    try:
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        dup_out = os.dup(1)
        dup_err = os.dup(2)
        os.dup2(out_f.fd, 1)
        os.dup2(err_f.fd, 2)

        sys.argv = [script, *extra]
        # CPython puts the script's own directory on sys.path[0]; hooks that
        # `import _lib.x` rely on that shape even when they also insert it.
        sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
        sys.stdin = io.TextIOWrapper(io.BytesIO(payload),
                                     encoding="utf-8", errors="replace")
        module = types.ModuleType("__main__")
        module.__file__ = script
        module.__builtins__ = builtins
        sys.modules["__main__"] = module
        os._exit = _guarded_os_exit  # type: ignore[assignment]

        try:
            exec(code, module.__dict__)  # noqa: S102 — running a hook IS the job
            rc = 0
        except SystemExit as exc:
            rc = _code_from_system_exit(exc)
        except BaseException:  # noqa: BLE001 — a crashing hook must be masked
            try:
                import traceback  # lazy: ~6 ms of import, crash path only
                traceback.print_exc()
            except Exception:  # noqa: BLE001
                pass
            rc = 1
    finally:
        os._exit = real_os_exit  # type: ignore[assignment]
        # Flush BEFORE the descriptors go back, or buffered hook output lands on
        # the real stream instead of in the capture.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass
        if dup_out is not None:
            try:
                os.dup2(dup_out, 1)
            finally:
                os.close(dup_out)
        if dup_err is not None:
            try:
                os.dup2(dup_err, 2)
            finally:
                os.close(dup_err)
        sys.argv = saved["argv"]
        sys.path[:] = saved["path"]
        sys.stdin = saved["stdin"]
        sys.stdout = saved["stdout"]
        sys.stderr = saved["stderr"]
        if saved["main"] is not None:
            sys.modules["__main__"] = saved["main"]
        else:
            sys.modules.pop("__main__", None)
        if os.environ != saved["env"]:
            os.environ.clear()
            os.environ.update(saved["env"])
        if saved["cwd"] is not None:
            try:
                if os.getcwd() != saved["cwd"]:
                    os.chdir(saved["cwd"])
            except OSError:
                pass
        _restore_signals(saved["signals"])

    out = out_f.read_all()
    err = err_f.read_all()
    out_f.close()
    err_f.close()
    return rc, out, err


def main(argv: list[str]) -> int:
    args = list(argv)
    fallback = FALLBACKS["silent"]
    if len(args) >= 2 and args[0] == "--fallback":
        fallback = FALLBACKS.get(args[1], FALLBACKS["silent"])
        args = args[2:]
    if not args:
        _write_bytes(1, fallback.encode("utf-8") + b"\n")
        return 0
    script, extra = args[0], args[1:]

    # Missing target = the [ -f ] guard: silent fallback, exit 0. Checked here
    # explicitly because CPython exits 2 for "can't open file", which would
    # otherwise masquerade as an intentional block below.
    if not os.path.isfile(script):
        _write_bytes(1, fallback.encode("utf-8") + b"\n")
        return 0

    payload = _read_stdin_bytes()

    try:
        rc, out, err = _execute(script, extra, payload)
    except BaseException:  # noqa: BLE001 — "could not launch" is masked, as before
        _write_bytes(1, fallback.encode("utf-8") + b"\n")
        return 0

    if rc == 0:
        # Forward exactly — an empty stdout is a valid no-op for Claude Code.
        _write_bytes(1, out)
        return 0
    if rc == BLOCK_EXIT:
        # Intentional block: stderr carries the reason; must propagate.
        _write_bytes(2, err)
        _write_bytes(1, out)
        return BLOCK_EXIT
    _write_bytes(1, fallback.encode("utf-8") + b"\n")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print
    # can't crash. Forwarded hook output goes out as raw bytes (_write_bytes),
    # so no locale decode sits anywhere in this path.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
