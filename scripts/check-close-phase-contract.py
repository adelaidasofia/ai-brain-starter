#!/usr/bin/env python3
"""Validate a session-close rule file against the close cascade's phase contract.

`hooks/detect-closing-signal.py` injects a numbered cascade into the model's
context at close. A rule file describes the same process for the same reader.
When the two disagree about what a NUMBER means, the number is worse than no
number at all — and the failure is silent, because each file is internally
consistent.

That is not hypothetical. ai-brain-starter#400 added a `/goal clear` reminder to
the shipped rule and labelled it "Phase 4b" using the cascade's numbering; under
the rule's own numbering Phase 4 was the step that happens automatically with no
involvement from the reader, so the label announced that the goal clears itself.
#415 aligned the shipped rule. Then the SAME defect turned up in an installed,
heavily customised copy of that rule: it carried a "Phase 4b" with no Phase 4
anywhere in the file. A guard that only ever ran against the file in this repo
could not see it.

So this checker takes a PATH. The shipped template is the default; `--rule`
points it at an installed copy, which is where rules actually drift.

Two tiers, deliberately:

  STRUCTURAL (always on, no false positives possible)
    - no duplicate phase numbers          a number must head exactly one section
    - no orphan sub-phase                 "4b" requires a "4" to belong to
    These are properties of the file alone. An installed rule may legitimately
    rename, reorder, add and drop phases; it may never define 4b without 4.

  MEANING (--strict-meaning, canonical files only)
    - a number present in BOTH the rule and the cascade must denote the same step
    Enforced on the file this repo ships. NOT enforced by default, because an
    installed rule is customised prose ("Summary format" for "Final summary") and
    a checker that fails on legitimate customisation teaches people to bypass it,
    which then hides the structural failures that matter.

Exit 0 = pass. Exit 1 = contract violated. Exit 2 = could not check (fail loud;
a checker that cannot read its inputs must never report success).

Pure stdlib. Usage:
    python3 scripts/check-close-phase-contract.py                    # shipped template
    python3 scripts/check-close-phase-contract.py --strict-meaning   # + meaning pins
    python3 scripts/check-close-phase-contract.py --rule ~/vault/rules/session-close.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULE = REPO_ROOT / "templates" / "rules" / "session-close.md"
DEFAULT_CASCADE = REPO_ROOT / "hooks" / "detect-closing-signal.py"

# "## Phase 4b — Goodbye" / "## Phase 2b: Commit what you wrote"
#
# The separator is REQUIRED. A rule may also carry meta-headings that merely
# mention a phase — "## Phase 1 MUST show its work" is guidance ABOUT Phase 1,
# not the definition of it. Without this, such a heading counts as a second
# Phase 1 and the duplicate check fires on a file that is perfectly correct;
# a checker that cries wolf on real prose is one people learn to bypass.
RULE_PHASE_RE = re.compile(
    r"^##\s+Phase\s+(?P<num>\d+(?:\.\d+)?[a-z]?)\s*[—:–]\s*(?P<title>.*)$",
    re.MULTILINE,
)
# "PHASE 4b — CLEAR THE SESSION GOAL"
CASCADE_PHASE_RE = re.compile(
    r"^PHASE\s+(?P<num>\d+(?:\.\d+)?[a-z]?)\s*[—:–-]\s*(?P<title>.*)$",
    re.MULTILINE,
)

# A number present in both files must mean the same step. Keyed by phase number;
# each value is a pair of substrings (rule-side, cascade-side) that must appear,
# case-insensitively, in that phase's title. Pinning SUBSTRINGS rather than whole
# titles leaves wording free while holding the meaning still.
MEANING_PINS = {
    "0b": ("unfinished business", "incomplete-work"),
    "1": ("scan the conversation", "conversation scan"),
    "2": ("write to the vault", "batch writes"),
    "2b": ("commit", "vault-safe-commit"),
    "3": ("functional audit", "functional audit"),
    "4": ("goodbye", "final summary"),
}


def _parse(text: str, pattern: re.Pattern) -> list[tuple[str, str]]:
    return [(m.group("num"), m.group("title").strip()) for m in pattern.finditer(text)]


def _base_number(num: str) -> str:
    """'4b' -> '4'. '1.8' -> '1.8' (a dotted number is its own phase, not a sub)."""
    return num[:-1] if num and num[-1].isalpha() else num


def check(rule_path: Path, cascade_path: Path, strict_meaning: bool) -> int:
    try:
        rule_text = rule_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"CANNOT CHECK: rule file unreadable: {rule_path} ({exc})", file=sys.stderr)
        return 2
    try:
        cascade_text = cascade_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"CANNOT CHECK: cascade unreadable: {cascade_path} ({exc})", file=sys.stderr)
        return 2

    rule_phases = _parse(rule_text, RULE_PHASE_RE)
    cascade_phases = _parse(cascade_text, CASCADE_PHASE_RE)

    if not rule_phases:
        print(f"CANNOT CHECK: no '## Phase N' headings in {rule_path}", file=sys.stderr)
        return 2
    if not cascade_phases:
        print(f"CANNOT CHECK: no 'PHASE N' lines in {cascade_path}", file=sys.stderr)
        return 2

    failures: list[str] = []

    # --- STRUCTURAL 1: no duplicate numbers ---------------------------------
    seen: dict[str, int] = {}
    for num, _title in rule_phases:
        seen[num] = seen.get(num, 0) + 1
    for num, n in sorted(seen.items()):
        if n > 1:
            failures.append(
                f"phase {num} heads {n} sections — a number must name exactly one step"
            )

    # --- STRUCTURAL 2: no orphan sub-phase ----------------------------------
    # An orphan is a sub-phase whose PARENT the cascade defines but the rule
    # dropped: the rule says "4b" while the cascade has both 4 and 4b, so the
    # reader is pointed at a parent that does not exist in front of them.
    #
    # Keyed to the cascade rather than to bare structure on purpose. The 0-series
    # (0a/0b/0c) legitimately has no bare "Phase 0" in EITHER file — that is the
    # established convention, not drift, and flagging it would be exactly the
    # over-strict failure that gets a guard switched off.
    numbers = {num for num, _ in rule_phases}
    cascade_numbers = {num for num, _ in cascade_phases}
    for num in sorted(numbers):
        base = _base_number(num)
        if base != num and base not in numbers and base in cascade_numbers:
            failures.append(
                f"phase {num} is an orphan — the cascade defines phase {base}, "
                f"but this rule has no phase {base} for {num} to belong to"
            )

    # --- MEANING (opt-in) ---------------------------------------------------
    if strict_meaning:
        rule_by_num = {num: title for num, title in rule_phases}
        cascade_by_num = {num: title for num, title in cascade_phases}
        for num, (want_rule, want_cascade) in sorted(MEANING_PINS.items()):
            r_title = rule_by_num.get(num)
            c_title = cascade_by_num.get(num)
            if r_title is None:
                failures.append(f"phase {num}: rule has no such phase (expected {want_rule!r})")
                continue
            if c_title is None:
                failures.append(f"phase {num}: cascade has no such phase (expected {want_cascade!r})")
                continue
            if want_rule.lower() not in r_title.lower():
                failures.append(
                    f"phase {num}: rule says {r_title!r}, expected it to mean {want_rule!r}"
                )
            if want_cascade.lower() not in c_title.lower():
                failures.append(
                    f"phase {num}: cascade says {c_title!r}, expected it to mean {want_cascade!r}"
                )

    print(f"rule:    {rule_path}  ({len(rule_phases)} phase heading(s))")
    print(f"cascade: {cascade_path}  ({len(cascade_phases)} phase line(s))")
    print(f"mode:    structural{' + meaning pins' if strict_meaning else ''}")
    if failures:
        print("")
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"\nFAILED: {len(failures)} phase-contract violation(s)")
        return 1
    print("OK - phase contract holds")
    return 0


def main() -> int:
    # This CLI prints em dashes and vault paths that contain non-ASCII (the
    # "gear Meta" folder). On a Windows cp1252 console print() would raise
    # UnicodeEncodeError and the caller would read the empty output as failure
    # (ai-brain-starter#313). scripts/check-utf8-stdout.py enforces this block.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rule", default=str(DEFAULT_RULE),
                    help="session-close rule file to check (default: the shipped template)")
    ap.add_argument("--cascade", default=str(DEFAULT_CASCADE),
                    help="the hook that injects the numbered cascade")
    ap.add_argument("--strict-meaning", action="store_true",
                    help="also pin what each shared number MEANS (canonical files only)")
    args = ap.parse_args()
    return check(Path(args.rule).expanduser(), Path(args.cascade).expanduser(),
                 args.strict_meaning)


if __name__ == "__main__":
    sys.exit(main())
