#!/usr/bin/env python3
"""
Pin the .ps1 UTF-8 BOM rule to ONE implementation, and keep its control honest.

The rule: every *.ps1 must start with EF BB BF, because Windows PowerShell 5.1
reads a BOM-less file as the console ANSI code page rather than UTF-8 and dies on
the first non-ASCII byte.

The rule used to live as an inline shell loop inside .github/workflows/lint.yml
and nowhere else. That made it structurally unreachable from any local command:
a BOM-less .ps1 passed a full green `bash scripts/ci.sh` -- the canonical gate
the pre-push hook requires -- and failed only after a push, costing a whole CI
round-trip per occurrence (measured 2026-08-19 on
tests/integration/test_bootstrap_ps1_slash_commands.ps1).

The fix was to extract scripts/check-ps1-bom.sh and have both callers run it.
This suite defends the two ways that fix silently rots:

  1. A caller stops delegating (someone re-inlines the byte comparison into
     lint.yml "just for this one step", and the two copies drift apart again).
  2. The negative control stops being negative (someone "fixes" the BOM-less
     fixture by adding a BOM, and the control passes trivially forever).

Runs automatically: scripts/ci.sh gate (f) globs scripts/test_*.py, so a new
suite here can never sit dormant.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "scripts" / "check-ps1-bom.sh"
LINT_YML = REPO / ".github" / "workflows" / "lint.yml"
CI_SH = REPO / "scripts" / "ci.sh"
DIAGNOSE_SH = REPO / "scripts" / "diagnose.sh"
DIAGNOSE_PS1 = REPO / "scripts" / "diagnose.ps1"
FIXTURES = REPO / "tests" / "fixtures" / "ps1-bom"

BOM = b"\xef\xbb\xbf"
# The byte comparison, as it reads in shell. If this string appears in a CALLER
# rather than in the checker, that caller has grown its own private copy.
INLINE_MARKER = "efbbbf"

# Both callers must actually INVOKE the script. Matching the bare path is not
# enough: every caller also NAMES the path in a comment, so a substring test
# stays green when the real invocation is deleted (measured while mutation
# testing this suite - two of seven mutations walked straight through it).
# So match the command, and strip comment lines before looking.
SCAN_CALL = re.compile(r"(?:^|\s)bash\s+scripts/check-ps1-bom\.sh\s*$", re.M)
SELFTEST_CALL = re.compile(r"(?:^|\s)bash\s+scripts/check-ps1-bom\.sh\s+--self-test\b")
# diagnose.sh resolves the script through a candidate list rather than a fixed
# repo-relative path (it also runs from an installed vault), so it is matched on
# the porcelain invocation instead.
PORCELAIN_CALL = re.compile(r'bash\s+"\$CHECK_BOM"\s+--porcelain\b')

# diagnose.ps1 is the one intentional duplicate: it must run on a Windows box
# with no bash. Pin it to the SAME three bytes as the shell implementation, so
# the rule cannot be changed on one side alone.
PS1_BOM_BYTES = re.compile(
    r"0xEF.*?0xBB.*?0xBF", re.S | re.I
)
# `| head -N` / `-First N` on the enumeration is what silently truncated the
# scan. Neither user-facing surface may cap again.
SH_CAP = re.compile(r"-name\s+'\*\.ps1'[^\n]*\|\s*head\b")
PS1_CAP = re.compile(r"Filter\s+'\*\.ps1'[^\n]*(-First|Select-Object)\b")


def _executable_lines(text: str) -> str:
    """Drop whole-line comments, so a path named in prose never reads as a call."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def test_checker_exists() -> None:
    check(CHECKER.is_file(), f"missing {CHECKER.relative_to(REPO)} - the single source of truth")


def test_both_callers_delegate() -> None:
    """Each caller must INVOKE the shared script, not reimplement it."""
    for caller in (LINT_YML, CI_SH):
        body = caller.read_text(encoding="utf-8")
        code = _executable_lines(body)
        rel = caller.relative_to(REPO)

        check(
            SCAN_CALL.search(code) is not None,
            f"{rel} no longer RUNS `bash scripts/check-ps1-bom.sh`. The BOM rule must have "
            f"exactly one implementation and both callers must invoke it; a caller that stops "
            f"delegating is how the two copies drifted in the first place. (Naming the path in "
            f"a comment does not count.)",
        )

        # The negative control has to run where the gate runs. A caller that
        # scans the fleet without first proving the checker still bites is
        # trusting a guard that may have gone inert.
        check(
            SELFTEST_CALL.search(code) is not None,
            f"{rel} runs the BOM scan without running `check-ps1-bom.sh --self-test` first. "
            f"An unproven guard reads exactly like a clean repo.",
        )

        # The re-inlining guard. Only the checker may carry the raw byte
        # comparison; a caller carrying it means a second copy is back.
        check(
            INLINE_MARKER not in body,
            f"{rel} contains the raw byte comparison '{INLINE_MARKER}'. That is a re-inlined "
            f"copy of the BOM rule. Call scripts/check-ps1-bom.sh instead - two copies drift, "
            f"and the local one is the half that goes stale silently.",
        )


