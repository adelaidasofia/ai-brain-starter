#!/usr/bin/env python3
"""Structural lint: a blocking hook must not rely on a non-zero exit under the
allow-fallback wrapper.

THE BUG CLASS THIS EXISTS FOR
    Most hooks in hooks.json are registered with a fail-open wrapper:

        <interpreter> <hook>.py 2>/dev/null || echo '{... "permissionDecision":"allow"}'

    That `||` exists so a CRASHING hook cannot wedge the tool. But it fires on
    ANY non-zero exit -- including a deliberate `sys.exit(2)` block. A hook that
    blocks by exiting 2 under this wrapper is therefore rewritten into an ALLOW:
    it is present on disk, registered in settings.json, visible in every audit,
    and structurally incapable of blocking anything.

    Caught live on validate-handoff-frontmatter.py (issue #375): registering it
    without converting it to the JSON protocol would have shipped a guard that
    looked correct and enforced nothing.

    This is the WIRED-BUT-NEUTERED sibling of ARTIFACT-WITHOUT-ACTIVATION. The
    gate-coverage invariant in ci.sh catches a test that never runs; this catches
    a guard that runs and can never say no.

THE RULE
    If a hook's registered command carries the allow-fallback wrapper, the hook
    must signal a block by PRINTING a deny decision (exit 0), never by exiting
    non-zero. Enforced by inspecting the script the command points at.

Exit 0 clean, 1 on violation, 2 on usage error.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The wrapper that rewrites a non-zero exit into an allow.
WRAPPER_RE = re.compile(r"\|\|\s*echo\b[^\n]*permissionDecision")
# A .py the command invokes, at either the skill path or ~/.claude/hooks/.
PY_IN_CMD_RE = re.compile(r"(?:~|\$HOME|/)[^\s'\"|&;]*?/([A-Za-z0-9_.-]+\.py)")
# A deliberate non-zero exit (the blocking mechanism we are banning here).
NONZERO_EXIT_RE = re.compile(r"^\s*(?:sys\.)?exit\(\s*([1-9]\d*)\s*\)", re.MULTILINE)
# Speaking the decision protocol at all.
DENY_RE = re.compile(r'"permissionDecision"\s*:\s*"deny"|permissionDecision.{0,20}deny')


def hook_commands(hooks_json: Path):
    """Yield (event, matcher, command) for every registered hook command."""
    data = json.loads(hooks_json.read_text(encoding="utf-8"))
    for event, groups in (data.get("hooks") or {}).items():
        for group in groups or []:
            for entry in group.get("hooks", []) or []:
                cmd = entry.get("command") or ""
                if cmd:
                    yield event, group.get("matcher"), cmd


def resolve_script(basename: str) -> Path | None:
    for sub in ("hooks", "scripts"):
        p = REPO / sub / basename
        if p.is_file():
            return p
    return None


def main() -> int:
    hooks_json = REPO / "hooks.json"
    if not hooks_json.is_file():
        print(f"ERROR: no hooks.json at {hooks_json}", file=sys.stderr)
        return 2

    violations: list[str] = []
    checked = 0
    for event, matcher, cmd in hook_commands(hooks_json):
        if not WRAPPER_RE.search(cmd):
            continue
        names = PY_IN_CMD_RE.findall(cmd)
        if not names:
            continue
        # The LAST .py is the hook itself (an earlier one is the Windows runner).
        script = resolve_script(names[-1])
        if script is None:
            continue  # not a script this repo ships; nothing to assert
        checked += 1
        src = script.read_text(encoding="utf-8")
        bad = NONZERO_EXIT_RE.findall(src)
        if bad:
            where = f"{event}" + (f" [{matcher}]" if matcher else "")
            violations.append(
                f"  {script.relative_to(REPO)}  ({where})\n"
                f"      exits non-zero ({', '.join(sorted(set(bad)))}) under the allow-fallback\n"
                f"      wrapper, so the `|| echo ...allow` rewrites that block into an ALLOW.\n"
                f"      Fix: print a permissionDecision deny on stdout and exit 0\n"
                f"      (see hooks/block-secret-in-note.py)."
            )

    if violations:
        print("::error::hook block-protocol violation - a guard that can never block:")
        print("\n".join(violations))
        return 1

    print(f"hook block-protocol: OK ({checked} wrapped hook(s) verified, "
          "none block via a non-zero exit)")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
