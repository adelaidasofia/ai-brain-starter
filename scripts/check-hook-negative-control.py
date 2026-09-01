#!/usr/bin/env python3
"""Fail when a shipped hook has NO test surface -- nothing that could ever prove
it fires.

THE CLASS. A guard that never fires is AMBIGUOUS, and the two readings are
byte-identical from outside:

  * CORRECTLY QUIET -- the dangerous thing never happened. A smoke detector
    that never went off is not a broken smoke detector.
  * DEAD -- the matcher is wrong, the wiring is stale, an exception is
    swallowed. It will also never fire when the dangerous thing DOES happen.

You cannot tell these apart by observation, because "quiet" and "dead" emit the
same signal: nothing. The only thing that separates them is a NEGATIVE CONTROL
-- a test that hands the guard the input it claims to catch and asserts it
fires. Absent that, "we ship guard X" is a claim about a FILE, not about
behavior.

WHY THIS IS A FOURTH, DISJOINT CHECK. The existing checks ask:

  check-hook-activation.py        is it wired?      (can it be reached)
  check-hook-activation.py (B)    is it owned?      (will the installer manage it)
  check-hook-emission-channel.py  can it speak?     (will anyone hear it)
  THIS ONE                        can it be proven? (does anything test it)

All four can disagree. A hook can be wired, owned, able to speak, and still
have a matcher that matches nothing on any real input. Overlapping these would
double-report one file, so this check does exactly one thing.

WHAT THIS PREDICATE IS, PRECISELY. It asserts that at least one test surface in
the repo NAMES the hook. That is a FLOOR, not proof:

  * It DOES catch the worst case -- a guard shipped with zero test surface,
    where no one has ever attempted to prove it can fire. That is the
    population this check exists for.
  * It does NOT prove the test actually exercises the failure path, or that
    the assertion is meaningful. A test naming a hook can still be vacuous.

Claiming more than this would be the same error the check is about, so the
message says "no test surface", never "untested" or "unproven".

A CONTROL CAN ALSO FAIL FOR THE WRONG REASON, which is subtler than vacuous
and reads as more rigorous. Measured 2026-08-28 (PR #610): a test with four
real assertions, naming the right function, still passed with the fix reverted
-- three fixtures running, because each tripped a DIFFERENT guard in the same
call path (the mkdir guard, then the load guard, then the lock) and every one
of them raised the expected exception TYPE from the wrong SITE. The assertion
was about an outcome, and several distinct failures produce that same outcome;
the more fail-closed guards a function has, the likelier a crude fixture trips
an earlier one. So: revert the specific fix and require RED, and when it stays
green find which guard actually fired instead of adjusting the assertion. Two
harness traps in the same family -- a mutation whose search string no longer
matches silently mutates nothing, and `grep -c '[FAIL]'` scores a CRASHED suite
as zero failures, identical to a clean pass. Assert the mutation applied; read
the exit code.

  * The name match is a SUBSTRING match, so a hook named `retry-budget` counts
    as covered by a test that only mentions `retry-budget-v2`. That error runs
    in the PERMISSIVE direction (a false GREEN, never a false accusation),
    which is the correct bias for a ratchet: it can under-report debt, but it
    can never fail a hook that someone did test.

SCOPE IS THE FAILURE MODE OF THIS KIND OF CHECK. A coverage check that scans
one directory reports every hook tested elsewhere as untested, and the number
looks authoritative either way. This scans EVERY test surface in the repo and
PRINTS the count it scanned, so a reader can see the denominator rather than
trust it. (Found the hard way: a first pass here scanned `tests/` only, missed
`tests/integration/` and `hooks/test_*.py`, and produced a confidently wrong
figure in both directions.)

BASELINE RATCHET. This ships with pre-existing debt, so it runs as a ratchet,
matching scripts/check-hook-activation.py: listed names are tolerated, the list
may never GROW, and a name that gains a test surface or disappears must be
REMOVED (a stale entry fails). Debt can only shrink. A NEW hook cannot ship
without a test surface.

ASCII-only output on purpose -- see scripts/check-utf8-stdout.py.
"""
# exit-contract: ENFORCING


from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Set

REPO = Path(__file__).resolve().parent.parent
HOOK_DIR = REPO / "hooks"

# Bounded reads via the shared primitive (cloud-safe walker ratchet): this is a
# RECURSIVE walker, and a repo checked out on a cloud-synced folder can hand it
# a placeholder or a stalled mount that blocks forever. scripts/
# check-cloud-safe-file-walkers.py enforces this for every recursive reader.
sys.path.insert(0, str(REPO / "hooks"))
from _lib.safe_read import safe_read_text  # noqa: E402

# Extensions a test surface can have. A shell or workflow file that drives a
# hook counts: the question is "does anything attempt to exercise this", not
# "is there a pytest function".
TEST_SUFFIXES = {".py", ".sh", ".yml", ".yaml"}

# Directories that are never test surfaces even if a filename contains "test"
# (e.g. a hook literally named check-*-test-*.py is the SUBJECT, not the test).
SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv"}

