#!/usr/bin/env python3
"""Validate the non-developer plain-language register rule stays wired: promoted
into the always-loaded CLAUDE.md template (governs every phase, every skill,
every session), and NOT re-duplicated as a second, driftable copy inside the
session-close rule.

The incident this guards: a non-technical user ran a skill mid-session (not
close) and the model narrated ".gitignore" leading-slash semantics, a raw
commit SHA, "another session working in parallel", worktrees, and an
instruction to paste an API key into a dotfile -- then ended the turn asking
her to decide whether to "commit and push" dozens of files, a call she had no
basis to make. The plain-language rule already existed, but only inside
templates/rules/session-close.md, so it governed the close phase and nothing
else -- the leak happened mid-session, long before any close ran.

The fix has two halves and this script proves both stay wired:

  1. PROMOTED: the rule now lives in templates/generated/claude-md-template.md
     -- the CLAUDE.md every install gets, read at the start of every session
     (Session Protocol step 1), so it governs every phase, not just close.
  2. SINGLE SOURCE OF TRUTH: templates/rules/session-close.md keeps a short
     pointer to the promoted rule, never a second copy of the prose. Two
     copies of one rule is the failure mode this script exists to catch --
     they will diverge the first time only one gets edited.

Checks (structural, deterministic, no LLM -- mirrors the two-tier philosophy
of check-close-phase-contract.py: pin MEANING by required substring, never
pin exact wording, so the prose stays free to reword):

  A. exactly one heading in the CLAUDE.md template has a body mentioning "not
     developers", and that body covers BOTH halves of the rule -- the
     machinery-narration ban AND the no-blind-technical-decision rule.
  B. session-close.md's own "not developers" paragraph names that EXACT
     heading text (proves the pointer really points at the CURRENT section,
     not a renamed or moved one -- a stale pointer is drift too).
  C. session-close.md's paragraph does NOT itself contain the
     machinery-narration-ban substring. If it does, someone pasted the full
     rule back in and two copies now exist -- exactly the drift this script
     exists to stop.

Exit 0 = wired correctly. Exit 1 = drifted (see message). Exit 2 = could not
check (fail loud; a checker that cannot read its inputs must never report
success).

Pure stdlib. Usage:
    python3 scripts/check-nondev-register-sync.py
    python3 scripts/check-nondev-register-sync.py --template PATH --session-close PATH
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = REPO_ROOT / "templates" / "generated" / "claude-md-template.md"
DEFAULT_SESSION_CLOSE = REPO_ROOT / "templates" / "rules" / "session-close.md"

# Required in the TEMPLATE section body -- both halves of the rule, pinned by
# substring so the prose stays free to reword. Case-insensitive throughout.
PIN_MACHINERY_BAN = "narrate machinery"
PIN_NO_BLIND_DECISION = "technical either/or"
# Identifies WHICH section is "the rule" -- any heading whose body mentions this.
PIN_SECTION_ANCHOR = "not developers"

# Window (chars) searched around the anchor in session-close.md for the
# pointer's heading-name and the anti-duplication check. Generous enough to
# hold a whole paragraph, tight enough that unrelated later content in a long
# file can't produce a false pass or false fail.
POINTER_WINDOW = 600

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _sections(text: str) -> list[tuple[str, str]]:
    """[(heading_text, body_text), ...] -- body runs to the next heading of any level."""
    matches = list(HEADING_RE.finditer(text))
    out = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group(2).strip(), text[start:end]))
    return out


def check(template_path: Path, session_close_path: Path) -> int:
    # OSError covers missing/unreadable/permission-denied; UnicodeDecodeError
    # (a ValueError subclass, NOT an OSError) covers a non-UTF-8 file -- both
    # mean "cannot check", and both must exit 2 rather than an unhandled
    # traceback that a caller could misread as something other than failure.
    try:
        template_text = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"CANNOT CHECK: template unreadable: {template_path} ({exc})", file=sys.stderr)
        return 2
    try:
        session_close_text = session_close_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"CANNOT CHECK: session-close rule unreadable: {session_close_path} ({exc})",
              file=sys.stderr)
        return 2

    # --- A: find the (exactly one) section in the template ------------------
    template_sections = _sections(template_text)
    matches = [(h, b) for h, b in template_sections if PIN_SECTION_ANCHOR in b.lower()]
    if not matches:
        print(f"CANNOT CHECK: no heading in {template_path} has a body mentioning "
              f"{PIN_SECTION_ANCHOR!r} -- the promoted rule looks gone entirely",
              file=sys.stderr)
        return 2
    if len(matches) > 1:
        names = ", ".join(f"'{h}'" for h, _ in matches)
        print(f"CANNOT CHECK: {len(matches)} headings in {template_path} mention "
              f"{PIN_SECTION_ANCHOR!r} ({names}) -- ambiguous which one is the rule",
              file=sys.stderr)
        return 2
    heading, body = matches[0]
    body_lower = body.lower()

    failures: list[str] = []

    if PIN_MACHINERY_BAN not in body_lower:
        failures.append(
            f"template section '{heading}' lost the machinery-narration ban "
            f"(expected substring {PIN_MACHINERY_BAN!r})"
        )
    if PIN_NO_BLIND_DECISION not in body_lower:
        failures.append(
            f"template section '{heading}' lost the no-blind-technical-decision "
            f"rule (expected substring {PIN_NO_BLIND_DECISION!r}) -- this is the half "
            f"added for the mid-session incident, not the original close-phase rule"
        )

    # --- B + C: session-close.md points at it, never duplicates it ---------
    # session-close.md's rule paragraph is prose under Phase/H2 headings, not
    # necessarily its own heading -- search the whole file for the anchor
    # sentence rather than requiring it to head a section.
    sc_lower = session_close_text.lower()
    occurrences = sc_lower.count(PIN_SECTION_ANCHOR)
    if occurrences == 0:
        failures.append(
            f"{session_close_path} no longer mentions {PIN_SECTION_ANCHOR!r} at all "
            f"-- the pointer back to the promoted rule is gone"
        )
    else:
        if occurrences > 1:
            failures.append(
                f"{session_close_path} mentions {PIN_SECTION_ANCHOR!r} {occurrences} "
                f"times -- ambiguous, expected exactly one pointer paragraph"
            )
        idx = sc_lower.find(PIN_SECTION_ANCHOR)
        window = session_close_text[idx: idx + POINTER_WINDOW]
        window_lower = window.lower()
        if heading.lower() not in window_lower:
            failures.append(
                f"{session_close_path}'s pointer does not name the current template "
                f"heading '{heading}' -- it points at a stale or renamed section"
            )
        if PIN_MACHINERY_BAN in window_lower:
            failures.append(
                f"{session_close_path} still carries the machinery-narration ban "
                f"inline ({PIN_MACHINERY_BAN!r} found near the pointer) -- this is now "
                f"a SECOND copy of the template rule, and the two will diverge the "
                f"first time only one of them is edited. Replace it with a pointer."
            )

    print(f"template:      {template_path}  (section: '{heading}')")
    print(f"session-close: {session_close_path}")
    if failures:
        print("")
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"\nFAILED: {len(failures)} non-dev register sync violation(s)")
        return 1
    print("OK - the non-dev register rule is promoted, single-sourced, and both halves are present")
    return 0


def main() -> int:
    # Windows cp1252-console safety: force UTF-8 so a non-ASCII print (this
    # file's messages quote headings that contain an em dash) can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE),
                     help="the always-loaded CLAUDE.md template (default: the shipped one)")
    ap.add_argument("--session-close", default=str(DEFAULT_SESSION_CLOSE),
                     help="the session-close rule file (default: the shipped one)")
    args = ap.parse_args()
    return check(Path(args.template).expanduser(), Path(args.session_close).expanduser())


if __name__ == "__main__":
    sys.exit(main())
