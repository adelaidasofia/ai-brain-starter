#!/usr/bin/env python3
"""auto-capture-public-ships must IMPORT and run to completion with no timezone set.

The hook shipped with

    TZ = ZoneInfo("America/user-local-tz")

an unsubstituted template placeholder that is not an IANA key. Nothing in the
repo ever substituted it, so `ZoneInfo()` raised ZoneInfoNotFoundError at MODULE
IMPORT -- before main() was ever reached. The SessionEnd hook exited 1 on every
machine it was ever installed on and captured exactly zero ships, for its whole
life. No test ran this hook at all, which is the only reason an import-time
crash could survive that long: there is no partial run to notice, no half-written
file, no wrong answer. The hook simply was not there.

So the load-bearing assertion here is the cheapest one imaginable -- run the
thing and look at the exit code. That is the test that was missing.

Covered:
  1. no $VAULT_TZ            -> exit 0, parseable JSON  (the original crash)
  2. $VAULT_TZ = the literal placeholder -> exit 0, degrades to local
  3. $VAULT_TZ = a real zone -> honoured  (skipped where no tz database exists)
  4. bypass flag             -> exit 0
  5. CLASS guard: no hook may call ZoneInfo() at module level, since that is
     precisely what turns a bad/absent zone into an import-time death. This repo
     is a TEMPLATE that gets synced into installs, so the placeholder class can
     come back the same way it arrived.
  6. negative control proving (5) actually trips -- a guard nobody has watched
     fail is not known to work.

Run: python3 hooks/test_auto_capture_ships_tz.py
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
HOOK = HOOKS / "auto-capture-public-ships.py"


def run_hook(cwd: Path, home: Path, **env_overrides) -> subprocess.CompletedProcess:
    """Run the hook hermetically: temp cwd, temp HOME, no ambient vault vars.

    HOME/USERPROFILE are redirected because the hook derives `DEV = Path.home()
    / "dev"` at import and WRITES into a resolved vault -- a test that inherited
    the real home could scan the user's repos and file into their real vault.
    """
    env = dict(os.environ)
    for k in ("VAULT_TZ", "VAULT_ROOT", "AUTO_CAPTURE_SHIPS_BYPASS", "TZ"):
        env.pop(k, None)
    env["HOME"] = str(home)          # POSIX Path.home()
    env["USERPROFILE"] = str(home)   # Windows Path.home()
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=str(cwd), env=env, input="{}",
        capture_output=True, text=True, timeout=60,
    )


def zoneinfo_calls_at_module_level(source: str) -> list[int]:
    """Line numbers of ZoneInfo(...) calls that execute at import time.

    Class bodies count as module level -- they run on import too. Anything
    inside a def/lambda does not: that is the whole point of the fix.
    """
    def is_zoneinfo(fn: ast.AST) -> bool:
        return ((isinstance(fn, ast.Name) and fn.id == "ZoneInfo")
                or (isinstance(fn, ast.Attribute) and fn.attr == "ZoneInfo"))

    hits: list[int] = []

    def walk(node: ast.AST, inside_func: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call) and is_zoneinfo(child.func) and not inside_func:
                hits.append(child.lineno)
            deeper = inside_func or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            walk(child, deeper)

    walk(ast.parse(source), False)
    return hits


def main() -> int:
    failures: list[str] = []
    notes: list[str] = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    def parsed(res):
        try:
            return json.loads(res.stdout)
        except Exception:
            return None

    def why(res):
        """Last stderr line -- the exception, without the traceback wall."""
        lines = [ln for ln in res.stderr.strip().splitlines() if ln.strip()]
        return lines[-1].strip() if lines else "(no stderr)"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = tmp / "home"
        cwd = tmp / "cwd"
        home.mkdir()
        cwd.mkdir()

        # -- 1. the original crash: no timezone configured anywhere ----------
        res = run_hook(cwd, home)
        check(res.returncode == 0,
              "hook exited %d with no $VAULT_TZ set -- this is the import-time "
              "ZoneInfoNotFoundError regression: %s" % (res.returncode, why(res)))
        check("ZoneInfoNotFoundError" not in res.stderr,
              "hook raised ZoneInfoNotFoundError: " + why(res))
        out = parsed(res)
        check(out is not None,
              "hook produced no parseable JSON on stdout: %r" % (res.stdout,))
        if out is not None:
            check(out.get("continue") is True,
                  "hook did not return continue=true: %r" % (out,))
            check(bool(out.get("tz")),
                  "hook did not report which zone it resolved; a silent "
                  "degradation is the class this fix exists to end: %r" % (out,))

        # -- 2. the literal placeholder must degrade, never kill the close ---
        res = run_hook(cwd, home, VAULT_TZ="America/user-local-tz")
        check(res.returncode == 0,
              "a bogus $VAULT_TZ killed the hook (exit %d); it must fall back to "
              "local time: %s" % (res.returncode, why(res)))

        # -- 3. a real zone is actually honoured ----------------------------
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("America/New_York")
            have_tzdb = True
        except Exception:
            have_tzdb = False
        if have_tzdb:
            res = run_hook(cwd, home, VAULT_TZ="America/New_York")
            out = parsed(res)
            check(res.returncode == 0 and out is not None
                  and out.get("tz") == "America/New_York",
                  "a valid $VAULT_TZ was not honoured: exit=%d out=%r"
                  % (res.returncode, res.stdout))
        else:
            notes.append("no tz database on this interpreter -- skipped the "
                         "valid-zone assertion (install `tzdata`)")

        # -- 4. bypass path ---------------------------------------------------
        res = run_hook(cwd, home, AUTO_CAPTURE_SHIPS_BYPASS="1")
        check(res.returncode == 0,
              "bypass path exited %d" % res.returncode)

        # -- 5. CLASS guard: no import-time ZoneInfo() in any hook -----------
        for path in sorted(HOOKS.glob("*.py")):
            try:
                lines = zoneinfo_calls_at_module_level(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue  # gate (a) py_compile owns syntax
            check(not lines,
                  "%s calls ZoneInfo() at module level (line%s %s). A bad or "
                  "absent zone then raises at IMPORT and the hook dies before "
                  "main(). Resolve it inside a function that falls back."
                  % (path.name, "" if len(lines) == 1 else "s",
                     ", ".join(str(n) for n in lines)))

        # -- 6. negative control: the guard above must actually trip ---------
        check(zoneinfo_calls_at_module_level(
                  'from zoneinfo import ZoneInfo\nTZ = ZoneInfo("America/X")\n') == [2],
              "the module-level ZoneInfo guard did not flag a planted "
              "violation -- it is vacuous and would never catch a regression")
        check(zoneinfo_calls_at_module_level(
                  'from zoneinfo import ZoneInfo\n'
                  'def tz():\n    return ZoneInfo("America/X")\n') == [],
              "the module-level ZoneInfo guard flagged a call inside a "
              "function -- it would forbid the very shape of the fix")

    for n in notes:
        print("note: " + n)
    if failures:
        print("FAILED - auto-capture-public-ships timezone:")
        for f in failures:
            print("  x " + f)
        return 1
    print("OK - auto-capture-public-ships imports and completes with no tz "
          "configured, degrades on a bogus zone, honours a real one, and no "
          "hook resolves a zone at import time")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
