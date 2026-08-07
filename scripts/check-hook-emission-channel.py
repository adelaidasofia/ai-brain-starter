#!/usr/bin/env python3
"""Fail when a wired hook can only speak on a channel its own wiring discards.

THE INCIDENT (MYC-3246). `warn-workflow-call-permission-elevation.py` shipped
registered in hooks.json, deployed to disk, and passing 8/8 negative controls --
and it could never warn anyone. It wrote its diagnostic to `sys.stderr`, and
every hooks.json command in this repo is wired as:

    <script> 2>/dev/null || echo <allow-json>

The shell discarded stderr before Claude Code read it. The hook exited 0 with
empty stdout, so the harness saw a clean allow. It was mute for its entire life
on main.

WHY ITS OWN TESTS COULD NOT SEE IT. The test harness piped the hook with
`2>&1`, merging stderr back into stdout. It proved the hook produces text
SOMEWHERE, not text the harness will surface. Any per-hook test can make that
mistake; only a check that reads the WIRING can catch the class.

WHAT THIS ENFORCES -- one invariant, deliberately narrow:

    a wired hook that WARNS must warn on a channel the wiring preserves.

Hooks that signal by EXIT CODE (a blocking `sys.exit(2)`, a
`permissionDecision` payload) are out of scope: their signal is the exit status
or a stdout payload, not prose, and the harness reads those regardless.

MYC-1017 says file presence is not activation. This adds the next layer:
registration is not activation either if the output channel is discarded.

ASCII-only output on purpose -- see scripts/check-utf8-stdout.py.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# A command discards stderr if it redirects fd 2 to the bit bucket. `2>&1` is
# NOT a discard -- it merges into stdout, which the harness does read.
DISCARDS_STDERR = re.compile(r"2>\s*/dev/null")

# Emitting prose on stderr. `print(..., file=sys.stderr)` and sys.stderr.write.
STDERR_EMIT = re.compile(r"sys\.stderr\.write\s*\(|file\s*=\s*sys\.stderr")

# Structured stdout emission -- the convention that actually reaches the model.
STDOUT_EMIT = ("additionalContext", "systemMessage", "suppressOutput")

# Exit-code / decision signalling: the hook's verdict does not depend on prose.
EXIT_SIGNAL = ("sys.exit(2)", "exit(2)", "permissionDecision", '"deny"', "'deny'")


def script_paths(command: str) -> List[Path]:
    """Every .py path referenced by a hook command.

    shlex, not a regex: hook commands routinely quote paths containing spaces
    (a vault folder named with an emoji and a space is normal here). A naive
    `[~/][^\\s]*\\.py` pattern truncates those and reports a real, present file
    as missing -- a false positive that costs more trust than the check earns.
    """
    out: List[Path] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return out
    for tok in tokens:
        if tok.endswith(".py"):
            out.append(Path(tok))
    return out


def classify(command: str, source: str) -> Optional[str]:
    """Return a violation reason, or None when the hook is fine."""
    if not DISCARDS_STDERR.search(command):
        return None  # wiring keeps stderr; nothing to enforce
    if not STDERR_EMIT.search(source):
        return None  # nothing written to stderr at all
    if any(tok in source for tok in STDOUT_EMIT):
        return None  # also speaks on stdout -- the message can arrive
    if any(tok in source for tok in EXIT_SIGNAL):
        return None  # signals by exit code / decision payload, not prose
    return (
        "writes to stderr only, but its wiring discards stderr (2>/dev/null). "
        "Emit hookSpecificOutput.additionalContext on STDOUT instead."
    )


def scan(hooks_json: Path, hooks_dir: Path) -> Tuple[List[str], int]:
    """Returns (violations, hooks_examined)."""
    doc = json.loads(hooks_json.read_text(encoding="utf-8"))
    violations: List[str] = []
    examined = 0
    for event, matchers in doc.get("hooks", {}).items():
        for matcher in matchers:
            for hook in matcher.get("hooks", []):
                command = hook.get("command", "")
                for path in script_paths(command):
                    local = hooks_dir / path.name
                    if not local.exists():
                        # Not this check's job. A missing script is what
                        # install-hooks-user-level.py --fail-on-missing covers,
                        # and double-reporting it here would just add noise.
                        continue
                    examined += 1
                    reason = classify(command, local.read_text(encoding="utf-8", errors="replace"))
                    if reason:
                        violations.append(f"{event}: {path.name}\n    {reason}")
    return violations, examined


def self_test() -> int:
    """Negative controls. The check must bite on the exact shape that shipped."""
    MUTE = 'import sys\nsys.stderr.write("problem")\n'
    STDOUT = 'import json\nprint(json.dumps({"hookSpecificOutput":{"additionalContext":"x"}}))\n'
    BOTH = MUTE + STDOUT
    BLOCKER = 'import sys\nsys.stderr.write("nope")\nsys.exit(2)\n'
    DISCARD = "python3 x.py 2>/dev/null || echo allow"
    MERGE = "python3 x.py 2>&1"
    KEEP = "python3 x.py"

    cases = [
        # The incident, exactly.
        ("stderr-only + discarding wiring -> VIOLATION", DISCARD, MUTE, True),
        # The fix.
        ("stdout additionalContext + discarding wiring -> ok", DISCARD, STDOUT, False),
        ("stderr AND stdout -> ok (message still arrives)", DISCARD, BOTH, False),
        # Blocking hooks signal by exit code, not prose.
        ("blocking hook exit(2) -> ok", DISCARD, BLOCKER, False),
        # Wiring that preserves stderr is fine either way.
        ("stderr-only but wiring keeps stderr -> ok", KEEP, MUTE, False),
        # 2>&1 merges INTO stdout -- not a discard. This is the exact
        # distinction the old test harness got wrong.
        ("stderr-only + 2>&1 merge -> ok", MERGE, MUTE, False),
    ]
    failures = 0
    for name, cmd, src, expect_violation in cases:
        got = classify(cmd, src) is not None
        if got == expect_violation:
            print(f"  ok   {name:52s} ({'violation' if got else 'quiet'})")
        else:
            print(f"  FAIL {name:52s} expected {expect_violation}, got {got}")
            failures += 1

    # Path parsing: a quoted path with spaces must resolve whole. Getting this
    # wrong produced 10 false positives in the ad-hoc sweep that motivated this.
    spaced = "/usr/bin/python3 '/Users/x/Notes/Some Meta/scripts/audit.py' --quiet 2>/dev/null || true"
    names = [p.name for p in script_paths(spaced)]
    if names == ["audit.py"]:
        print(f"  ok   {'quoted path with spaces parses whole':52s} (shlex)")
    else:
        print(f"  FAIL {'quoted path with spaces parses whole':52s} got {names}")
        failures += 1

    print()
    if failures:
        print(f"check-hook-emission-channel self-test: {failures} FAILED")
        return 1
    total = len(cases) + 1
    print(f"check-hook-emission-channel self-test: {total}/{total} passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--hooks-json", default=str(REPO_ROOT / "hooks.json"))
    ap.add_argument("--hooks-dir", default=str(REPO_ROOT / "hooks"))
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    violations, examined = scan(Path(args.hooks_json), Path(args.hooks_dir))
    if not violations:
        print(f"hook emission channels OK -- {examined} wired hook(s) can reach a reader.")
        return 0

    print(f"::error::{len(violations)} wired hook(s) can only speak on a discarded channel.")
    print("::error::They are registered, on disk, and MUTE -- exactly the MYC-3246 defect.")
    for v in violations:
        for line in v.splitlines():
            print(f"::error::{line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
