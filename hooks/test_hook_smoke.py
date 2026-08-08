#!/usr/bin/env python3
"""Every hook must actually RUN. Nothing asserted that, and two never did.

THE CLASS (MYC-3537)
  A hook that raises at module import is the maximally silent failure. There is
  no partial run, no half-written file, no wrong answer -- the hook is simply
  not there, while the install continues to report it as present. Nothing in the
  suite executed most hooks, so the cheapest possible assertion (run it, look at
  the exit code) was the one nobody was making.

  Found this way, both dead on arrival for their entire lives:

    auto-capture-public-ships.py   ZoneInfo("America/user-local-tz"), an
                                   unsubstituted template placeholder. Crashed
                                   on EVERY platform. (#420)
    scan-prior-sessions-for-secrets.py
                                   `import fcntl` at module scope. fcntl is
                                   POSIX-only, so this SECRET SCANNER crashed at
                                   import on every Windows install -- and the
                                   file's own docstring says it supports Windows.

TWO GATES, BECAUSE ONE CANNOT SEE THE OTHER'S BUG
  1. SMOKE (dynamic): run each hook hermetically and fail on a Python
     traceback. Catches the tz class -- a crash on the platform running CI.
  2. PLATFORM IMPORTS (static): ban an unguarded module-level import of a
     platform-only stdlib module. CI runs Linux, so the smoke pass CANNOT see a
     Windows-only import crash; `import fcntl` is invisible to it and sailed
     through every green build. The static half is what makes the Windows class
     visible from a Linux runner, and vice versa.

Neither gate asserts behaviour -- that belongs in each hook's own suite. These
two only assert that the hook EXISTS as a running program on every platform the
project installs on, which is the floor both bugs fell through.

Run: python3 hooks/test_hook_smoke.py
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
REPO = HOOKS.parent
HOOKS_JSON = REPO / "hooks.json"
TIMEOUT_SEC = 60

# Stdlib modules that exist on one platform and not the other. An unguarded
# module-level import of any of these is an import-time crash on the other side.
POSIX_ONLY = {"fcntl", "termios", "pwd", "grp", "resource", "posix", "pty",
              "tty", "crypt", "spwd", "nis", "syslog", "readline"}
WINDOWS_ONLY = {"msvcrt", "winreg", "winsound", "_winapi"}
PLATFORM_ONLY = POSIX_ONLY | WINDOWS_ONLY

# Hooks the SMOKE pass cannot run, each with a reason. Never a bare skip: a
# silent exemption list is how the original hole stayed open. The static
# platform-import gate still covers everything here.
SMOKE_EXEMPT = {
    "warn-recreate-deleted-file.test.py":
        "not a hook -- a stray test file living in hooks/ (misnamed; it is not "
        "test_*.py so no runner picks it up either)",
}

# Minimal but structurally plausible payloads, keyed by the event a hook is
# registered under in hooks.json. A hook that reads a field it was not given
# must cope: the real harness sends partial payloads too.
PAYLOADS = {
    "UserPromptSubmit": {"prompt": "hello"},
    "PreToolUse": {"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
    "PostToolUse": {"tool_name": "Bash", "tool_input": {"command": "echo hi"},
                    "tool_response": {"stdout": "hi"}},
    "Stop": {},
    "SubagentStop": {},
    "SessionStart": {},
    "SessionEnd": {},
    "PreCompact": {},
    "Notification": {"message": "x"},
}
DEFAULT_EVENT = "PreToolUse"


def hook_files() -> list[Path]:
    return sorted(p for p in HOOKS.glob("*.py")
                  if not p.name.startswith(("test_", "_")))


def event_for() -> dict[str, str]:
    """script basename -> the event it is registered under in hooks.json."""
    try:
        doc = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, str] = {}
    for event, groups in doc.get("hooks", {}).items():
        for group in groups:
            for h in group.get("hooks", []):
                for name in re.finditer(r"hooks/([A-Za-z0-9_.\-]+\.py)",
                                        h.get("command", "")):
                    out.setdefault(name.group(1), event)
    return out


def unguarded_platform_imports(source: str) -> list[tuple[str, int]]:
    """Module-level imports of platform-only modules with no ImportError guard.

    Only Module.body is inspected: an import nested in a function is deferred,
    and one inside a `try:` whose handler catches ImportError is the sanctioned
    idiom (hooks/session-lock.py is the reference).
    """
    hits: list[tuple[str, int]] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in PLATFORM_ONLY:
                    hits.append((root, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in PLATFORM_ONLY:
                hits.append((root, node.lineno))
    return hits


def main() -> int:
    failures: list[str] = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    hooks = hook_files()
    check(len(hooks) > 40,
          "only %d hooks discovered -- the glob has gone blind, which would "
          "make this whole suite vacuously green" % len(hooks))

    # ---- gate 2: static platform-only imports ---------------------------
    for path in hooks:
        try:
            hits = unguarded_platform_imports(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue  # ci.sh gate (a) owns syntax
        for mod, line in hits:
            other = "Windows" if mod in POSIX_ONLY else "POSIX"
            check(False,
                  "%s:%d imports `%s` at module level with no ImportError "
                  "guard -- that is an import-time crash on %s, and a Linux CI "
                  "smoke run cannot see it. Use the hooks/session-lock.py "
                  "idiom: try/except ImportError with a None fallback."
                  % (path.name, line, mod, other))

    # ---- gate 1: dynamic smoke ------------------------------------------
    events = event_for()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home, cwd = tmp / "home", tmp / "cwd"
        (home / ".claude").mkdir(parents=True)
        cwd.mkdir()
        # Hermetic: a real HOME would let a hook scan the developer's machine or
        # write into their live vault. Bypass switches are CLEARED, not set --
        # a bypassed hook returns before doing anything and would smoke green
        # while thoroughly broken.
        env = {k: v for k, v in os.environ.items()
               if not k.endswith("_BYPASS")
               and k not in ("VAULT_ROOT", "VAULT_TZ", "CLAUDE_CWD")}
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)

        for path in hooks:
            if path.name in SMOKE_EXEMPT:
                continue
            event = events.get(path.name, DEFAULT_EVENT)
            payload = dict(PAYLOADS.get(event, {}))
            payload["cwd"] = str(cwd).replace("\\", "/")
            try:
                res = subprocess.run(
                    [sys.executable, str(path)], input=json.dumps(payload),
                    cwd=str(cwd), env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                check(False, "%s did not finish in %ds on a minimal [%s] "
                             "payload" % (path.name, TIMEOUT_SEC, event))
                continue
            if "Traceback (most recent call last)" in res.stderr:
                last = [ln for ln in res.stderr.strip().splitlines() if ln.strip()][-1]
                check(False,
                      "%s CRASHED on a minimal [%s] payload (rc=%d): %s"
                      % (path.name, event, res.returncode, last.strip()[:160]))

    # ---- negative controls: both gates must be able to go red -----------
    check(unguarded_platform_imports("import fcntl\n") == [("fcntl", 1)],
          "the platform-import gate missed a bare `import fcntl` -- it is "
          "vacuous and would not have caught the secret-scanner bug")
    check(unguarded_platform_imports(
              "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n") == [],
          "the platform-import gate flagged the GUARDED idiom -- it would "
          "forbid the very shape of the fix")
    check(unguarded_platform_imports(
              "def f():\n    import fcntl\n    return fcntl\n") == [],
          "the platform-import gate flagged a deferred import inside a "
          "function, which cannot crash at import time")

    with tempfile.TemporaryDirectory() as td:
        planted = Path(td) / "planted-crasher.py"
        planted.write_text("from zoneinfo import ZoneInfo\n"
                           "TZ = ZoneInfo('America/not-a-real-zone')\n",
                           encoding="utf-8")
        res = subprocess.run([sys.executable, str(planted)], input="{}",
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=TIMEOUT_SEC)
        check("Traceback (most recent call last)" in res.stderr,
              "a planted import-time crasher produced no traceback -- the "
              "smoke gate's crash predicate does not detect the class it "
              "exists for")

    if failures:
        print("FAILED - hook smoke:")
        for f in failures:
            print("  x " + f)
        return 1
    print("OK - %d hook(s) import and run to completion on a minimal payload; "
          "no unguarded platform-only imports; both gates verified non-vacuous"
          % len(hooks))
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
