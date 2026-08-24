#!/usr/bin/env python3
"""_baseline_sections.py - make a ratchet baseline's section headers self-checking.

THE BUG CLASS THIS EXISTS FOR
    Both SHA-pinned baselines in this repo group their rows under a counted
    section header:

        # ---- SEV-4-json-encoded  (42 file(s)) ----
        <sha256>  SEV-4-json-encoded  hooks/some-hook.py
        ...

    Every checker computes its real counts itself and skips `#` lines, so a
    header that no longer matches the rows beneath it breaks nothing, fails
    nothing, and is invisible to CI. It only lies to the human reading it.

    Measured on origin/main before this module landed: SEV-4 declared 42 files
    over 26 rows, SEV-3 declared 6 over 3, SEV-B declared 16 over 15, and SEV-E
    declared 15 files / 17 reads over 12 files / 14 reads. Four headers, all
    stale, all green.

    That is the entire point of a ratchet destroyed. A backlog exists so a
    reader can see it shrinking, and a burn-down number that is hand-decremented
    goes stale on the first row anyone burns down. utf8-stdout-baseline.txt
    already says this in its own prose -- "A ledger that has to be
    hand-decremented is a ledger that lies" -- and then carried two
    hand-decremented headers regardless. Prose cannot enforce. This can.

THE RULE
    A `# ---- <TAG>  (N file(s)[, M read(s)]) ----` header must describe exactly
    the rows between it and the next header (or EOF):
      - N equals the number of rows in the section
      - every row in the section carries the section's own TAG
      - M, where the format declares reads, equals the real read count -- which
        only the caller can know, so it is verified by check_read_counts()
    and every row must sit inside some counted section.

WHY THESE COUNTS ARE WELL-DEFINED
    A row pins its file by SHA over normalized content, so while the row lives
    the file is byte-identical to when it was pinned and its read count cannot
    move. The declared numbers can therefore only go stale when rows are
    DELETED -- the burn-down -- which is exactly the edit this module catches.

HOW EACH BASELINE IS ENFORCED
    scripts/vault-root-read-baseline.txt
        Inside scripts/check-vault-root-reads.py, both halves: load_baseline()
        checks the FILE counts, and check_all() checks the READ counts once the
        ratchet is otherwise clean (only the scanner can count reads).
    scripts/utf8-stdout-baseline.txt
        From the CLI below, wired into scripts/ci.sh and lint.yml. NOT from
        inside scripts/check-utf8-stdout.py, deliberately: that file is itself
        content-pinned by a THIRD ratchet (scripts/cloud-safe-walker-baseline.txt,
        "edited files must adopt safe_read"), so editing it to add two lines
        would oblige a safe_read migration and a restructure of the negative
        controls in scripts/test-utf8-console-guard.sh. Enforcing from outside
        costs nothing the gate cares about -- ci.sh is the local pre-push gate
        and lint.yml is CI, which is where every other ratchet here is enforced
        -- and leaves that pin honest.

    declares_reads is symmetric on purpose: a header that omits a declared
    read count fails, and so does one that adds a read count nothing verifies.
    Either way the number cannot end up unchecked, which is the silent-no-op
    this repo treats as worse than no guard at all.

ASCII-only output on purpose - the baselines it reads belong to the
console-encoding guards.

Usage:
    _baseline_sections.py --self-test            # the negative controls
    _baseline_sections.py --check PATH [--reads] # validate one baseline file
                                                 #   --reads = the (N file(s),
                                                 #   M read(s)) two-number format
    _baseline_sections.py --check-all [DIR]      # every *baseline*.txt in DIR
                                                 #   (default: this directory).
                                                 #   Format auto-detected per
                                                 #   file; flat ones exempt;
                                                 #   finding NONE exits 2.

Exit: 0 clean, 1 stale header, 2 usage/IO error.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# `# ---- TAG  (N file(s)[, M read(s)]) ----`. Trailing dashes are decorative
# and a suffix may follow (the cleared tier carries "-- CLEARED by ..."), so
# only the head of the line is anchored. A dashes-only divider has no \S+ token
# after the run and is therefore NOT a header -- but no such divider exists in
# either baseline today, and any new one would make its rows uncounted and fail.
_HEADER_RE = re.compile(r"^#\s*-{3,}\s*(?P<tag>\S+)")

# The counts, wherever they sit on the header line. `file(s)` contains literal
# parens of its own, hence the escapes.
_COUNTS_RE = re.compile(
    r"\(\s*(?P<files>\d+)\s*file\(s\)"
    r"(?:\s*,\s*(?P<reads>\d+)\s*read\(s\))?\s*\)"
)

_SHA_LEN = 64


class Section:
    """One counted tier of a baseline: its declared numbers and its real rows."""

    __slots__ = ("tag", "declared_files", "declared_reads", "rows", "line_no")

    def __init__(self, tag, declared_files, declared_reads, line_no):
        self.tag = tag
        self.declared_files = declared_files
        self.declared_reads = declared_reads
        self.rows = []          # list of (path, row_tag, line_no)
        self.line_no = line_no


def parse_sections(text):
    """(sections, errors) for a baseline's text.

    Structural errors only -- a header whose counts do not parse, a row outside
    any counted section, a row filed under a header for a different tag. The
    numbers themselves are compared by check_row_counts/check_read_counts.

    A baseline with NO section headers is flat and wholly exempt: it publishes
    no per-tier ledger, so it has none that can go stale. (Two of this repo's
    baselines are flat today, and the utf8 guard's hermetic fixture builds a
    flat one.) The orphan-row rule therefore binds only once a file has opted
    into tiers -- which is precisely when a row landing above the first header
    would slip out of every count.
    """
    sections = []
    errors = []
    orphans = []
    current = None

    for line_no, raw in enumerate(text.splitlines(), start=1):
        header = _HEADER_RE.match(raw)
        if header:
            counts = _COUNTS_RE.search(raw)
            if not counts:
                errors.append(
                    "L{n}: section header '{t}' declares no (N file(s)) count. "
                    "Every tier must declare one, or its rows go uncounted."
                    .format(n=line_no, t=header.group("tag"))
                )
                current = None
                continue
            reads = counts.group("reads")
            current = Section(
                tag=header.group("tag"),
                declared_files=int(counts.group("files")),
                declared_reads=int(reads) if reads is not None else None,
                line_no=line_no,
            )
            sections.append(current)
            continue

        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split(None, 2)
        if len(parts) != 3 or len(parts[0]) != _SHA_LEN:
            # Row shape is the calling checker's error to raise, with its own
            # message and exit code. Skipping here keeps that contract intact.
            continue
        row_tag, path = parts[1], parts[2]
        if current is None:
            orphans.append((line_no, path))
            continue
        if row_tag != current.tag:
            errors.append(
                "L{n}: row '{p}' is tagged {rt} but sits under the {st} header "
                "(line {sl}). Per-tier counts are meaningless while it does."
                .format(n=line_no, p=path, rt=row_tag, st=current.tag,
                        sl=current.line_no)
            )
        current.rows.append((path, row_tag, line_no))

    if sections:
        for line_no, path in orphans:
            errors.append(
                "L{n}: row '{p}' sits above every section header, so no "
                "burn-down number covers it.".format(n=line_no, p=path)
            )

    return sections, errors


def check_row_counts(text, declares_reads=False):
    """Every structural error plus every declared-vs-real FILE count mismatch.

    `declares_reads` is this baseline's format, and it is checked both ways: a
    missing read count fails when the format declares them, and a stray read
    count fails when it does not -- so a number can never be declared and left
    unverified.
    """
    sections, errors = parse_sections(text)

    for sec in sections:
        real = len(sec.rows)
        if sec.declared_files != real:
            errors.append(
                "L{n}: {t} header declares {d} file(s) but {r} row(s) follow it. "
                "Correct the header to {r}."
                .format(n=sec.line_no, t=sec.tag, d=sec.declared_files, r=real)
            )
        if declares_reads and sec.declared_reads is None:
            errors.append(
                "L{n}: {t} header declares no read(s) count; this baseline's "
                "format is (N file(s), M read(s)).".format(n=sec.line_no, t=sec.tag)
            )
        if not declares_reads and sec.declared_reads is not None:
            errors.append(
                "L{n}: {t} header declares {m} read(s), but nothing verifies that "
                "number for this baseline. Drop it, or wire check_read_counts()."
                .format(n=sec.line_no, t=sec.tag, m=sec.declared_reads)
            )

    return errors


def check_read_counts(text, reads_by_tag):
    """Declared-vs-real READ count mismatches, given real reads per tag.

    Split from check_row_counts because only the calling checker can scan the
    pinned files. Call it after the ratchet is otherwise clean: with a stale row
    in play the pinned set is not the declared set, and the mismatch reported
    here would be a second symptom of that one cause.
    """
    sections, _ = parse_sections(text)
    errors = []
    for sec in sections:
        if sec.declared_reads is None:
            continue
        real = reads_by_tag.get(sec.tag, 0)
        if sec.declared_reads != real:
            errors.append(
                "L{n}: {t} header declares {d} read(s) but its pinned file(s) "
                "hold {r}. Correct the header to {r}."
                .format(n=sec.line_no, t=sec.tag, d=sec.declared_reads, r=real)
            )
    return errors


# --------------------------------------------------------------------------
# self-test - the negative controls. A guard earns trust by failing on the
# thing it catches, so every case below that expects an error is a real drift
# shape, and the clean cases prove it does not fire on a correct baseline.
# --------------------------------------------------------------------------

_SHA_A = "a" * _SHA_LEN
_SHA_B = "b" * _SHA_LEN

_CLEAN_FILES_ONLY = (
    "# preamble\n"
    "# ---- SEV-1  (2 file(s)) ----\n"
    "{a} SEV-1 hooks/one.py\n"
    "{b} SEV-1 hooks/two.py\n"
    "\n"
    "# ---- SEV-2  (0 file(s)) ----\n"
).format(a=_SHA_A, b=_SHA_B)

_CLEAN_WITH_READS = (
    "# ---- SEV-A  (0 file(s), 0 read(s)) -- CLEARED ------\n"
    "# commentary under a cleared tier is not a row\n"
    "\n"
    "# ---- SEV-B  (2 file(s), 3 read(s)) ----\n"
    "{a} SEV-B scripts/one.py\n"
    "{b} SEV-B scripts/two.py\n"
).format(a=_SHA_A, b=_SHA_B)

def _empty_dir():
    """A real directory with no baselines in it - the wrong-path shape."""
    import tempfile
    return tempfile.mkdtemp(prefix="bl-none-")


def _planted_drift_dir():
    """A baseline this module was never explicitly wired to, carrying drift.

    This is the case the per-file wiring could not cover: a NEW baseline that
    nobody remembered to add to ci.sh. Discovery by glob is what makes it fail.
    """
    import tempfile
    d = tempfile.mkdtemp(prefix="bl-planted-")
    body = (
        "# ---- SEV-NEW  (7 file(s)) ----\n"
        "{a} SEV-NEW hooks/one.py\n"
    ).format(a=_SHA_A)
    Path(d, "brand-new-baseline.txt").write_text(body, encoding="utf-8")
    return d


_CASES = (
    # (name, callable -> errors, expect_error)
    (
        "a correct files-only baseline is clean",
        lambda: check_row_counts(_CLEAN_FILES_ONLY),
        False,
    ),
    (
        "a correct files+reads baseline is clean",
        lambda: check_row_counts(_CLEAN_WITH_READS, declares_reads=True),
        False,
    ),
    (
        "an OVERCOUNTING header is caught (the shape found on origin/main)",
        lambda: check_row_counts(_CLEAN_FILES_ONLY.replace("(2 file(s))", "(42 file(s))")),
        True,
    ),
    (
        "an UNDERCOUNTING header is caught",
        lambda: check_row_counts(_CLEAN_FILES_ONLY.replace("(2 file(s))", "(1 file(s))")),
        True,
    ),
    (
        "a cleared tier that still declares rows is caught",
        lambda: check_row_counts(_CLEAN_FILES_ONLY.replace("(0 file(s))", "(3 file(s))")),
        True,
    ),
    (
        "deleting a row without decrementing the header is caught",
        lambda: check_row_counts(
            _CLEAN_FILES_ONLY.replace("{} SEV-1 hooks/two.py\n".format(_SHA_B), "")
        ),
        True,
    ),
    (
        "a stale READ count is caught even when the file count is right",
        lambda: check_read_counts(_CLEAN_WITH_READS, {"SEV-A": 0, "SEV-B": 2}),
        True,
    ),
    (
        "a correct read count passes",
        lambda: check_read_counts(_CLEAN_WITH_READS, {"SEV-A": 0, "SEV-B": 3}),
        False,
    ),
    (
        "stripping the count off a header is caught, not silently skipped",
        lambda: check_row_counts(_CLEAN_FILES_ONLY.replace("  (2 file(s)) ", " ")),
        True,
    ),
    (
        "a row above the first header is caught once a file has tiers",
        lambda: check_row_counts(
            "{a} SEV-1 hooks/orphan.py\n".format(a=_SHA_A) + _CLEAN_FILES_ONLY
        ),
        True,
    ),
    (
        "a FLAT baseline with no headers at all is exempt, not an error",
        lambda: check_row_counts(
            "# flat baseline - no tiers, so no ledger to go stale\n"
            "{a} SEV-1 hooks/one.py\n"
            "{b} SEV-1 hooks/two.py\n".format(a=_SHA_A, b=_SHA_B)
        ),
        False,
    ),
    (
        "a row filed under the wrong tier header is caught",
        lambda: check_row_counts(
            _CLEAN_FILES_ONLY.replace(
                "{} SEV-1 hooks/two.py".format(_SHA_B),
                "{} SEV-9 hooks/two.py".format(_SHA_B),
            )
        ),
        True,
    ),
    (
        "a missing read count is caught when the format declares reads",
        lambda: check_row_counts(_CLEAN_FILES_ONLY, declares_reads=True),
        True,
    ),
    (
        "an unverifiable read count is caught when the format does not",
        lambda: check_row_counts(_CLEAN_WITH_READS, declares_reads=False),
        True,
    ),
    (
        "the SWEEP fails loud when its glob matches nothing (never a silent pass)",
        lambda: [] if check_all(_empty_dir()) == 2 else ["expected exit 2"],
        False,
    ),
    (
        "the SWEEP catches a drifted baseline it was never explicitly wired to",
        lambda: [] if check_all(_planted_drift_dir()) == 1 else ["expected exit 1"],
        False,
    ),
    (
        "prose naming a historical count is not a header and never fires",
        lambda: check_row_counts(
            "# Counts at the time of pinning: 21 SEV-1, 42 SEV-4, 78 total.\n"
            + _CLEAN_FILES_ONLY
        ),
        False,
    ),
)


def self_test():
    failures = 0
    for name, run, expect in _CASES:
        got = bool(run())
        if got != expect:
            verb = "expected an error, got none" if expect else "expected clean, got an error"
            print("  FAIL {}: {}".format(name, verb))
            failures += 1
        else:
            print("  ok   {}".format(name))
    total = len(_CASES)
    if failures:
        print("_baseline_sections self-test: {} of {} FAILED".format(failures, total))
        return 1
    print("_baseline_sections self-test: {}/{} passed".format(total, total))
    return 0


def check_baseline_file(path, declares_reads=False, quiet=False):
    """Validate one baseline file's FILE counts. 0 clean, 1 stale, 2 IO error.

    `quiet` suppresses only the success line -- check_all() prints its own
    per-file summary. Failures are never quiet.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print("ERROR: cannot read {}: {}".format(path, exc), file=sys.stderr)
        return 2
    drift = check_row_counts(text, declares_reads=declares_reads)
    if drift:
        print("::error::stale section header(s) in {}:".format(path))
        for line in drift:
            print("  {}".format(line))
        print()
        print("  These headers are the burn-down ledger a human reads to decide")
        print("  whether the backlog is shrinking. The checkers compute their own")
        print("  counts and skip every '#' line, so nothing else can catch this.")
        print("  Correct the header to the real count -- do not delete the count.")
        return 1
    if not quiet:
        print("baseline section headers: OK ({})".format(path))
    return 0


