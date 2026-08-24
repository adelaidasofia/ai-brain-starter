#!/usr/bin/env python3
"""
Pin the .ps1 encoding rules (UTF-8 BOM required, em dash banned) to ONE implementation, and keep its control honest.

The rule: every *.ps1 must start with EF BB BF, because Windows PowerShell 5.1
reads a BOM-less file as the console ANSI code page rather than UTF-8 and dies on
the first non-ASCII byte.

The rule used to live as an inline shell loop inside .github/workflows/lint.yml
and nowhere else. That made it structurally unreachable from any local command:
a BOM-less .ps1 passed a full green `bash scripts/ci.sh` -- the canonical gate
the pre-push hook requires -- and failed only after a push, costing a whole CI
round-trip per occurrence (measured 2026-08-19 on
tests/integration/test_bootstrap_ps1_slash_commands.ps1).

The fix was to extract scripts/check-ps1-encoding.sh and have both callers run it.
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
CHECKER = REPO / "scripts" / "check-ps1-encoding.sh"
LINT_YML = REPO / ".github" / "workflows" / "lint.yml"
CI_SH = REPO / "scripts" / "ci.sh"
DIAGNOSE_SH = REPO / "scripts" / "diagnose.sh"
DIAGNOSE_PS1 = REPO / "scripts" / "diagnose.ps1"
FIXTURES = REPO / "tests" / "fixtures" / "ps1-encoding"

BOM = b"\xef\xbb\xbf"
EM_DASH = "\u2014"
# The byte comparisons, as they read in shell. If either appears in a CALLER
# rather than in the checker, that caller has grown its own private copy.
#
INLINE_MARKER = "efbbbf"

# The em-dash rule needs a narrower marker than "efbbbf" does. A literal em dash
# is ordinary punctuation: it appears in comments AND in user-facing message
# strings all over these files (a `bad "..."` hint with one in it), so
# its mere presence says nothing. What identifies a re-inlined copy is an em
# dash being SEARCHED FOR - the old loops were `grep -q $'\\xe2\\x80\\x94' "$f"`.
# So match the search, in either the escaped or the pasted form.
INLINE_EMDASH_SEARCH = re.compile(
    r"(?:grep|Select-String)[^\n]*(?:\\\\xe2\\\\x80\\\\x94|" + EM_DASH + r")"
)

# Both callers must actually INVOKE the script. Matching the bare path is not
# enough: every caller also NAMES the path in a comment, so a substring test
# stays green when the real invocation is deleted (measured while mutation
# testing this suite - two of seven mutations walked straight through it).
# So match the command, and strip comment lines before looking.
SCAN_CALL = re.compile(r"(?:^|\s)bash\s+scripts/check-ps1-encoding\.sh\s*$", re.M)
SELFTEST_CALL = re.compile(r"(?:^|\s)bash\s+scripts/check-ps1-encoding\.sh\s+--self-test\b")
# diagnose.sh resolves the script through a candidate list rather than a fixed
# repo-relative path (it also runs from an installed vault), so it is matched on
# the porcelain invocation instead.
PORCELAIN_CALL = re.compile(r'bash\s+"\$CHECK_ENC"\s+--porcelain\b')

# diagnose.ps1 is the one intentional duplicate: it must run on a Windows box
# with no bash. Pin it to the SAME three bytes as the shell implementation, so
# the rule cannot be changed on one side alone.
#
# Deliberately NOT re.DOTALL. With it, `0xEF.*?0xBB.*?0xBF` matched three
# unrelated occurrences 600 lines apart, so the assertion would survive the real
# comparison being deleted. The bytes are compared on one line; keep the match
# on one line.
PS1_BOM_BYTES = re.compile(r"0xEF.*0xBB.*0xBF", re.I)
# The em-dash half of the same pin. PowerShell spells it as a char code rather
# than bytes, so this is the value, not the UTF-8 encoding.
PS1_EMDASH_BYTES = re.compile(r"0x2014", re.I)

# `| head -N` / `-First N` on the enumeration is what silently truncated the
# scan. Neither user-facing surface may cap again.
#
# Scoped to the PowerShell section rather than the whole file, and matched
# anywhere in it rather than on the enumeration's own line. A line-anchored
# version missed a cap applied on the FOLLOWING line, and a file-wide version
# would trip on the legitimate `git remote -v | head -1` elsewhere in
# diagnose.sh.
SH_CAP = re.compile(r"\|\s*head\b")
PS1_CAP = re.compile(r"(-First\b|Select-Object\b)")

# Section 8 is "PowerShell files (Windows compat)" in both reports.
SH_SECTION = ("# ----- 8.", "# ----- 9.")
PS1_SECTION = ('Section "8.', "# ----- 9.")


def _section(text: str, bounds: tuple[str, str], path: Path) -> str:
    """Return the named section, or fail loudly rather than scanning an empty string."""
    start_marker, end_marker = bounds
    start = text.find(start_marker)
    end = text.find(end_marker, start + 1) if start != -1 else -1
    if start == -1 or end == -1:
        failures.append(
            f"{path} no longer has a section delimited by {start_marker!r}..{end_marker!r}, "
            f"so the no-cap check has nothing to scan. An empty scan reads as clean; fix the "
            f"markers rather than leaving this assertion inert."
        )
        return ""
    return text[start:end]


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
            f"{rel} no longer RUNS `bash scripts/check-ps1-encoding.sh`. The BOM rule must have "
            f"exactly one implementation and both callers must invoke it; a caller that stops "
            f"delegating is how the two copies drifted in the first place. (Naming the path in "
            f"a comment does not count.)",
        )

        # The negative control has to run where the gate runs. A caller that
        # scans the fleet without first proving the checker still bites is
        # trusting a guard that may have gone inert.
        check(
            SELFTEST_CALL.search(code) is not None,
            f"{rel} runs the BOM scan without running `check-ps1-encoding.sh --self-test` first. "
            f"An unproven guard reads exactly like a clean repo.",
        )

        # The re-inlining guard. Only the checker may carry the raw byte
        # comparison; a caller carrying it means a second copy is back.
        check(
            INLINE_MARKER not in body,
            f"{rel} contains the raw byte comparison '{INLINE_MARKER}'. That is a re-inlined "
            f"copy of the BOM rule. Call scripts/check-ps1-encoding.sh instead - two copies drift, "
            f"and the local one is the half that goes stale silently.",
        )
        check(
            INLINE_EMDASH_SEARCH.search(code) is None,
            f"{rel} searches for an em dash itself. That is a re-inlined copy of the em-dash "
            f"rule; it lived here as its own `find` loop until both rules moved into "
            f"scripts/check-ps1-encoding.sh.",
        )


def test_diagnose_sh_delegates() -> None:
    """The user-facing bash report must use the shared rule, not a private copy."""
    body = DIAGNOSE_SH.read_text(encoding="utf-8")
    code = _executable_lines(body)
    rel = DIAGNOSE_SH.relative_to(REPO)

    check(
        PORCELAIN_CALL.search(code) is not None,
        f"{rel} no longer calls check-ps1-encoding.sh --porcelain. It carried its own copy of the "
        f"BOM rule once and the two answers diverged; the shared script is the only owner.",
    )
    check(
        INLINE_MARKER not in body,
        f"{rel} contains the raw byte comparison '{INLINE_MARKER}' again. That is a re-inlined "
        f"copy of the BOM rule - delegate to check-ps1-encoding.sh --porcelain instead.",
    )
    check(
        INLINE_EMDASH_SEARCH.search(code) is None,
        f"{rel} searches for an em dash itself again. Both rules delegate through --porcelain "
        f"now; a private copy here is how the BOM half drifted.",
    )
    # The absent-helper path must WARN, never fall through to a silent pass. A
    # health report that cannot run a check has three states, not two.
    check(
        "check-ps1-encoding.sh not found" in body,
        f"{rel} does not warn when check-ps1-encoding.sh is missing. A check that could not run "
        f"must say so - reporting nothing reads exactly like reporting clean.",
    )
    section = _executable_lines(_section(body, SH_SECTION, rel))
    check(
        SH_CAP.search(section) is None,
        f"{rel} pipes the PowerShell section through `head` again. That is the truncation that "
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
        f"the rule in check-ps1-encoding.sh and must test the same three bytes.",
    )
    check(
        PS1_EMDASH_BYTES.search(body) is not None,
        f"{rel} no longer tests for 0x2014. It carries the Windows-native copy of BOTH rules; "
        f"dropping the em-dash half leaves Windows users with a check the other platforms have.",
    )
    check(
        "check-ps1-encoding.sh" in body,
        f"{rel} no longer explains why it duplicates check-ps1-encoding.sh. Without that note the "
        f"duplicate reads as an oversight and the next person 'fixes' it by adding a bash call, "
        f"which breaks the diagnostic on Windows.",
    )
    section = _executable_lines(_section(body, PS1_SECTION, rel))
    check(
        PS1_CAP.search(section) is None,
        f"{rel} caps its *.ps1 enumeration (-First / Select-Object in the PowerShell section). "
        f"It is currently the honest half of this pair; capping it would hide the same defect "
        f"diagnose.sh used to hide.",
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
    em_dash = FIXTURES / "emdash.ps1.txt"
    check(em_dash.is_file(), f"missing em-dash control {em_dash.relative_to(REPO)}")
    if em_dash.is_file():
        em_bytes = em_dash.read_bytes()
        check(
            em_bytes.startswith(BOM),
            f"{em_dash.relative_to(REPO)} has no BOM. It is the control for the EM DASH rule, "
            f"so it must be BOM-clean or it goes red for the wrong reason.",
        )
        em_text = em_bytes.decode("utf-8")
        check(
            em_text.count(EM_DASH) == 1,
            f"{em_dash.relative_to(REPO)} must contain exactly one U+2014 (found "
            f"{em_text.count(EM_DASH)}). With none it is a second positive control wearing a "
            f"negative control's name.",
        )
        # One character apart from the clean baseline, so a red on this and a
        # green on with-bom isolates the em dash as the cause.
        check(
            em_text.replace(EM_DASH, "-") == with_bom.read_bytes().decode("utf-8"),
            f"{em_dash.relative_to(REPO)} differs from with-bom.ps1.txt by more than the em "
            f"dash, so the red/green difference no longer isolates U+2014.",
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
    stray = sorted(q.relative_to(REPO).as_posix() for q in FIXTURES.glob("*.ps1"))
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
        f"check-ps1-encoding.sh --self-test failed (exit {proc.returncode}):\n"
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
        print(f"FAILED - {len(failures)} problem(s) with the .ps1 encoding gate:")
        for f in failures:
            print(f"  * {f}")
        return 1
    print(f"    OK - {len(tests)} checks: BOTH rules, one scanner; lint.yml + ci.sh + diagnose.sh "
          f"delegate, diagnose.ps1 pinned native; no capped enumerations; controls intact; "
          f"repo clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
