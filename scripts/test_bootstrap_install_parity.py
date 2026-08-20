#!/usr/bin/env python3
"""Structural parity guard: bootstrap.ps1 must write everywhere bootstrap.sh does.

WHY THIS EXISTS
---------------
2026-08-19: bootstrap.sh had installed `commands/*.md` into ~/.claude/commands/
since 2026-05-14, with a comment explaining that skill folders alone do not
register slash commands. bootstrap.ps1 never had that step. Windows installs
were getting a strictly smaller install than macOS for months, and nothing
reported the difference -- the install said success on both platforms.

A guard for that bug class already existed: tests/integration/
test_phase_doc_slash_commands_installed.sh, written after an install where a
phase doc said "run /second-brain-mapping" and the bootstrap had never
installed it. That guard only inspects bootstrap.sh. So the guard against
"the installer silently skipped something" was itself platform-blind, which is
the same shape as the bug it guards against.

Fixing `commands/` was the INSTANCE. This is the CLASS: assert that every
destination under the user's ~/.claude that bootstrap.sh writes to is also
written by bootstrap.ps1, so the next macOS-first feature cannot ship
Windows-blind in silence.

WHAT IT COMPARES
----------------
Literal `.claude/<name>` destination tokens in each installer. This is a
heuristic on purpose: it is cheap, it has no dependencies, and it fails toward
NOISE (a new shared destination shows up and must be acknowledged) rather than
toward SILENCE. A precise parse of two 2000-line installers in two languages
would be the thing that rots.

THE ALLOW-LIST IS A RATCHET, NOT AN EXEMPTION LIST
--------------------------------------------------
KNOWN_GAPS is checked in BOTH directions:

  (a) a gap NOT in the list fails the build -- you cannot add a
      Windows-blind destination without editing this file, in review, with a
      reason and a ticket.
  (b) a list entry that is NO LONGER a gap ALSO fails the build, with
      "stale amnesty -- delete this row".

(b) is what stops this from becoming a permanent parking lot. An appendable
suppression list is a gate anyone can buy past; amnesty here can only ratchet
DOWN. Every row must carry a real reason and, for a real gap, a ticket.

Stdlib only. Run directly, or via scripts/ci.sh (which globs scripts/test_*.py,
so this cannot ship dormant).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SH = REPO / "bootstrap.sh"
PS1 = REPO / "bootstrap.ps1"

# Destination under ~/.claude that bootstrap.sh writes and bootstrap.ps1 does
# not. Each row is either a REAL GAP with a ticket, or a deliberate
# platform difference with a reason. Rows are deleted when fixed -- never
# rewritten to keep a fixed gap quiet.
KNOWN_GAPS: dict[str, str] = {
    ".ai-brain-starter-install-gaps.jsonl": (
        "MYC-4021 - REAL GAP. bootstrap.sh records every install step that did "
        "not finish; hooks/first-week-checkin.py READS this file and quietly "
        "runs each row's repair command. Windows never writes it, so "
        "`gaps_file.is_file()` is False and partial-install self-repair never "
        "runs there. Delete this row when bootstrap.ps1 writes the same file."
    ),
    ".bootstrap.log": (
        "MYC-4037 - REAL GAP. README documents this as 'Forensic log of every "
        "bootstrap run' with no platform qualification, and "
        "test_bootstrap_corporate_profile.sh relies on it. A Windows install "
        "has no forensic log to ask a client for when something goes wrong."
    ),
    ".bootstrap-state": (
        "MYC-4037 - REAL GAP (same ticket as .bootstrap.log). README documents "
        "it as 'Last successful run timestamp'. Windows never writes it, so "
        "any logic keyed on last-successful-run is macOS-only."
    ),
}

# Tokens that are not install destinations: log/temp scratch, or a path that
# only ever appears inside a comment or an error string.
IGNORE = {
    "skills",  # both install skills; the sub-path differs by installer shape
}

DEST_RE_SH = re.compile(r'(?:\$HOME|~)/\.claude/([A-Za-z0-9_.-]+)')
DEST_RE_PS = re.compile(r'\.claude[/\\]([A-Za-z0-9_.-]+)')


def destinations(path: Path, pattern: re.Pattern[str]) -> set[str]:
    if not path.is_file():
        print(f"FAIL: {path} not found - this guard cannot run", file=sys.stderr)
        raise SystemExit(1)
    text = path.read_text(encoding="utf-8", errors="replace")
    return {m for m in pattern.findall(text)} - IGNORE


def main() -> int:
    sh_dests = destinations(SH, DEST_RE_SH)
    ps_dests = destinations(PS1, DEST_RE_PS)

    # A positive control: if the extraction silently matched nothing, every
    # comparison below is vacuous and would report a clean parity that was
    # never measured.
    if len(sh_dests) < 5 or len(ps_dests) < 5:
        print(
            f"FAIL: destination extraction looks broken "
            f"(bootstrap.sh={len(sh_dests)}, bootstrap.ps1={len(ps_dests)}); "
            f"both installers write many paths under ~/.claude, so a near-empty "
            f"set means the regex stopped matching, not that parity is perfect.",
            file=sys.stderr,
        )
        return 1

    gaps = sh_dests - ps_dests
    failures: list[str] = []

    # (a) an unlisted gap is a NEW Windows-blind destination.
    for d in sorted(gaps - set(KNOWN_GAPS)):
        failures.append(
            f"  NEW Windows-blind destination: ~/.claude/{d}\n"
            f"      bootstrap.sh writes it; bootstrap.ps1 does not. Either add the\n"
            f"      step to bootstrap.ps1, or add a KNOWN_GAPS row with a reason\n"
            f"      and a ticket if the difference is deliberate."
        )

    # (b) a listed gap that is no longer a gap must be DELETED, or the list
    #     silently becomes a permanent exemption.
    for d in sorted(set(KNOWN_GAPS) - gaps):
        failures.append(
            f"  STALE AMNESTY: ~/.claude/{d} is in KNOWN_GAPS but is no longer a gap.\n"
            f"      bootstrap.ps1 covers it now. DELETE the row - keeping it converts\n"
            f"      a closed gap into a standing exemption for the next one."
        )

    if failures:
        print("bootstrap install-parity guard FAILED:\n", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1

    print(
        f"OK - bootstrap install parity: {len(sh_dests)} destination(s) in "
        f"bootstrap.sh, {len(gaps)} known gap(s) still open "
        f"({', '.join(sorted(gaps)) or 'none'}), 0 unlisted and 0 stale."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
