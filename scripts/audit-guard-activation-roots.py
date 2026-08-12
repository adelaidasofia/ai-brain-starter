#!/usr/bin/env python3
"""Are your guards actually active in every config root Claude Code may launch from?

THE BUG CLASS. Claude Code reads its whole config from ONE root. That root is
`~/.claude` by default, but `CLAUDE_CONFIG_DIR` relocates it wholesale — and
account switchers, multi-profile wrappers and CI harnesses set it routinely.
An installer that writes hooks into `~/.claude/settings.json` therefore
activates them for exactly one entry point. Launch from any other root and
every guard is DORMANT: no error, no warning, no missing file. The hook scripts
are still on disk, so every "is it installed?" check passes.

That is the worst shape a guard can be in — you trust it, and it is not there.

Measured on the maintainer's machine 2026-08-12: `~/.claude` carried 207 hook
registrations across 11 events (68 PreToolUse, 22 Stop, 20 UserPromptSubmit);
each of 7 alternate roots carried 0. Every fabrication guard, every blocking
PreToolUse gate and every prompt-time rule injection was silently absent from
every terminal session and every headless `claude -p` job.

WHAT THIS DOES. Compares hook ACTIVATION (registration in settings.json) across
the primary root and every alternate root you name, per hook EVENT, and reports
what is missing where.

WHAT THIS DELIBERATELY DOES NOT DO. It does not tell you to copy everything
everywhere. Per-session work multiplies under concurrency: N simultaneous
sessions each running a corpus-walking SessionStart hook is how you peg a
machine. Hooks split into two tiers and only one of them must be everywhere:

  GUARD   — enforcement. PreToolUse / Stop / SubagentStop / UserPromptSubmit /
            PreCompact. Blocks, denies, or injects a rule. MUST be active in
            every root, or it is a guard you believe in and do not have.
  SURFACE — reporting. Most SessionStart / SessionEnd dashboards. Written for a
            human to read at the top of an interactive session. A headless job
            has no reader, and running them there costs time and can melt the
            box under concurrency. Interactive root only is CORRECT.

So the report separates the two and only fails on GUARD-tier gaps.

Usage:
    python3 scripts/audit-guard-activation-roots.py
    python3 scripts/audit-guard-activation-roots.py --roots ~/.config/x/a ~/.config/x/b
    python3 scripts/audit-guard-activation-roots.py --roots-glob '~/.config/*/accounts/*/'
    python3 scripts/audit-guard-activation-roots.py --json

Exit codes:
    0  every GUARD-tier hook is active in every root checked
    1  at least one GUARD-tier hook is missing from at least one root
    2  UNEVALUATED — primary root unreadable, or no roots to compare against
       (never 0: "nothing to compare" is not "everything matches")
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

# Events whose whole purpose is to block, deny, or inject a rule. Absence here
# is a silent loss of enforcement, so these must be active in EVERY root.
GUARD_EVENTS = {
    "PreToolUse", "PostToolUse", "Stop", "SubagentStop",
    "UserPromptSubmit", "PreCompact", "PermissionDenied",
}
# Events that are predominantly human-facing reporting. Interactive-root-only is
# a legitimate design choice, so a gap here is INFO, never a failure.
SURFACE_EVENTS = {"SessionStart", "SessionEnd", "FileChanged", "CwdChanged"}


def load_hooks(root: Path) -> tuple[dict[str, set[str]], str | None]:
    """{event: {command, ...}} for one config root, plus an error string."""
    settings = root / "settings.json"
    if not settings.is_file():
        return {}, f"no settings.json at {settings}"
    try:
        data = json.loads(settings.read_text())
    except (OSError, ValueError) as e:
        return {}, f"unreadable/invalid JSON: {e}"
    out: dict[str, set[str]] = {}
    for event, blocks in (data.get("hooks") or {}).items():
        cmds = {
            h.get("command", "")
            for b in blocks or []
            for h in (b.get("hooks") or [])
            if h.get("command")
        }
        if cmds:
            out[event] = cmds
    return out, None


def discover_roots(explicit: list[str], patterns: list[str]) -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        roots.append(Path(env).expanduser())
    for r in explicit:
        roots.append(Path(r).expanduser())
    for pat in patterns:
        roots += [Path(p) for p in sorted(glob.glob(os.path.expanduser(pat)))]
    primary = Path.home() / ".claude"
    seen, uniq = set(), []
    for r in roots:
        rp = r.resolve()
        if rp == primary.resolve() or rp in seen or not rp.is_dir():
            continue
        seen.add(rp)
        uniq.append(r)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--primary", default=str(Path.home() / ".claude"),
                    help="root holding the intended config (default ~/.claude)")
    ap.add_argument("--roots", nargs="*", default=[],
                    help="alternate config roots to check")
    ap.add_argument("--roots-glob", nargs="*", default=[],
                    help="glob(s) expanding to alternate config roots")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    primary = Path(a.primary).expanduser()
    base, err = load_hooks(primary)
    if err or not base:
        sys.stderr.write(
            f"UNEVALUATED: primary root has no usable hook config ({err or 'no hooks'}). "
            "This is not a pass.\n")
        return 2

    roots = discover_roots(a.roots, a.roots_glob)
    if not roots:
        sys.stderr.write(
            "UNEVALUATED: no alternate config roots given or discovered. "
            "Nothing was compared — that is not the same as everything matching. "
            "Pass --roots / --roots-glob, or set CLAUDE_CONFIG_DIR.\n")
        return 2

    findings, guard_gap = [], False
    for root in roots:
        other, oerr = load_hooks(root)
        row = {"root": str(root), "error": oerr, "guard_missing": {}, "surface_missing": {}}
        for event, cmds in base.items():
            missing = sorted(cmds - other.get(event, set()))
            if not missing:
                continue
            if event in GUARD_EVENTS:
                row["guard_missing"][event] = missing
                guard_gap = True
            elif event in SURFACE_EVENTS:
                row["surface_missing"][event] = missing
            else:  # unknown event -> treat as guard; fail safe, not silent
                row["guard_missing"][event] = missing
                guard_gap = True
        findings.append(row)

    result = {
        "primary": str(primary),
        "primary_hook_count": sum(len(c) for c in base.values()),
        "roots_checked": len(roots),
        "verdict": "FAIL" if guard_gap else "PASS",
        "findings": findings,
    }

    if a.json:
        print(json.dumps(result, indent=2))
        return 1 if guard_gap else 0

    print(f"primary: {primary}  ({result['primary_hook_count']} hook registrations)")
    for row in findings:
        gm = sum(len(v) for v in row["guard_missing"].values())
        sm = sum(len(v) for v in row["surface_missing"].values())
        print(f"\n  {row['root']}")
        if row["error"]:
            print(f"      {row['error']}")
        print(f"      GUARD-tier missing  : {gm}")
        for ev, cmds in sorted(row["guard_missing"].items()):
            print(f"        {ev} ({len(cmds)})")
            for c in cmds[:3]:
                print(f"            - {c[:96]}")
            if len(cmds) > 3:
                print(f"            ... and {len(cmds) - 3} more")
        print(f"      surface-tier missing: {sm}  (informational — interactive-only is valid)")

    print(f"\nVERDICT: {result['verdict']}")
    if guard_gap:
        print(
            "GUARD-tier hooks are registered in the primary root and ABSENT from at\n"
            "least one other root. Sessions launched from those roots run with those\n"
            "guards silently off. Port the GUARD tier; leave the surface tier alone."
        )
    return 1 if guard_gap else 0


if __name__ == "__main__":
    sys.exit(main())
