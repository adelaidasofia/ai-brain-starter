#!/usr/bin/env python3
"""Ban a moving ref as the "before" state of a test.

THE BUG CLASS THIS EXISTS FOR

A negative control proves a test can fail. To do that it needs a "before"
source: the code as it was when the bug was live. If that source is read from
a MOVING ref -- `git show origin/main:file` -- the control is guaranteed to
pass in the PR that introduces it and guaranteed to fail forever after it
lands, because the merge is what makes origin/main the fixed code. From then
on the control runs the fix against itself.

It is invisible in review. It cannot be caught by running it, because it is
green every time you run it before merging. It is caught by reading it, or by
this guard.

Measured, 2026-08-16: this shape took main red for five consecutive commits
and blocked every merge in the repo.

WHAT IS ALLOWED

  - a vendored fixture under tests/fixtures/  (immune to ref movement, to
    history rewrites, AND to clone depth -- a shallow CI checkout has no old
    objects, so even a pinned SHA is not always reachable)
  - a pinned full-length SHA, with a comment saying why
  - reading a moving ref from a sandbox repo the test BUILT itself, which is a
    different thing entirely: those tests create a throwaway repo in a temp dir
    and assert against its own origin/main

WHAT IS BANNED

  reading a moving ref out of THIS repo's real history (`git -C "$REPO_ROOT"`,
  or a bare `git` whose cwd is the repo) to obtain a before/pre-fix state.

The distinction is structural, not a phrase match: it keys on WHICH repo is
being read, so it cannot be evaded by rewording a comment.

ESCAPE HATCH

  # frozen-before-state: <reason>

on the offending line or the line above. A guard that refuses without a
sanctioned path is an outage, not a safety net.

WHAT THIS DOES NOT CATCH (say it here, so nobody trusts it further than it goes)

This is a pattern matcher over source text, which makes it a denylist against
an open set -- and a denylist against an open set always loses. It is an
ACCIDENT catcher, not an adversary-proof control. Known evasions, none of them
exotic:

  REF=origin/main; git show "$REF:file"     # ref through a variable
  git show "origin/main":file               # ref quoted separately
  SHA=$(git rev-parse origin/main); git show "$SHA:file"   # resolved first

Each of these is still the same bug, and this guard is silent on all three.
The structural fix for the class is upstream of here: make the CI job that
runs the tests resolve the same refs a push-to-main run does, so a
before-state defect fails on the PR instead of after the merge. Until that
lands, treat a green from this script as "the obvious spelling is absent",
never as "the class is impossible".

Usage:
  check-frozen-before-state.py [--root DIR]   scan (exit 1 on any finding)
  check-frozen-before-state.py --self-test    prove the guard fires and abstains
"""
# exit-contract: ENFORCING


from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from _lib.safe_read import safe_read_text  # noqa: E402

# Bounds for the shared safe_read primitive. Test sources are small; 4 MB is
# far above any real one and still refuses a runaway or a cloud placeholder.
READ_TIMEOUT_S = 5.0
MAX_TEST_BYTES = 4 * 1024 * 1024

# Refs whose contents change as the branch advances. HEAD~N / HEAD^ are moving
# for the same reason: they are relative to whatever HEAD happens to be.
MOVING_REF = r"(?:origin/(?:main|master|HEAD)|@\{upstream\}|HEAD[~^]\d*|(?<![\w/.-])(?:main|master)(?=:))"

# A git read of a blob or tree at a ref: `git show <ref>:<path>`, `git cat-file
# ... <ref>:<path>`, `git rev-parse <ref>:<path>`. The `:<path>` is what makes it
# a CONTENT read (a before-state) rather than a mere commit-distance check --
# `git rev-list --count origin/main` is fine and stays fine.
GIT_CONTENT_READ = re.compile(
    r"git\b(?P<args>[^\n;|&]*?)\b(?:show|cat-file|rev-parse)\b[^\n;|&]*?"
    r"(?P<ref>" + MOVING_REF + r"):(?P<path>[\w./$\"'{}-]+)"
)

# `-C <target>`. When the target is the real repo (or absent, meaning cwd), the
# read is against this repo's history. A sandbox target -- any other variable or
# a temp path -- is the legitimate case.
DASH_C = re.compile(r"-C\s+(?P<target>\"[^\"]+\"|'[^']+'|\S+)")

REAL_REPO_TARGETS = {"$REPO_ROOT", "${REPO_ROOT}", "$ROOT", "${ROOT}", ".", "$PWD", "${PWD}"}

EXEMPT = re.compile(r"#\s*frozen-before-state:\s*\S")

SCAN_SUFFIXES = {".sh", ".bash", ".py", ".ps1"}


def strip_comment(line: str) -> str:
    """Drop the comment tail, so PROSE ABOUT the banned pattern is not a finding.

    This is not leniency. The whole point of fixing an instance of this class is
    to leave a comment explaining what the old code did and why it self-destructs
    -- the fix for the 2026-08-16 incident does exactly that, and the first
    version of this guard flagged that explanation as the defect. A guard that
    reds on its own documentation gets bypassed, and then it guards nothing.

    Code after an unquoted `#` does not execute, so removing it cannot hide a
    real finding. A `#` is only a comment at the start of a word (bash: `a#b` is
    one word, not a comment), and never inside quotes.
    """
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or line[i - 1].isspace():
                return line[:i]
    return line