# Any baseline this repo grows, without anyone remembering to wire it up. The
# ORIGINAL bug was a hand-maintained count next to machine-checkable data; a
# hand-maintained LIST of which baselines to check is the same bug one level up,
# and it fails the same silent way -- the new file just never gets looked at.
_BASELINE_GLOB = "*baseline*.txt"


def _declares_reads(text):
    """True if ANY header in this file publishes a read count.

    Per-file, not a global switch: the two tiered baselines use different
    formats, and a future one picks its own. Detecting it here keeps the
    symmetry check meaningful WITHIN a file (all its headers must agree)
    without forcing every baseline into one shape.
    """
    for sec in parse_sections(text)[0]:
        if sec.declared_reads is not None:
            return True
    return False


def check_all(directory):
    """Validate every baseline in `directory`. 0 clean, 1 drift, 2 nothing found.

    Finding NOTHING is an ERROR, not a pass. A glob that quietly matches zero
    files reports success while checking nothing, which is precisely the
    silent-no-op this module exists to prevent -- and it is how a wrong path or
    a renamed directory would turn this whole gate into decoration.
    """
    d = Path(directory)
    paths = sorted(d.glob(_BASELINE_GLOB))
    if not paths:
        print(
            "ERROR: no {} found under {} -- this gate checked NOTHING. Fix the "
            "path rather than reading this as a pass.".format(_BASELINE_GLOB, d),
            file=sys.stderr,
        )
        return 2

    worst = 0
    tiered = flat = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print("ERROR: cannot read {}: {}".format(path, exc), file=sys.stderr)
            worst = max(worst, 2)
            continue
        sections, _ = parse_sections(text)
        if not sections:
            flat += 1
            print("  flat   {} (no tiers, no ledger to go stale)".format(path.name))
            continue
        tiered += 1
        rc = check_baseline_file(path, declares_reads=_declares_reads(text), quiet=True)
        worst = max(worst, rc)
        if rc == 0:
            print("  tiered {} ({} tier(s)) OK".format(path.name, len(sections)))
    print(
        "baseline headers: {} tiered checked, {} flat exempt, {} file(s) total"
        .format(tiered, flat, len(paths))
    )
    return worst


def main(argv):
    args = list(argv)
    if "--self-test" in args:
        return self_test()
    if "--check-all" in args:
        rest = [a for a in args if a != "--check-all"]
        directory = rest[0] if rest else str(Path(__file__).resolve().parent)
        return check_all(directory)
    if "--check" in args:
        declares_reads = "--reads" in args
        rest = [a for a in args if a not in ("--check", "--reads")]
        if len(rest) != 1:
            print("usage: _baseline_sections.py --check PATH [--reads]", file=sys.stderr)
            return 2
        return check_baseline_file(rest[0], declares_reads=declares_reads)
    print(
        "usage: _baseline_sections.py --self-test\n"
        "       _baseline_sections.py --check PATH [--reads]\n"
        "       _baseline_sections.py --check-all [DIR]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
