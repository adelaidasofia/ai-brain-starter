#!/usr/bin/env python3
"""A shipped hook must not write runtime state into the directory it RUNS from.

scrub-session-jsonl-secrets.py resolved its audit log as `HOOK_DIR /
"scrub-log.jsonl"`. HOOK_DIR is correct for importing the sibling `_lib`, and
wrong for a log: the copy wired on a real install is
`~/.claude/skills/ai-brain-starter/hooks/`, a GIT CHECKOUT of this repo. So the
log accumulated as an untracked file inside the deployed public repo, the
deployed-hook-drift surfacer reported it as a hand-edit, and it fired 108 times
in a single day (measured 2026-09-01) — against a file the hook itself wrote.
A standing alarm that is always wrong is how a surfacer becomes wallpaper.

Two legs: the BEHAVIOUR (run the hook from a copied checkout, assert the log
lands outside it) and the CLASS (no hook assigns a runtime-state path relative
to its own __file__), so the next hook cannot reintroduce it.

Run: python3 hooks/test_scrub_log_location.py
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


# --- BEHAVIOUR: reproduce the incident shape -----------------------------
# A copy of hooks/ standing in for the deployed skills checkout, a sandboxed
# HOME so the real one is never touched, and a real session JSONL to scrub.
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    # Stand up the fake checkout WITHOUT a recursive copy. copytree is a
    # recursive reader, which scripts/check-cloud-safe-file-walkers.py forbids
    # without the shared safe_read primitive -- rightly: an unbounded recursive
    # read blocks forever on a cloud placeholder or a FIFO. Nothing here needs
    # recursion. The hook file is COPIED (so its Path(__file__).resolve().parent
    # lands in this directory, which is the whole point of the fixture) and
    # _lib is SYMLINKED, which reads nothing at all.
    checkout = tmp / "skills" / "ai-brain-starter" / "hooks"
    checkout.mkdir(parents=True)
    shutil.copy2(HOOKS / "scrub-session-jsonl-secrets.py",
                 checkout / "scrub-session-jsonl-secrets.py")
    (checkout / "_lib").symlink_to(HOOKS / "_lib", target_is_directory=True)
    home = tmp / "home"
    (home / ".claude").mkdir(parents=True)

    sess = tmp / "session.jsonl"
    sess.write_text(json.dumps({"x": "nothing secret here"}) + "\n", encoding="utf-8")

    before = {p.name for p in checkout.iterdir()}
    assert before == {"scrub-session-jsonl-secrets.py", "_lib"}, before
    proc = subprocess.run(
        [sys.executable, str(checkout / "scrub-session-jsonl-secrets.py")],
        input=json.dumps({"transcript_path": str(sess)}),
        capture_output=True, text=True, encoding="utf-8",
        env=dict(os.environ, HOME=str(home), USERPROFILE=str(home)),
    )
    after = {p.name for p in checkout.iterdir()}

    # The hook is fail-open by design; a non-zero exit would make every
    # assertion below vacuous (nothing ran, so nothing was written either).
    check(proc.returncode == 0,
          f"the hook did not run: rc={proc.returncode} {proc.stderr[-300:]}")
    check("scrub-log.jsonl" not in after,
          "the audit log landed INSIDE the running checkout — that untracked "
          "file is what the drift surfacer reports as a hand-edit every session")
    check(after == before,
          f"the hook wrote {sorted(after - before)} into its own directory; a "
          f"shipped hook's directory is a git checkout, not a state dir")
    check((home / ".claude" / "hooks" / "scrub-log.jsonl").is_file(),
          "the audit log did not land at ~/.claude/hooks/ either — it must move "
          "OUT of the checkout, not disappear (the log is the evidence trail "
          "that the secret-scrub pipeline is running at all)")

    # POSITIVE CONTROL: the leg above passes trivially if the hook never logs.
    # It logs unconditionally, so a populated file proves the path was exercised.
    # Read defensively: against the PRE-FIX hook this file does not exist at all,
    # and a suite that raises there reports a traceback instead of the finding —
    # the negative control has to read as a clean FAIL, not a crash.
    canonical = home / ".claude" / "hooks" / "scrub-log.jsonl"
    logged = canonical.read_text(encoding="utf-8") if canonical.is_file() else ""
    check(logged.strip().startswith("{"),
          f"the audit log is empty/malformed/absent ({logged[:80]!r}) — the "
          f"location assertions above would pass on a hook that never wrote")

# --- CLASS: no hook resolves runtime state against its own __file__ -------
# Extensions that are unambiguously runtime state. `.json` is deliberately out:
# a bundled config read from the hook dir is legitimate.
STATE_SUFFIXES = (".jsonl", ".log", ".state", ".db")
offenders: list[str] = []
for py in sorted(HOOKS.glob("*.py")):
    try:
        tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        continue
    # names bound to a directory derived from __file__ (HOOK_DIR = ...parent)
    dir_names = {"HOOK_DIR", "HERE", "HOOKS", "SCRIPT_DIR"}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        left, right = node.left, node.right
        if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            continue
        if not right.value.endswith(STATE_SUFFIXES):
            continue
        rooted_at_file = (
            (isinstance(left, ast.Name) and left.id in dir_names)
            or "__file__" in ast.dump(left)
        )
        if rooted_at_file:
            offenders.append(f"{py.name}:{node.lineno} -> {right.value}")

check(not offenders,
      "hook(s) resolve runtime state against their own __file__, so the file "
      "lands in whichever deployed copy runs — on a real install that is a git "
      f"checkout of this repo: {offenders}")

if failures:
    print("FAILED — scrub-log location (a hook's directory is not a state dir):")
    for f in failures:
        print(f"  ✗ {f}")
    sys.exit(1)
print("OK - the audit log lands outside the running checkout, the hook writes "
      "nothing into its own directory, and no hook roots runtime state at __file__")
