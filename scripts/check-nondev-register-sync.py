#!/usr/bin/env python3
"""Prove the non-developer plain-language register REACHES people, and that every
copy of it says the same thing.

The incident: a non-technical user ran a skill mid-session (not close). The model
narrated a raw revision id, ".gitignore" semantics, parallel sessions and
worktrees at her, told her to paste an API key into a shell config, then ended
the turn asking her to decide whether to save dozens of changed files to git --
a call she had no basis to make. The rule that forbids all of this existed, but
only inside the session-close rule file, so it governed one phase.

The trap this script exists to close: SYNC IS NOT REACH. Comparing two files in
this repo to each other stays green forever while the rule reaches zero
installed vaults. scripts/drift-check.sh is the only mechanism that carries repo
content into an already-installed vault, and it has exactly four scopes. A file
outside all four propagates to nobody -- templates/generated/claude-md-template.md
is in none of them, so it reaches NEW installs only, never an existing one.

So this script checks three things:

  REACH   -- at least one declared HOME sits on a real drift-check propagation
             scope, with the scopes PARSED OUT OF drift-check.sh itself (never a
             hardcoded filename), so moving the rule off a scope, or deleting a
             scope from drift-check, fails here instead of silently.
  AGREE   -- every copy (homes plus new-install copies) carries a byte-identical
             canonical block, delimited by NONDEV-REGISTER markers. Two copies of
             one rule diverge the first time only one gets edited.
  INTACT  -- the block still carries every half of the rule, pinned by substring
             so prose stays free to reword: the machinery-narration ban, the
             no-blind-technical-decision rule, the credential-paste ban, and the
             every-phase scope statement.

Exit 0 = wired. Exit 1 = drifted (see message). Exit 2 = could not check (fail
loud; a checker that cannot read its inputs must never report success).

Pure stdlib. Usage:
    python3 scripts/check-nondev-register-sync.py
    python3 scripts/check-nondev-register-sync.py --repo-root PATH
"""
# exit-contract: ENFORCING


from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

DRIFT_CHECK = "scripts/drift-check.sh"

# Files carrying the canonical block that MUST reach an already-installed vault.
# Each one is checked against the scopes parsed out of drift-check.sh; a home
# that no scope covers fails REACH.
HOMES = (
    "templates/rules/session-close.md",
    "templates/rules/session-start-checks.md",
)

# Files carrying the canonical block that reach NEW installs only. Deliberately
# NOT counted as reach: drift-check Scope C only updates a CLAUDE.md heading that
# is ALREADY present in the user's file ("Heading not present. The rule isn't
# installed ... Don't report drift"), so a brand-new section in the generated
# template never lands in an existing vault's CLAUDE.md.
NEW_INSTALL_COPIES = ("templates/generated/claude-md-template.md",)

MARK_START = "<!-- NONDEV-REGISTER:START"
MARK_END = "<!-- NONDEV-REGISTER:END -->"
BLOCK_RE = re.compile(re.escape(MARK_START) + r".*?" + re.escape(MARK_END), re.DOTALL)

# Substring pins -- meaning, never exact wording.
PINS = {
    "machinery-narration ban": "narrate machinery",
    "no-blind-technical-decision rule": "technical either/or",
    "credential-paste ban": "paste a credential",
    "every-phase scope": "every phase",
}

# A shell word following "$STARTER_DIR/" in drift-check.sh, up to the first
# character that ends it.
_STARTER_REF = re.compile(r"\$STARTER_DIR/([^\s;)\]]+)")
_SHELL_VAR = re.compile(r"\$\{?\w+\}?")

# drift-check declares four scopes; scopes A, B and C/D each contribute at least
# one distinct source root. Parsing fewer than this means the file's shape moved
# out from under the parser -- fail loud rather than silently declare "no scope
# covers the rule" (which would read as a rule problem, not a parser problem).
MIN_EXPECTED_SCOPES = 3


def parse_propagation_scopes(drift_check_text: str) -> list:
    """Repo-relative source patterns drift-check.sh actually propagates FROM.

    Derived from the real "$STARTER_DIR/..." references rather than a hardcoded
    list, so adding, moving or deleting a scope changes this answer. Shell
    variables collapse to '*' (Scope B's curated $name list is a subset of
    scripts/*, so this over-approximates B and never under-approximates it --
    the homes this script guards live under templates/rules, where C and D are
    exact).
    """
    out = []
    for raw in _STARTER_REF.findall(drift_check_text):
        pat = raw.replace('"', "").replace("'", "")
        pat = _SHELL_VAR.sub("*", pat).rstrip("/")
        # A pattern that collapsed to bare "*" (e.g. "$STARTER_DIR/$SOMEVAR")
        # conveys no scope and would make every REACH check vacuously green.
        if pat and pat != "*" and pat not in out:
            out.append(pat)
    return out


