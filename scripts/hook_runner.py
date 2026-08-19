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

    <interpreter> -X utf8 "<abs>/scripts/hook_runner.py" --fallback silent "<abs>/hooks/<hook>.py"

(`-X utf8` is PEP 540 UTF-8 Mode: the hook now runs IN this interpreter, so a
PYTHONUTF8 in a child env has nothing to act on. Without it a hook doing
`open(path).read()` on a UTF-8 file decodes with the console code page and
raises UnicodeDecodeError on the unmapped cp1252 bytes — and UnicodeDecodeError
is a ValueError, so it slips past the `except OSError` in a dozen shipped hooks
and gets masked to "continue". Carried forward from PR #446.)

and this wrapper reproduces the POSIX masking semantics in Python:

  - target script missing        -> print fallback JSON, exit 0 (the [ -f ] guard)
  - target exits 0               -> forward its stdout verbatim, exit 0
  - target exits 2 (a BLOCK)     -> forward stderr, exit 2 (blocking semantics
                                    preserved — exit 2 is Claude Code's
                                    intentional block signal, never masked)
  - target exits anything else,
    crashes, or can't launch     -> print fallback JSON, exit 0 (the 2>/dev/null
                                    + || echo masking)
  - target HANGS                 -> after ABS_HOOK_TIMEOUT seconds (45 by
                                    default) the fallback JSON is emitted and
                                    the process hard-exits 0. See "A HANG IS
                                    BOUNDED" below.

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
  * a FORK whose child   -> that child falls out of exec() and would otherwise
    does NOT hard-exit      re-run the whole reporting path, emitting the
                            capture a SECOND time: two concatenated JSON
                            documents, which Claude Code cannot parse. The
                            finally in _execute hard-exits any process that is
                            not the owner before a single byte is reported.
  * stdout / stderr      -> captured at the FILE-DESCRIPTOR level (dup2 onto two
                            temp files), which is what subprocess's
                            capture_output did: output written by a grandchild
                            that inherited fd 1, or by a raw os.write(1, ...),
                            is captured too. The captured BYTES are forwarded
                            unchanged, so no locale decode sits in the path
                            (that was the cp1252 crash class of #313).
  * DESCRIPTOR HYGIENE   -> subprocess passed close_fds=True, so the old hook
                            saw exactly [0,1,2]. In-process there is no such
                            boundary, and the two capture files plus the two
                            saved stdio dups landed on fds 3-6 — precisely the
                            range `os.close(3)` and every daemonize recipe
                            clears. A hook doing that made the restore raise and
                            the verdict VANISH (measured: the pre-fix runner
                            emitted {"continue":true}, this one emitted nothing
                            at all). Every descriptor this file owns is now
                            parked above _HIGH_FD_BASE and marked
                            non-inheritable, a capture the hook closed is
                            re-acquired from the live stdio fd, and no single
                            restore step can raise.
  * cwd, sys.path, sys.argv, os.environ, sys.stdin/stdout/stderr, signal
    handlers, sys.modules["__main__"]
                         -> snapshotted before and restored in a finally, each
                            step INDIVIDUALLY tolerant of failure: a raise
                            anywhere in the restore used to escape _execute and
                            replace a real verdict with the fallback (measured
                            on a hook that swaps sys.modules["signal"]). The
                            reporting path in main() is wrapped for the same
                            reason — a raise there produced exit 1 plus a
                            visible traceback, exactly the class this file
                            exists to prevent.
  * teardown noise       -> main() ends in os._exit, so an atexit handler or a
                            thread waking after the verdict cannot append bytes
                            to it. All the runner's own output goes out through
                            unbuffered os.write, so nothing is lost by skipping
                            interpreter shutdown.

A HANG IS BOUNDED, AND THE RUNNER STAYS KILLABLE.
  * BOUNDED: a watchdog thread emits the fallback JSON and hard-exits 0 after
    ABS_HOOK_TIMEOUT seconds (default 45, set to 0 to disable). This replaces
    the `subprocess.run(..., timeout=45)` the child-process shape carried; the
    installer additionally writes `"timeout": 45` on every Windows hook entry so
    Claude Code bounds it from the outside too.
  * KILLABLE: the hook IS this process, so a hook that installs ANY SIGTERM
    handler — an ordinary non-exiting cleanup handler, not just a hostile
    SIG_IGN — made the runner survive its own SIGTERM (measured: the pre-fix
    runner died 0.0 s after terminate(), this one was still alive 6 s later).
    signal.signal is therefore shimmed for the hook's window: a handler the hook
    installs for a fatal signal still runs, and then the process dies the way an
    unhandled signal would have. Restored the moment the hook returns.

KNOWN, ACCEPTED DIVERGENCES (all narrower than the bug they replace):
  * A hard crash of the interpreter itself (segfault via ctypes, os.abort) now
    takes the runner down instead of being masked. No stdlib-Python hook can
    reach that state.
  * A hook that closes EVERY descriptor in its own process without forking
    (os.closerange(3, RLIMIT_NOFILE)) destroys the runner's only handle on the
    real stdout, and its verdict cannot be reported. Parking our descriptors
    high covers the idioms that appear in practice (os.close(3), closerange over
    a small range); the daemonize recipe that closes everything forks first, and
    the fork guard above hard-exits that child untouched.
  * A hook that reaches past the shim — `import _signal` and calling its signal
    directly — can still make the runner ignore SIGTERM for the hook's window.
    Nothing in this repo does; the outer `"timeout"` still bounds it.
  * sys.stdin is replaced with the payload; a hook reading raw fd 0 (nothing in
    this repo does) sees the already-drained descriptor.
"""

from __future__ import annotations

import os
import sys

try:  # captured ONCE, so a hook that swaps sys.modules["signal"] cannot steer
    import signal as _signal
except ImportError:  # pragma: no cover — signal is always present
    _signal = None  # type: ignore[assignment]

# Real callables, bound before any hook can touch the module objects they live
# on. Every internal use goes through these, never through a fresh `import`.
_REAL_OS_EXIT = os._exit
_REAL_SIGNAL = getattr(_signal, "signal", None)
_REAL_GETSIGNAL = getattr(_signal, "getsignal", None)

FALLBACKS = {
    "silent": '{"continue":true,"suppressOutput":true}',
    "allow": ('{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
              '"permissionDecision":"allow"}}'),
}

# Claude Code's intentional-BLOCK signal. Never masked; see the docstring.
BLOCK_EXIT = 2

# Seconds a hook may run before the watchdog masks it. Mirrors the bound the
# child-process shape carried (subprocess.run(..., timeout=45), PR #446).
DEFAULT_TIMEOUT = 45.0

# Descriptors this file owns are parked at or above this number. 3-6 is the
# range `os.close(3)` and every daemonize recipe clears; 50 is comfortably
# clear of it and comfortably under any RLIMIT_NOFILE a Python can start with.
_HIGH_FD_BASE = 50

# Set the moment main() begins reporting, so a watchdog firing in that window
# cannot write a second verdict on top of the real one.
_REPORTED = [False]


def _quietly(fn, *args) -> None:
    """Run a restore step. It may NOT raise: a restore that escapes replaces a
    real verdict with the fallback, which is the failure this file prevents."""
    try:
        fn(*args)
    except BaseException:  # noqa: BLE001 — see the docstring
        pass


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
    except BaseException:  # noqa: BLE001 — emission is best-effort, never fatal
        pass


def _fd_is_free(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    except BaseException:  # noqa: BLE001
        return False
    return False


def _park_fd_high(fd: int) -> int:
    """Move a descriptor WE own out of the low range a hook may close.

    The old runner handed the hook exactly [0,1,2] because subprocess passed
    close_fds=True. In-process there is no such boundary, so the two capture
    files and the two saved stdio dups landed on fds 3-6 — precisely the range
    `os.close(3)` and every daemonize recipe clears.

    Returns the new descriptor, or the original one when no high slot could be
    taken — degrading to the old behaviour rather than losing the descriptor."""
    if fd < 0:
        return fd
    for target in range(_HIGH_FD_BASE, _HIGH_FD_BASE + 32):
        if target == fd:
            return fd
        if not _fd_is_free(target):
            continue
        try:
            try:
                os.dup2(fd, target, inheritable=False)
            except TypeError:  # pragma: no cover — very old/odd builds
                os.dup2(fd, target)
        except (OSError, ValueError):
            continue
        try:
            os.close(fd)
        except OSError:
            pass
        return target
    return fd


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
            # Out of the 3-6 range a hook may close, and out of reach of any
            # grandchild it starts.
            self.fd = _park_fd_high(self.fd)
            return
        import tempfile  # slow path only: the fast one could not make a file
        self._obj = tempfile.TemporaryFile()
        # NOT parked: the file object owns this descriptor and closes it itself.
        self.fd = self._obj.fileno()

    def heal_from(self, live_fd: int) -> None:
        """Re-acquire the buffer if the hook closed our descriptor.

        dup2 gave fd 1 (or 2) a SECOND reference to the same open file, so the
        hook's output is still there after `os.close(<our fd>)`; only our handle
        on it is gone. Without this the capture reads back empty and a clean
        hook's stdout disappears.

        Accepted caveat: if the hook closed our descriptor AND reopened
        something else at that number, fstat succeeds and we read that instead.
        Parking above _HIGH_FD_BASE means a hook would have to open ~50 files
        before it could land there."""
        if self._obj is not None:
            return
        if not _fd_is_free(self.fd):
            return
        try:
            self.fd = _park_fd_high(os.dup(live_fd))
        except OSError:
            pass

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


def _hook_timeout() -> float:
    """Seconds before the watchdog masks a hung hook. 0 (or junk) disables it."""
    raw = os.environ.get("ABS_HOOK_TIMEOUT")
    if raw is None:
        return DEFAULT_TIMEOUT
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _arm_watchdog(seconds: float, out_fd: int, payload: bytes) -> None:
    """Bound a hang: emit the fallback on `out_fd` and hard-exit 0.

    _thread, not threading: both are resident at interpreter start, but
    `import threading` still costs ~0.6 ms of module execution and this file's
    entire reason to exist is the milliseconds. A raw thread is also never
    joined at shutdown, which is what we want — the runner hard-exits and the
    watchdog goes with it."""
    if seconds <= 0:
        return
    import _thread
    import time

    def _fire() -> None:
        try:
            time.sleep(seconds)
        except BaseException:  # noqa: BLE001
            return
        if _REPORTED[0]:
            return
        _write_bytes(out_fd, payload)
        _REAL_OS_EXIT(0)

    try:
        _thread.start_new_thread(_fire, ())
    except BaseException:  # noqa: BLE001 — no watchdog is worse, not fatal
        pass


def _fatal_signums() -> frozenset:
    """Signals whose DEFAULT action ends the process. A hook may handle these;
    it may not make the runner outlive them."""
    if _signal is None:
        return frozenset()
    out = set()
    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGBREAK"):
        sig = getattr(_signal, name, None)
        if sig is None:
            continue
        try:
            out.add(int(sig))
        except (TypeError, ValueError):
            continue
    return frozenset(out)


_FATAL_SIGNUMS = _fatal_signums()


def _die_from_signal(signum) -> None:
    """End the process the way an UNHANDLED `signum` would have.

    Re-raising under SIG_DFL keeps the wait status a caller sees identical to
    the pre-fix runner's (-SIGTERM, not exit 143). os.kill has no such semantics
    on Windows, so the explicit exit below is the floor, not the fallback."""
    try:
        if _REAL_SIGNAL is not None and _signal is not None:
            _REAL_SIGNAL(signum, _signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    except BaseException:  # noqa: BLE001
        pass
    try:
        code = 128 + int(signum)
    except (TypeError, ValueError):
        code = 1
    _REAL_OS_EXIT(code)


def _install_killability_shim(hook_view: dict):
    """Let the hook handle fatal signals; do not let it outlive them.

    The hook IS the runner now, so `signal.signal(SIGTERM, cleanup)` — an
    ordinary handler, not a hostile one — silently made the runner unkillable
    for the hook's whole window. The shim wraps any fatal-signal handler the
    hook installs: the hook's own function runs first (its cleanup still
    happens), then the process dies exactly as it did when the hook was a child.

    Returns a callable that puts the real signal-module functions back."""
    if _signal is None or _REAL_SIGNAL is None:
        return lambda: None

    def _guarded_signal(signalnum, handler):
        try:
            num = int(signalnum)
        except (TypeError, ValueError):
            return _REAL_SIGNAL(signalnum, handler)
        if num not in _FATAL_SIGNUMS or handler is _signal.SIG_DFL:
            return _REAL_SIGNAL(signalnum, handler)

        def _stay_killable(sig, frame, _handler=handler):
            # The hook's cleanup runs first. If it raises (SystemExit is the
            # common one) the exception unwinds the main thread and _execute
            # reports it — which also ends the process promptly, so only the
            # "handler returned normally" path needs the explicit death below.
            if callable(_handler):
                _handler(sig, frame)
            _die_from_signal(sig)

        previous = hook_view.get(num)
        if previous is None and _REAL_GETSIGNAL is not None:
            try:
                previous = _REAL_GETSIGNAL(signalnum)
            except (OSError, ValueError):
                previous = None
        hook_view[num] = handler
        _REAL_SIGNAL(signalnum, _stay_killable)
        return previous

    def _guarded_getsignal(signalnum):
        # The hook must see what IT installed, not our wrapper.
        try:
            num = int(signalnum)
        except (TypeError, ValueError):
            num = None
        if num is not None and num in hook_view:
            return hook_view[num]
        if _REAL_GETSIGNAL is None:
            return None
        return _REAL_GETSIGNAL(signalnum)

    _signal.signal = _guarded_signal  # type: ignore[assignment]
    _signal.getsignal = _guarded_getsignal  # type: ignore[assignment]

    def _restore() -> None:
        _signal.signal = _REAL_SIGNAL  # type: ignore[assignment]
        if _REAL_GETSIGNAL is not None:
            _signal.getsignal = _REAL_GETSIGNAL  # type: ignore[assignment]

    return _restore


def _snapshot_signals() -> dict:
    if _signal is None or _REAL_GETSIGNAL is None:
        return {}
    snap = {}
    try:
        sigs = _signal.valid_signals()
    except (AttributeError, ValueError):  # pragma: no cover
        return snap
    for sig in sigs:
        try:
            snap[sig] = _REAL_GETSIGNAL(sig)
        except (OSError, ValueError):
            continue
    return snap


def _restore_signals(snap: dict) -> None:
    if not snap or _REAL_SIGNAL is None or _REAL_GETSIGNAL is None:
        return
    for sig, handler in snap.items():
        # None means "not set from Python" — signal.signal cannot express that,
        # so leave it rather than raise.
        if handler is None:
            continue
        try:
            if _REAL_GETSIGNAL(sig) is not handler:
                _REAL_SIGNAL(sig, handler)
        except BaseException:  # noqa: BLE001 — never replace a verdict
            continue


def _restore_path(path: list) -> None:
    try:
        sys.path[:] = path
    except (TypeError, AttributeError):
        sys.path = list(path)


def _restore_main(main_module) -> None:
    if main_module is not None:
        sys.modules["__main__"] = main_module
    else:
        sys.modules.pop("__main__", None)


def _restore_environ(env: dict) -> None:
    if os.environ != env:
        os.environ.clear()
        os.environ.update(env)


def _restore_cwd(cwd) -> None:
    if cwd is None:
        return
    if os.getcwd() != cwd:
        os.chdir(cwd)


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


def _execute(script: str, extra: list[str], payload: bytes,
             fallback: bytes) -> tuple[int, bytes, bytes]:
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
        "signals": _snapshot_signals(),
    }
    try:
        saved["cwd"] = os.getcwd()
    except OSError:
        saved["cwd"] = None

    owner_pid = os.getpid()

    def _guarded_os_exit(status: int = 0) -> None:
        # PID-scoped: a forked/multiprocessing child must still hard-exit, or
        # its teardown would suddenly start running the parent's atexit hooks.
        if os.getpid() != owner_pid:
            _REAL_OS_EXIT(status)
        raise SystemExit(status)

    out_f = _Capture()
    err_f = _Capture()
    dup_out = dup_err = None
    unshim = None
    rc = 1
    try:
        try:
            try:
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                sys.stderr.flush()
            except Exception:  # noqa: BLE001
                pass
            # Parked high for the same reason the captures are: these two are
            # the ONLY handle on the caller's real stdout/stderr, and 3-6 is
            # the range a daemonizing hook clears.
            dup_out = _park_fd_high(os.dup(1))
            dup_err = _park_fd_high(os.dup(2))
            os.dup2(out_f.fd, 1)
            os.dup2(err_f.fd, 2)

            # Armed AFTER the real stdout is safely dup'ed: the watchdog writes
            # the fallback there, never into a capture nobody will read.
            _arm_watchdog(_hook_timeout(), dup_out, fallback)

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
            unshim = _install_killability_shim({})
            # Carried forward from the child-process shape (PR #446): any
            # interpreter the hook itself starts runs in UTF-8 mode, so a vault
            # path or data file outside the console code page cannot crash it.
            # The runner's OWN utf-8 mode comes from `-X utf8` on the launcher.
            _quietly(os.environ.setdefault, "PYTHONUTF8", "1")

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
            # A FORKED CHILD STOPS HERE. It must never reach the reporting path:
            # it would flush the same capture into the same pipe a second time,
            # and two concatenated JSON documents parse as neither.
            if os.getpid() != owner_pid:
                for _stream in (sys.stdout, sys.stderr):
                    _quietly(_stream.flush)
                _REAL_OS_EXIT(rc if isinstance(rc, int) else 0)

            _quietly(setattr, os, "_exit", _REAL_OS_EXIT)
            if unshim is not None:
                _quietly(unshim)
            # Flush BEFORE the descriptors go back, or buffered hook output
            # lands on the real stream instead of in the capture.
            for stream in (sys.stdout, sys.stderr):
                _quietly(stream.flush)
            # fd 1/2 still reference the capture files even if the hook closed
            # OUR handles on them, so re-acquire before restoring.
            _quietly(out_f.heal_from, 1)
            _quietly(err_f.heal_from, 2)
            # EVERY step below is individually tolerant: one raise here used to
            # escape _execute and replace the hook's real verdict with the
            # fallback (a guard's answer silently rewritten to "continue").
            if dup_out is not None:
                _quietly(os.dup2, dup_out, 1)
                _quietly(os.close, dup_out)
            if dup_err is not None:
                _quietly(os.dup2, dup_err, 2)
                _quietly(os.close, dup_err)
            _quietly(setattr, sys, "argv", saved["argv"])
            _quietly(_restore_path, saved["path"])
            _quietly(setattr, sys, "stdin", saved["stdin"])
            _quietly(setattr, sys, "stdout", saved["stdout"])
            _quietly(setattr, sys, "stderr", saved["stderr"])
            _quietly(_restore_main, saved["main"])
            _quietly(_restore_environ, saved["env"])
            _quietly(_restore_cwd, saved["cwd"])
            _quietly(_restore_signals, saved["signals"])

        out = out_f.read_all()
        err = err_f.read_all()
    finally:
        _quietly(out_f.close)
        _quietly(err_f.close)
    return rc, out, err


def main(argv: list[str]) -> int:
    args = list(argv)
    fallback = FALLBACKS["silent"]
    if len(args) >= 2 and args[0] == "--fallback":
        fallback = FALLBACKS.get(args[1], FALLBACKS["silent"])
        args = args[2:]
    fallback_bytes = fallback.encode("utf-8") + b"\n"
    if not args:
        _REPORTED[0] = True
        _write_bytes(1, fallback_bytes)
        return 0
    script, extra = args[0], args[1:]

    # Missing target = the [ -f ] guard: silent fallback, exit 0. Checked here
    # explicitly because CPython exits 2 for "can't open file", which would
    # otherwise masquerade as an intentional block below.
    if not os.path.isfile(script):
        _REPORTED[0] = True
        _write_bytes(1, fallback_bytes)
        return 0

    payload = _read_stdin_bytes()

    # EVERYTHING from here down is inside the mask, including the reporting
    # itself. A raise in the exit-code decision or in _write_bytes used to
    # escape as exit 1 plus a traceback on the real stderr — a visible hook
    # error, the one outcome this file exists to prevent.
    try:
        rc, out, err = _execute(script, extra, payload, fallback_bytes)
        _REPORTED[0] = True
        if rc == 0:
            # Forward exactly — an empty stdout is a valid no-op for Claude Code.
            _write_bytes(1, out)
            return 0
        if rc == BLOCK_EXIT:
            # Intentional block: stderr carries the reason; must propagate.
            _write_bytes(2, err)
            _write_bytes(1, out)
            return BLOCK_EXIT
    except BaseException:  # noqa: BLE001 — "could not launch" is masked, as before
        pass
    _REPORTED[0] = True
    _write_bytes(1, fallback_bytes)
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
    _rc = main(sys.argv[1:])
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.flush()
        except Exception:  # noqa: BLE001
            pass
    # HARD exit, not sys.exit: the protocol JSON has already gone out through
    # unbuffered os.write, and anything the hook scheduled for teardown — an
    # atexit handler, a non-daemon thread waking 400 ms later — would otherwise
    # append its own bytes AFTER the verdict. Measured: a hook registering an
    # atexit writer and exiting 1 produced the fallback JSON plus trailing
    # garbage, where the pre-fix runner produced the JSON alone.
    _REAL_OS_EXIT(_rc)