def _targets_real_repo(args: str) -> bool:
    """True when this git invocation reads THIS repo rather than a sandbox one."""
    m = DASH_C.search(args)
    if not m:
        return True  # no -C: runs in the repo's own working dir
    target = m.group("target").strip("\"'")
    return target in REAL_REPO_TARGETS


def scan_text(text: str, rel: str) -> list[str]:
    findings: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines, start=1):
        prev = lines[i - 2] if i >= 2 else ""
        # The escape hatch lives IN a comment, so it is read from the raw line.
        if EXEMPT.search(line) or EXEMPT.search(prev):
            continue
        for m in GIT_CONTENT_READ.finditer(strip_comment(line)):
            if not _targets_real_repo(m.group("args")):
                continue
            findings.append(
                f"{rel}:{i}: reads '{m.group('ref')}:{m.group('path')}' from this repo's "
                f"history as a before-state. A moving ref self-destructs on merge -- vendor a "
                f"fixture under tests/fixtures/, or pin a full SHA and say why. "
                f"Escape hatch: '# frozen-before-state: <reason>'."
            )
    return findings


def scan_tree(root: Path) -> list[str]:
    findings: list[str] = []
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return findings
    for path in sorted(tests_dir.rglob("*")):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if "fixtures" in path.relative_to(root).parts:
            continue  # a fixture IS the frozen snapshot; its contents are data
        # Bounded, symlink-refusing read through the shared safe_read primitive
        # rather than a bare read_text. A recursive rglob can reach a FIFO, a
        # cloud-placeholder file that blocks on materialisation, or something
        # far larger than any test source; safe_read is the one reviewed reader
        # in this repo that handles all three, and check-cloud-safe-file-walkers
        # enforces its use on exactly this shape (recursive walk -> content read).
        res = safe_read_text(
            path, timeout=READ_TIMEOUT_S, max_bytes=MAX_TEST_BYTES, errors="replace"
        )
        if not res.ok:
            # Unreadable is a FINDING, never a silent pass: a file this guard
            # cannot read is a file it cannot clear.
            findings.append(f"{path.relative_to(root)}: could not read ({res.status})")
            continue
        findings.extend(scan_text(res.text, str(path.relative_to(root))))
    return findings


# --- negative control -------------------------------------------------------
# Every expected value below is written out literally. None of it is computed by
# calling the scanner, because a self-test that builds its expectation from the
# code under test only ever proves the code agrees with itself.

_MUST_FLAG = [
    ('git -C "$REPO_ROOT" show origin/main:bootstrap.sh > "$PREFIX_SRC"', "the exact shape that took main red"),
    ('git show origin/main:install.sh > before.sh', "bare git, cwd is the repo"),
    ('git -C . cat-file blob main:scripts/ci.sh', "-C . is still this repo"),
    ('git -C "$REPO_ROOT" show HEAD~1:bootstrap.sh', "HEAD~1 moves with HEAD"),
    ('git -C "$REPO_ROOT" show @{upstream}:bootstrap.sh', "upstream moves too"),
    ('git -C "$REPO_ROOT" show origin/main:b.sh > "$OUT"  # grab the old one',
     "a trailing comment must not let a real finding through"),
]

_MUST_PASS = [
    ('git -C "$CO" show origin/main:file.sh', "sandbox repo the test built itself"),
    ('git -C "$TMP/clone" rev-parse origin/main', "sandbox, and no :path content read"),
    ('git -C "$REPO_ROOT" rev-list --count origin/main', "commit distance, not a content read"),
    ('git -C "$REPO_ROOT" show 27d5243a9f1c4b8e:bootstrap.sh', "pinned SHA, immutable"),
    ('PREFIX_SRC="$REPO_ROOT/tests/fixtures/bootstrap-prefix.sh"', "vendored fixture"),
    ('git -C "$REPO_ROOT" show origin/main:x.sh  # frozen-before-state: reports upstream news', "explicit escape hatch"),
    # Both of these are real lines from the fix for the 2026-08-16 incident. The
    # first version of this guard flagged them -- it reported the explanation of
    # the bug as the bug. Kept as fixtures so that regression cannot come back.
    ('# This check used to read it with `git show origin/main:bootstrap.sh`. That was',
     "prose documenting the banned pattern is not the pattern"),
    ('#      bootstrap.sh (git show origin/main:bootstrap.sh) and assert it DOES',
     "indented header prose, same shape"),
]


def self_test() -> int:
    failures = []
    for snippet, why in _MUST_FLAG:
        if not scan_text(snippet, "t.sh"):
            failures.append(f"FALSE NEGATIVE ({why}): {snippet}")
    for snippet, why in _MUST_PASS:
        got = scan_text(snippet, "t.sh")
        if got:
            failures.append(f"FALSE POSITIVE ({why}): {snippet}\n    -> {got[0]}")
    if failures:
        print("check-frozen-before-state SELF-TEST FAILED", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(
        f"check-frozen-before-state self-test OK "
        f"({len(_MUST_FLAG)} flagged, {len(_MUST_PASS)} correctly ignored)"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    # The scan is only trustworthy if the scanner works. Prove it every run --
    # a scanner that silently matches nothing reports a clean tree.
    if self_test() != 0:
        return 1

    findings = scan_tree(Path(args.root))
    if findings:
        print("\nmoving-ref before-state found:\n", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print(
            "\nWhy this is not style: a control whose before-state is a moving ref passes in "
            "the PR that adds it and fails forever once that PR lands.\n",
            file=sys.stderr,
        )
        return 1
    print("check-frozen-before-state: no moving-ref before-state in tests/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