def scope_covering(rel_path: str, scopes: list):
    """The first parsed scope pattern that covers rel_path, or None."""
    for pat in scopes:
        if "*" in pat:
            if PurePosixPath(rel_path).match(pat):
                return pat
        elif rel_path == pat or rel_path.startswith(pat + "/"):
            return pat
    return None


def _read(path: Path) -> str:
    """Text, or raise a message-carrying RuntimeError. OSError covers
    missing/unreadable; UnicodeDecodeError (a ValueError, NOT an OSError) covers
    a non-UTF-8 file. Both mean CANNOT CHECK."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"{path} ({exc})") from exc


def extract_block(text: str):
    m = BLOCK_RE.search(text)
    if not m:
        return None
    return m.group(0).replace("\r\n", "\n").replace("\r", "\n")


def check(repo_root: Path) -> int:
    # --- inputs -------------------------------------------------------------
    try:
        drift_text = _read(repo_root / DRIFT_CHECK)
    except RuntimeError as exc:
        print(f"CANNOT CHECK: drift-check unreadable: {exc}", file=sys.stderr)
        return 2

    scopes = parse_propagation_scopes(drift_text)
    if len(scopes) < MIN_EXPECTED_SCOPES:
        print(
            f"CANNOT CHECK: parsed only {len(scopes)} propagation source(s) from "
            f"{DRIFT_CHECK} ({scopes or 'none'}); expected at least "
            f"{MIN_EXPECTED_SCOPES}. drift-check's shape changed -- re-read it and "
            f"fix this parser before trusting any REACH verdict.",
            file=sys.stderr,
        )
        return 2

    blocks = {}
    for rel in HOMES + NEW_INSTALL_COPIES:
        try:
            text = _read(repo_root / rel)
        except RuntimeError as exc:
            print(f"CANNOT CHECK: {exc}", file=sys.stderr)
            return 2
        block = extract_block(text)
        if block is None:
            print(
                f"CANNOT CHECK: {rel} carries no {MARK_START}...{MARK_END} block -- "
                f"the rule looks gone from a file declared to hold it",
                file=sys.stderr,
            )
            return 2
        blocks[rel] = block

    failures = []

    # --- REACH --------------------------------------------------------------
    reached = []
    for rel in HOMES:
        pat = scope_covering(rel, scopes)
        if pat is None:
            failures.append(
                f"REACH: {rel} is on no drift-check propagation scope "
                f"(parsed: {', '.join(scopes)}) -- an already-installed vault would "
                f"never receive the rule from it"
            )
        else:
            reached.append((rel, pat))
    if not reached and not failures:
        failures.append("REACH: no home declared at all -- the rule reaches nobody")

    # --- AGREE --------------------------------------------------------------
    canonical_rel = HOMES[0]
    canonical = blocks[canonical_rel]
    for rel, block in blocks.items():
        if rel == canonical_rel:
            continue
        if block != canonical:
            failures.append(
                f"AGREE: {rel}'s block differs from {canonical_rel}'s -- two copies of "
                f"one rule, and they will keep diverging. Copy the canonical block over."
            )

    # --- INTACT -------------------------------------------------------------
    low = canonical.lower()
    for name, pin in PINS.items():
        if pin not in low:
            failures.append(
                f"INTACT: the canonical block lost the {name} (expected substring {pin!r})"
            )

    # --- report -------------------------------------------------------------
    for rel, pat in reached:
        print(f"reach:  {rel}  <- drift-check scope source '{pat}'")
    for rel in NEW_INSTALL_COPIES:
        print(f"copy:   {rel}  (new installs only, not a reach path)")
    if failures:
        print("")
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"\nFAILED: {len(failures)} non-dev register violation(s)")
        return 1
    print("OK - the register reaches installed vaults, every copy agrees, all halves present")
    return 0


def main() -> int:
    # Windows cp1252-console safety: the messages quote headings containing an
    # em dash, so force UTF-8 rather than crash on print.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo-root", default=str(REPO_ROOT),
                    help="repo to check (default: the one this script ships in)")
    args = ap.parse_args()
    return check(Path(args.repo_root).expanduser())


if __name__ == "__main__":
    sys.exit(main())