def test_diagnose_sh_delegates() -> None:
    """The user-facing bash report must use the shared rule, not a private copy."""
    body = DIAGNOSE_SH.read_text(encoding="utf-8")
    code = _executable_lines(body)
    rel = DIAGNOSE_SH.relative_to(REPO)

    check(
        PORCELAIN_CALL.search(code) is not None,
        f"{rel} no longer calls check-ps1-bom.sh --porcelain. It carried its own copy of the "
        f"BOM rule once and the two answers diverged; the shared script is the only owner.",
    )
    check(
        INLINE_MARKER not in body,
        f"{rel} contains the raw byte comparison '{INLINE_MARKER}' again. That is a re-inlined "
        f"copy of the BOM rule - delegate to check-ps1-bom.sh --porcelain instead.",
    )
    # The absent-helper path must WARN, never fall through to a silent pass. A
    # health report that cannot run a check has three states, not two.
    check(
        "check-ps1-bom.sh not found" in body,
        f"{rel} does not warn when check-ps1-bom.sh is missing. A check that could not run "
        f"must say so - reporting nothing reads exactly like reporting clean.",
    )
    check(
        SH_CAP.search(code) is None,
        f"{rel} caps its *.ps1 enumeration with `| head` again. That is the truncation that "
        f"made a 25-file vault report 'All .ps1 files have UTF-8 BOM' after reading 20.",
    )


def test_diagnose_ps1_stays_native_and_pinned() -> None:
    """The Windows surface keeps its own check - but pinned to the same bytes.

    Folding this one in would break it: it runs as `pwsh diagnose.ps1` on a box
    that need not have bash. So the duplicate is deliberate, and this is what
    stops the two implementations from drifting apart silently.
    """
    body = DIAGNOSE_PS1.read_text(encoding="utf-8")
    rel = DIAGNOSE_PS1.relative_to(REPO)

    check(
        PS1_BOM_BYTES.search(body) is not None,
        f"{rel} no longer compares the bytes 0xEF 0xBB 0xBF. It is the Windows-native copy of "
        f"the rule in check-ps1-bom.sh and must test the same three bytes.",
    )
    check(
        "check-ps1-bom.sh" in body,
        f"{rel} no longer explains why it duplicates check-ps1-bom.sh. Without that note the "
        f"duplicate reads as an oversight and the next person 'fixes' it by adding a bash call, "
        f"which breaks the diagnostic on Windows.",
    )
    check(
        PS1_CAP.search(body) is None,
        f"{rel} caps its *.ps1 enumeration. It is currently the honest half of this pair; "
        f"capping it would hide the same defect diagnose.sh used to hide.",
    )


def test_fixture_integrity() -> None:
    """The negative control is only a control while the fixture lacks a BOM."""
    no_bom = FIXTURES / "no-bom.ps1.txt"
    with_bom = FIXTURES / "with-bom.ps1.txt"

    check(no_bom.is_file(), f"missing negative-control fixture {no_bom.relative_to(REPO)}")
    check(with_bom.is_file(), f"missing positive-control fixture {with_bom.relative_to(REPO)}")
    if not (no_bom.is_file() and with_bom.is_file()):
        return

    check(
        not no_bom.read_bytes().startswith(BOM),
        f"{no_bom.relative_to(REPO)} starts with a BOM. It is the NEGATIVE control: with a BOM "
        f"the check passes it trivially and proves nothing.",
    )
    check(
        with_bom.read_bytes().startswith(BOM),
        f"{with_bom.relative_to(REPO)} does not start with a BOM. It is the POSITIVE control "
        f"and exists to prove the check is not simply always-red.",
    )
    # Same bytes apart from the BOM, so the red/green difference between them is
    # the BOM and nothing else.
    check(
        with_bom.read_bytes()[len(BOM):] == no_bom.read_bytes(),
        "the two fixtures differ by more than the BOM, so a red on one and a green on the "
        "other no longer isolates the BOM as the cause.",
    )

    # A fixture named *.ps1 would be scanned by the very gate it is a fixture
    # for (and by the em-dash and pwsh-parse steps), so the negative control
    # would red the repo forever. The .ps1.txt suffix is load-bearing.
    stray = sorted(p.relative_to(REPO).as_posix() for p in FIXTURES.glob("*.ps1"))
    check(
        not stray,
        f"fixture(s) named *.ps1 in {FIXTURES.relative_to(REPO)}: {stray}. Use the .ps1.txt "
        f"suffix - a real .ps1 here is scanned by the gate itself.",
    )


def test_negative_control_passes() -> None:
    """Run the control. It builds a real git repo and drives a red -> green cycle."""
    if not CHECKER.is_file():
        return
    proc = subprocess.run(
        ["bash", str(CHECKER), "--self-test"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    check(
        proc.returncode == 0,
        f"check-ps1-bom.sh --self-test failed (exit {proc.returncode}):\n"
        f"{(proc.stdout + proc.stderr).strip()}",
    )


def test_repo_is_clean() -> None:
    """The rule itself, over this repo. Cheap, and names the offender directly."""
    if not CHECKER.is_file():
        return
    proc = subprocess.run(
        ["bash", str(CHECKER)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    check(
        proc.returncode == 0,
        f"a tracked *.ps1 is missing its UTF-8 BOM:\n{(proc.stdout + proc.stderr).strip()}",
    )


def main() -> int:
    tests = [
        test_checker_exists,
        test_both_callers_delegate,
        test_diagnose_sh_delegates,
        test_diagnose_ps1_stays_native_and_pinned,
        test_fixture_integrity,
        test_negative_control_passes,
        test_repo_is_clean,
    ]
    for t in tests:
        t()

    if failures:
        print(f"FAILED - {len(failures)} problem(s) with the .ps1 BOM gate:")
        for f in failures:
            print(f"  * {f}")
        return 1
    print(f"    OK - {len(tests)} checks: one implementation; lint.yml + ci.sh + diagnose.sh "
          f"delegate, diagnose.ps1 pinned native; no capped enumerations; controls intact; "
          f"repo clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