# Pre-existing debt, 2026-08-15. Every name here is a shipped hook with NO test
# surface anywhere in the repo -- nothing that could distinguish it working
# from it being dead. Ordered worst-first by blast radius.
#
# The two secret-handling guards at the top are the reason this check exists:
# a secret guard nobody has proven can fire is indistinguishable from no secret
# guard at all, and it fails silently in the one direction that matters.
NO_TEST_BASELINE: Set[str] = {
    # -- secret / data-exposure guards (highest stakes) --
    "block-secret-in-note",
    # -- correctness / process guards --
    "agent-briefing-check",
    "block-branch-switch-with-untracked-build",
    "check-rule-conflicts-on-write",
    "session-turn-counter",
    "snapshot-pending-work-on-stop",
    "list-wip-stashes-on-session-start",
    "retry-budget",
    "permission-denied",
    "pre-compact-context",
    "migrate-to-user-level",
    # -- context injection / routing --
    "inject-best-of-best-on-consulting",
    "inject-love-language-context",
    "route-suggest",
    # -- validators (assert a shape; a dead one passes everything) --
    "validate-mcp-json",
    "validate-skill-frontmatter",
    "validate-subagent-return",
    "validate-calendar-timezone",
    # -- session-stop / worktree surfacers --
    "warn-uncommitted-builds-on-stop",
    "surface-orphan-worktree-snapshots",
    "surface-dependabot-backlog",
    # -- lifecycle / nudges --
    "first-week-checkin",
    "install-ping",
    "sunday-review-nudge",
    "imessage-mcp-auto-export",
    "whatsapp-mcp-auto-export",
}

# THE RATCHET, enforced. The docstring above says this list "may never GROW".
# Nothing enforced that: measured on the parent commit, appending one name
# took it 28 -> 29 and the gate still printed "shrinking baseline" and exited
# 0. Same appendable-suppression-list hole as scripts/check-hook-activation.py
# (fixed there in the same change). Amnesty ratchets DOWN, never up; raising
# this number is a deliberate, reviewable act.
#
# 28 -> 26: `remove-ended-worktree` gained a real test surface, so its amnesty
# was withdrawn. The cap follows the list down to the new length rather than
# keeping the freed slot as slack -- slack is silently re-appendable, which is
# the exact hole the ratchet closed.
NO_TEST_MAX = 26


def is_test_surface(path: Path) -> bool:
    if path.suffix not in TEST_SUFFIXES:
        return False
    if any(part in SKIP_PARTS for part in path.parts):
        return False
    rel = str(path.relative_to(REPO))
    return "test" in path.name.lower() or rel.startswith("tests/")


def collect_test_blob() -> tuple[str, int, List[str]]:
    """Concatenated text of every test surface, how many were scanned, and any
    that could NOT be read.

    An unreadable test surface must never be skipped silently. Skipping shrinks
    the blob, so a hook whose ONLY test lives in the unreadable file is then
    reported as having no test surface -- a false accusation produced by an I/O
    error, indistinguishable from a real finding. A coverage check whose
    denominator can silently shrink is the exact defect this script exists to
    catch, so unreadable surfaces are returned and reported, never swallowed.

    Every non-ok status counts as unreadable, including `too-large` and
    `binary`: truncating or skipping either one shrinks the blob just as much
    as an EACCES would."""
    parts: List[str] = []
    unreadable: List[str] = []
    n = 0
    for p in REPO.rglob("*"):
        if not p.is_file() or not is_test_surface(p):
            continue
        res = safe_read_text(p, timeout=5.0, max_bytes=8_000_000)
        if not res.ok:
            detail = f" {res.detail}" if res.detail else ""
            unreadable.append(f"{p.relative_to(REPO)}: {res.status}{detail}")
            continue
        parts.append(res.text or "")
        n += 1
    return "\n".join(parts), n, unreadable


def shipped_hooks() -> List[str]:
    """Hook basenames, excluding the tests that live alongside them."""
    return sorted(
        p.stem for p in HOOK_DIR.glob("*.py")
        if not p.name.startswith("test_")
    )


def evaluate(hooks: List[str], blob: str, baseline: Set[str],
             baseline_max: int | None = None) -> tuple[List[str], List[str]]:
    """Pure core: (problems, hooks-with-no-test-surface). Kept free of I/O so
    --selftest can drive it with synthetic inputs instead of writing probe files
    into hooks/, which would race a concurrent run and leave debris on crash."""
    missing = [h for h in hooks if h not in blob]
    problems: List[str] = []

    if baseline_max is not None and len(baseline) > baseline_max:
        problems.append(
            f"NO_TEST_BASELINE has {len(baseline)} entries but NO_TEST_MAX is "
            f"{baseline_max}.\n    The baseline may only SHRINK. Add a test "
            f"surface for the hook instead of widening the amnesty list."
        )

    for name in missing:
        if name in baseline:
            continue
        problems.append(
            f"hooks/{name}.py has NO test surface anywhere in the repo.\n"
            f"    Nothing can tell this guard working from dead -- both emit\n"
            f"    silence. Add a test that feeds it the input it claims to\n"
            f"    catch and asserts it fires, or add it to NO_TEST_BASELINE\n"
            f"    with a reason (scripts/check-hook-negative-control.py)."
        )

    # Ratchet: an entry that no longer needs excusing must be removed, else the
    # list silently re-accumulates slack and stops meaning anything.
    have, missing_set = set(hooks), set(missing)
    for name in sorted(baseline):
        if name not in have:
            problems.append(
                f"'{name}' is on NO_TEST_BASELINE but no such hook exists.\n"
                f"    Remove the stale entry."
            )
        elif name not in missing_set:
            problems.append(
                f"'{name}' is on NO_TEST_BASELINE but now HAS a test surface.\n"
                f"    Remove it -- the debt is paid."
            )
    return problems, missing


