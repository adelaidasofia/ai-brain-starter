#!/usr/bin/env python3
"""Fail when a SessionStart hook strands its message on a TOP-LEVEL key.

THE BUG THIS EXISTS FOR. SessionStart delivers context to the model ONLY via
`hookSpecificOutput.additionalContext`. A top-level `additionalContext` is
accepted, exits 0, prints valid JSON, appears in logs as a hook that fired --
and is DISCARDED in transit. The guard runs, decides correctly, and nobody
ever hears it.

Measured on this repo 2026-08-23: SIX wired SessionStart hooks emitted that
shape -- the backup alarm, both MYC-575 worktree-melt guards, runaway-process
remediation, orphan-snapshot recovery, and the footprint signal. Every one of
them ships to every install, so a client's backup alarm was mute for the same
reason the maintainer's own was during a 65-day offsite outage (MYC-3576): not
a wrong decision, a discarded one.

Bug class SILENT-NO-OP, delivery-layer variant. It is worse than a guard that
never runs, because the code reads as working and the logs show it firing.

Why STRUCTURAL and not a per-file test: the six were written by different
hands at different times, each reasonably. Nothing told the seventh author the
shape mattered. A check that enumerates 100% of the wired SessionStart set is
the only thing that makes the next one fail loudly at CI instead of silently in
production.

SIBLING, NOT DUPLICATE: scripts/check-hook-emission-channel.py enforces the
adjacent invariant -- a hook must not warn on a channel its WIRING discards
(stderr swallowed by `2>/dev/null`, MYC-3246). That one reads the wiring; this
one reads the PAYLOAD SHAPE. The proof they are different: that guard runs in
CI and was green while all six hooks below were mute, because their stdout JSON
was valid and their wiring preserved it -- the message was simply on a key the
harness drops.

Usage:
  check-sessionstart-emit-shape.py [--repo <path>]
  check-sessionstart-emit-shape.py --self-test    # negative control
"""
# exit-contract: ENFORCING

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tempfile
from pathlib import Path

# Tolerate BOTH command forms. A POSIX-only regex is how a sibling audit
# (MYC-3898) silently audited almost nothing on every Windows install while
# reporting OK -- the parse failed, found no hooks, and called that clean.
HOOK_RE = re.compile(r"hooks[/\\]([\w.-]+\.py)")


def wired_sessionstart_hooks(hooks_json: Path) -> list[str]:
    """Hook filenames wired to SessionStart, from the manifest."""
    data = json.loads(hooks_json.read_text(encoding="utf-8"))
    events = data.get("hooks") or data
    names: list[str] = []
    for event, blocks in events.items():
        if event != "SessionStart" or not isinstance(blocks, list):
            continue
        for block in blocks:
            for hook in block.get("hooks", []) or []:
                m = HOOK_RE.search(hook.get("command") or "")
                if m:
                    names.append(m.group(1))
    return sorted(set(names))


def violations(source: str) -> list[int]:
    """Line numbers of `additionalContext` dicts NOT nested under hookSpecificOutput.

    AST, not grep: the question is structural (WHERE the key sits), and a
    regex cannot tell a top-level key from a correctly nested one.
    """
    tree = ast.parse(source)

    # Dicts that are the VALUE of a "hookSpecificOutput" key -- the correct home.
    nested_ok: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "hookSpecificOutput":
                if isinstance(v, ast.Dict):
                    nested_ok.add(id(v))

    bad: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict) or id(node) in nested_ok:
            continue
        for k in node.keys:
            if isinstance(k, ast.Constant) and k.value == "additionalContext":
                bad.append(node.lineno)
    return sorted(set(bad))


def audit(repo: Path) -> tuple[int, list[str]]:
    hooks_json = repo / "hooks.json"
    if not hooks_json.is_file():
        print(f"FAIL: no hooks.json at {hooks_json}", file=sys.stderr)
        return (2, [])

    names = wired_sessionstart_hooks(hooks_json)
    # POSITIVE CONTROL on the SEARCH itself. A census that resolves zero hooks
    # would print a clean report and mean nothing -- the exact shape of the
    # undercount that reported 117 files as 17 (MYC-4003).
    if not names:
        print("FAIL: parsed hooks.json but resolved ZERO SessionStart hooks -- "
              "the manifest shape changed and this audit is blind.", file=sys.stderr)
        return (2, [])

    problems: list[str] = []
    checked = 0
    for name in names:
        path = repo / "hooks" / name
        if not path.is_file():
            continue
        checked += 1
        try:
            for line in violations(path.read_text(encoding="utf-8")):
                problems.append(f"hooks/{name}:{line}")
        except SyntaxError as exc:
            problems.append(f"hooks/{name}: UNPARSEABLE ({exc})")

    if not checked:
        print("FAIL: resolved SessionStart hook names but read ZERO files.",
              file=sys.stderr)
        return (2, [])

    print(f"SessionStart hooks wired: {len(names)}  read: {checked}  "
          f"violations: {len(problems)}")
    return (1 if problems else 0, problems)


BAD_HOOK = '''import json
def emit(ctx):
    print(json.dumps({"continue": True, "additionalContext": ctx}))
'''
GOOD_HOOK = '''import json
def emit(ctx):
    print(json.dumps({"continue": True, "hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": ctx}}))
'''


def self_test() -> int:
    """Negative control. A guard is only trustworthy once it has FAILED."""
    ok = True

    bad = violations(BAD_HOOK)
    print(f"  {'ok  ' if bad else 'FAIL'} detects a top-level additionalContext {bad}")
    ok &= bool(bad)

    good = violations(GOOD_HOOK)
    print(f"  {'ok  ' if not good else 'FAIL'} passes a correctly nested emit {good}")
    ok &= not good

    # End-to-end: a planted bad hook must make the whole audit exit non-zero.
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "hooks").mkdir()
        (repo / "hooks" / "planted-bad.py").write_text(BAD_HOOK, encoding="utf-8")
        (repo / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"command": "python3 hooks/planted-bad.py"}]}]}}), encoding="utf-8")
        rc, probs = audit(repo)
        print(f"  {'ok  ' if rc == 1 else 'FAIL'} planted bad hook fails the audit "
              f"(rc={rc}, {probs})")
        ok &= rc == 1

    # And a manifest this audit cannot parse must FAIL, never read as clean.
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "hooks").mkdir()
        (repo / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": []}}), encoding="utf-8")
        rc, _ = audit(repo)
        print(f"  {'ok  ' if rc == 2 else 'FAIL'} zero-resolve manifest fails loud (rc={rc})")
        ok &= rc == 2

    print("\n" + ("all green" if ok else "SELF-TEST FAILED"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    rc, problems = audit(Path(args.repo))
    if problems:
        print("\nSessionStart hooks stranding context on a TOP-LEVEL key "
              "(the model never receives these):", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("\nFix: nest it as "
              '{"hookSpecificOutput": {"hookEventName": "SessionStart", '
              '"additionalContext": <msg>}}', file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
