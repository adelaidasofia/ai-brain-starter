#!/usr/bin/env python3
"""The close hook must never go SILENT after a close signal has matched.

detect-closing-signal wraps main() in a catch-all so a hook bug can never block
the user. Correct. But it returned a PASSTHROUGH, and a passthrough after a
matched close signal is byte-indistinguishable from a healthy close: the user
says "bye", the cascade never runs, and nothing anywhere says why.

That is the same defect #534 fixed for the KNOWN refusal (no Meta dir), left
open for the UNKNOWN one. It is not theoretical — a NameError introduced while
writing #534 took exactly this path, and every scenario read "quiet and fine"
until a negative control caught it. A refactor bug in any branch downstream of
the signal match silently disables the whole close cascade, permanently, with
no signal on any surface.

Assertions:
  1. A crash AFTER the signal matches emits a visible notice, not a passthrough.
  2. That notice names the error, so the bug is diagnosable from the transcript.
  3. It instructs the model not to report a normal close.
  4. NEGATIVE CONTROL — a crash with NO signal matched still passes through
     silently. A hook that has not decided the user is closing has no business
     speaking, and a notice there would fire on every unrelated prompt.
  5. NEGATIVE CONTROL — the happy path is untouched: a normal close on a
     healthy vault still emits the cascade, not the crash notice.

Control 4 is the one that matters: "always emit something" would pass 1-3 and
turn every hook error on every prompt into user-visible noise.
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "detect_closing_signal", HOOKS / "detect-closing-signal.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures = []


def run(prompt, cwd, vault_root, boom=None):
    """Run main() with stdin stubbed; optionally make a post-signal step raise."""
    payload = json.dumps({"prompt": prompt, "session_id": "t", "cwd": str(cwd)})
    original_stdin, original_find = sys.stdin, mod.find_meta_dir
    # vault-root-ok: saving the caller's value to RESTORE it after the stub,
    # not resolving a vault root. The hook under test does its own resolution
    # through _lib/vault_root.py; this only keeps the test from leaking env.
    original_vr = os.environ.get("VAULT_ROOT")
    try:
        sys.stdin = io.StringIO(payload)
        os.environ["VAULT_ROOT"] = str(vault_root)
        if boom:
            def explode(*_a, **_k):
                raise boom
            mod.find_meta_dir = explode
        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.main()
        return buf.getvalue()
    finally:
        sys.stdin = original_stdin
        mod.find_meta_dir = original_find
        if original_vr is None:
            os.environ.pop("VAULT_ROOT", None)
        else:
            os.environ["VAULT_ROOT"] = original_vr


def context_of(out):
    try:
        return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")
    except (json.JSONDecodeError, AttributeError):
        return ""


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    vault = tmp / "vault"
    (vault / "⚙️ Meta").mkdir(parents=True)

    # 1-3. Crash after the signal matched -> loud, diagnosable, non-normal.
    out = run("ok bye", vault, vault, boom=NameError("name 'collapse_worktree' is not defined"))
    ctx = context_of(out)
    if not ctx:
        failures.append("crash after a matched signal produced a SILENT passthrough (the bug)")
    if "CRASHED before it could emit the cascade" not in ctx:
        failures.append("crash notice missing its headline")
    if "collapse_worktree" not in ctx:
        failures.append("crash notice does not name the error, so the bug is not diagnosable")
    if "do not report a normal close" not in ctx:
        failures.append("crash notice does not tell the model to stop reporting a normal close")

    # 4. NEGATIVE CONTROL — no signal matched: stay silent.
    out = run("what is the weather", vault, vault, boom=RuntimeError("kaboom"))
    if context_of(out):
        failures.append(
            "crash with NO close signal emitted a notice — every unrelated prompt "
            "would become user-visible noise"
        )

    # 5. NEGATIVE CONTROL — happy path unchanged.
    out = run("ok bye", vault, vault)
    ctx = context_of(out)
    if "CRASHED" in ctx:
        failures.append("healthy close wrongly reported a crash")
    if "SESSION CLOSE detected" not in ctx:
        failures.append("healthy close no longer emits the cascade")

if failures:
    print("FAILED — close catch-all silence guard:")
    for f in failures:
        print(f"  ✗ {f}")
    sys.exit(1)
print("OK - the close hook fails loud on a post-signal crash, stays quiet otherwise")