def selftest() -> int:
    """A guard earns trust only by failing on the thing it catches.

    Each case asserts this check goes RED for one distinct reason. If any case
    passes when it should fail, this check is decorative and CI must say so."""
    cases = [
        ("a NEW hook with no test surface must FAIL",
         ["already-tested", "brand-new-guard"], "already-tested is named here", set()),
        ("a STALE baseline entry (hook deleted) must FAIL",
         ["already-tested"], "already-tested is named here", {"ghost-hook"}),
        ("a PAID-OFF baseline entry (now tested) must FAIL",
         ["already-tested"], "already-tested is named here", {"already-tested"}),
    ]
    failures = 0
    for label, hooks, blob, baseline in cases:
        problems, _ = evaluate(hooks, blob, baseline)
        if problems:
            print(f"  ok: {label}")
        else:
            print(f"  FAIL: {label} -- check stayed GREEN")
            failures += 1

    # And the inverse: a clean tree must NOT fail, or the check cries wolf and
    # gets ignored -- the failure mode that makes a guard worthless.
    problems, _ = evaluate(["already-tested"], "already-tested is named here", set())

    # THE RATCHET CAP -- the shape that shipped: a hook with no test surface
    # waved through by appending one name. Measured on the parent commit as
    # 28 -> 29, exit 0, output still reading "shrinking".
    over, _ = evaluate(["g"], "", {"g"}, 0)
    print(f"{'PASS' if over else 'FAIL'}  baseline OVER cap -> FAIL")
    at, _ = evaluate(["g"], "", {"g"}, 1)
    print(f"{'PASS' if not at else 'FAIL'}  baseline AT cap -> pass")
    nocap, _ = evaluate(["g"], "", {"g"})
    print(f"{'PASS' if not nocap else 'FAIL'}  no cap given -> cap not enforced")
    assert over and not at and not nocap, "ratchet-cap control did not bite"

    if problems:
        print("  FAIL: a fully-tested tree went RED (false alarm)")
        failures += 1
    else:
        print("  ok: a fully-tested tree stays GREEN (no false alarm)")

    print(f"\nnegative-control selftest: {4 - failures}/4 passed")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true",
                    help="print hooks with no test surface and exit 0 (worklist)")
    ap.add_argument("--selftest", action="store_true",
                    help="prove this check goes RED on each thing it claims to catch")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not HOOK_DIR.is_dir():
        print(f"FATAL: no hooks/ directory at {HOOK_DIR}.", file=sys.stderr)
        print("Refusing to report -- a zero-hook run would print a clean pass.",
              file=sys.stderr)
        return 2

    blob, n_surfaces, unreadable = collect_test_blob()
    if unreadable:
        print("FATAL: could not read some test surfaces:", file=sys.stderr)
        for u in unreadable:
            print(f"  {u}", file=sys.stderr)
        print("Every hook whose only test lives in one of these would be "
              "reported as\nhaving no test surface -- an accusation manufactured "
              "by an I/O error.\nRefusing to report.", file=sys.stderr)
        return 2
    if n_surfaces == 0:
        print("FATAL: scanned 0 test surfaces.", file=sys.stderr)
        print("Every hook would be reported as untested, which is a bug in this",
              file=sys.stderr)
        print("check, not a finding about the hooks. Refusing to report.",
              file=sys.stderr)
        return 2

    hooks = shipped_hooks()
    if not hooks:
        print(f"FATAL: found 0 shipped hooks under {HOOK_DIR}.", file=sys.stderr)
        return 2

    problems, missing = evaluate(hooks, blob, NO_TEST_BASELINE, NO_TEST_MAX)

    if args.list:
        for h in missing:
            print(h)
        return 0

    covered = len(hooks) - len(missing)
    print(f"hook negative control: {covered}/{len(hooks)} shipped hooks have a "
          f"test surface; {len(missing)} on the shrinking baseline "
          f"({n_surfaces} test surfaces scanned).")

    if problems:
        print("")
        for p in problems:
            print(f"  {p}")
        print(f"\nhook negative control FAILED -- {len(problems)} problem(s).")
        return 1

    print("hook negative control OK -- no hook ships without a test surface "
          "or a baseline entry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
