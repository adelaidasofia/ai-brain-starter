#!/usr/bin/env python3
"""Negative control for standing_report.

The dangerous failure is not verbosity, it is a CHANGED finding-set rendering
as the quiet digest — that would trade coverage for silence, which is the one
thing this pass must never do. So the controls that matter are: any change
restores the full render, and every degraded path returns full.

Lived at hooks/_lib/standing_report.test.py and ran in NO CI job for its whole
life: the collectors glob `hooks/test_*.py` and `tests/test_*.py`, and the
dormant-suite invariant in scripts/ci.sh globs the same two, so a suite under
_lib/ named `*.test.py` was invisible to the check that exists to catch exactly
this. Same defect ci.sh already documents for warn-recreate-deleted-file.test.py.
Renamed and registered in PY_DIRECT 2026-09-01, which is also how the condense()
controls below became real rather than decorative.
"""
import os
import sys
import pathlib
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _lib.standing_report as sr  # noqa: E402

fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


# Realistic length ON PURPOSE. The digest carries a summary line plus an age
# clause, so a toy 25-char fixture is SHORTER than any digest and the
# never-grow guard correctly hands it back whole — which would silently make
# every compression assertion below vacuous.
_ROWS = "\n".join(f"  - item {i}: some finding with a path and a remedy" for i in range(20))
FULL_A = "[x] 90 items a fresh install will not reproduce. Run the sweep.\n" + _ROWS
FULL_B = "[x] 91 items a fresh install will not reproduce. Run the sweep.\n" + _ROWS + "\n  - item 20: one more"
SUMMARY = "[x] 90 items a fresh install will not reproduce. Run the sweep."

with tempfile.TemporaryDirectory() as td:
    # Pin the state dir through the ENV OVERRIDE, which is the same channel a
    # real caller uses - not by assigning a module global. The old form set
    # sr.STATE_DIR directly, so it bound only while the module happened to read
    # that global; the moment resolution moved into a function the assignment
    # went INERT and this suite silently began writing the developer's real
    # ~/.claude/.standing-reports while still printing ok. Pinning the way
    # production configures it means this file cannot drift out of binding
    # again without failing.
    os.environ[sr.STATE_DIR_ENV] = str(pathlib.Path(td) / "sr")

    print("the pin BINDS (control: everything below is vacuous if it does not)")
    sr.report("bind-probe", FULL_A, SUMMARY)
    check("state landed in the pinned dir, not real HOME",
          (pathlib.Path(td) / "sr" / "bind-probe.json").is_file())
    check("nothing was written to the real state dir",
          not (pathlib.Path.home() / ".claude" / ".standing-reports"
               / "bind-probe.json").exists())

    print("first sight renders IN FULL")
    check("first call returns full", sr.report("k", FULL_A, SUMMARY) == FULL_A)

    print("unchanged set collapses to the digest, carrying count + age")
    r2 = sr.report("k", FULL_A, SUMMARY)
    check("second call is not the full text", r2 != FULL_A)
    check("digest keeps the summary/count", SUMMARY in r2)
    check("digest states it is unchanged", "unchanged" in r2)
    check("digest is much shorter", len(r2) < len(FULL_A) + 60)

    print("NEGATIVE CONTROL: any change restores the FULL render")
    check("changed set returns full", sr.report("k", FULL_B, SUMMARY) == FULL_B)
    check("then collapses again", sr.report("k", FULL_B, SUMMARY) != FULL_B)
    check("reverting is also a change -> full", sr.report("k", FULL_A, SUMMARY) == FULL_A)

    print("the counter names its own unit, and counts SESSIONS when it can")
    sr.report("k", FULL_A, SUMMARY)
    r = sr.report("k", FULL_A, SUMMARY)
    check("no session id -> labelled as fires, not sessions",
          "fire(s)" in r and "session(s)" not in r)
    sr.report("s", FULL_A, SUMMARY, "SESS-1")
    r1 = sr.report("s", FULL_A, SUMMARY, "SESS-1")
    r1b = sr.report("s", FULL_A, SUMMARY, "SESS-1")
    check("3 fires in one session still reads 1 session",
          "1 session(s)" in r1b and "1 session(s)" in r1)
    r2 = sr.report("s", FULL_A, SUMMARY, "SESS-2")
    check("a second session increments to 2", "2 session(s)" in r2)

    print("a digest longer than the text it replaces is never returned")
    SHORT = "[t] 2 things\n- a"
    sr.report("short", SHORT, "[t] 2 things")
    check("short render passes through unchanged",
          sr.report("short", SHORT, "[t] 2 things") == SHORT)

    print("keys are isolated")
    check("other key renders full", sr.report("k2", FULL_A, SUMMARY) == FULL_A)

    print("detail pointer is cited once recorded")
    sr.set_detail_path("k3", "/tmp/detail.txt")
    sr.report("k3", FULL_A, SUMMARY)
    check("digest cites detail path", "/tmp/detail.txt" in sr.report("k3", FULL_A, SUMMARY))

    print("FAIL-OPEN: degraded paths return FULL, never the digest")
    check("empty full passes through", sr.report("k", "", SUMMARY) == "")
    os.environ[sr.BYPASS_ENV] = "1"
    check("bypass returns full", sr.report("k", FULL_A, SUMMARY) == FULL_A)
    del os.environ[sr.BYPASS_ENV]

    # A path under an unwritable root: mkdir raises inside _write, report()
    # catches, and the caller gets the FULL text rather than a wrong digest.
    # /dev/null is a FILE on every POSIX box, so a child directory of it can
    # never be created - portable where /proc is not (it does not exist on
    # macOS, which made this control vacuous there).
    os.environ[sr.STATE_DIR_ENV] = str(pathlib.Path("/dev/null") / "sr")
    check("unwritable state returns full", sr.report("k", FULL_A, SUMMARY) == FULL_A)

    sr2 = pathlib.Path(td) / "sr2"
    sr2.mkdir(parents=True, exist_ok=True)
    (sr2 / "k.json").write_text("{corrupt", encoding="utf-8")
    os.environ[sr.STATE_DIR_ENV] = str(sr2)
    check("corrupt state returns full", sr.report("k", FULL_A, SUMMARY) == FULL_A)

    print("condense(): a MULTI-SECTION render keeps EVERY section's verdict")
    # The live regression. dev-hub-refresh joins several independent verdicts
    # into one string before condensing; condense() summarised only the opener,
    # so every section after the first vanished from the digest while the
    # digest still looked healthy. Measured 2026-09-01 on a real machine: a
    # 3-section render collapsed to one 229-char line naming only the first,
    # silencing the un-backed-up-work verdict on every session after the first.
    def _rows(tag, n):
        return "\n".join(f"  - `repo-{i}` :: `claude/{tag}-{i}` (3 commits, idle 41d)"
                          for i in range(n))
    MULTI = ("[claimed-work-stranded]\n\n"
             "at least 5 branches carry CLAIMED work no other session can act on.\n"
             + _rows("claim", 10) + "\n\n"
             "[unpushed-commits-surface]\n\n"
             "2 item(s) at real LOST-WORK risk — push/back-up now.\n"
             + _rows("drift", 10) + "\n\n"
             "[unverifiable-backup-state]\n\n"
             "1 replica has not been restore-drilled in 63 days.\n"
             + _rows("backup", 10))
    os.environ[sr.STATE_DIR_ENV] = str(pathlib.Path(td) / "sr-multi")
    check("first multi-section render is full", sr.condense("m", MULTI) == MULTI)
    m = sr.condense("m", MULTI)
    # Vacuity guard: a digest that is not shorter is the never-grow guard
    # handing back FULL, and every assertion below would then pass against the
    # full text while proving nothing.
    check("multi-section digest is genuinely shorter", len(m) < len(MULTI))
    for tag in ("[claimed-work-stranded]", "[unpushed-commits-surface]",
                "[unverifiable-backup-state]"):
        check(f"digest keeps the {tag} verdict", tag in m)
    check("digest keeps each section's FIGURE",
          "5 branches" in m and "2 item(s)" in m and "63 days" in m)
    check("digest drops the ENUMERATION (that is the whole compression)",
          "`repo-7`" not in m)
    check("digest still states it is unchanged", "unchanged" in m)

    print("condense(): a SINGLE-section render is byte-identical to before")
    # The one-section path must not move: it is the path every other surfacer
    # riding condense() takes. Asserted as EXACT EQUALITY, not startswith + a
    # couple of `in` checks -- an adversarial review installed a mutant that
    # appended " [see log]" to every section summary, a real change every
    # consumer would see, and the weaker form still reported ok.
    OPENER = ("[untracked-tooling] 90 deployed personal automation(s) a fresh "
              "install will not reproduce. Run the sweep.")
    ONE = OPENER + "\n" + _rows("t", 20)
    os.environ[sr.STATE_DIR_ENV] = str(pathlib.Path(td) / "sr-one")
    sr.condense("o", ONE)
    one = sr.condense("o", ONE)
    check("single-section digest is EXACTLY the opener + inline age clause",
          one == f"{OPENER}  [unchanged, 2 fire(s) today]")
    check("single-section digest is shorter than the render", len(one) < len(ONE))
    # A bare "[tag]" opener with the count on a LATER line: the digest must
    # still carry a figure (this is the pull-in-the-next-numeric-line rule).
    BARE = "[some-tag]\n\n7 things need a human.\n" + _rows("b", 20)
    os.environ[sr.STATE_DIR_ENV] = str(pathlib.Path(td) / "sr-bare")
    sr.condense("b", BARE)
    bare = sr.condense("b", BARE)
    check("bare-tag opener still yields a digest with the count",
          "[some-tag]" in bare and "7 things" in bare and len(bare) < len(BARE))

    print("condense(): every SHIPPED section-opener shape is recognised")
    # The first version of this fix matched only a BARE lower-case "[tag]" line,
    # so a second section opening with tag + text -- the form this module's own
    # docstring calls canonical, and what the deployed orphan-skills and
    # ship-close-out surfacers render -- was swallowed into the first and its
    # verdict deleted from the digest. Same bug, one shape over. One leg per
    # shape so the CLASS stays closed, not just the instance.
    HEAD_A = "[claimed-work-stranded]\n\nat least 4 branches carry CLAIMED work.\n"
    for name, header in (
        ("bare tag", "[unpushed-commits-surface]"),
        ("tag + text", "[unpushed-commits-surface] 2 item(s) at LOST-WORK risk."),
        ("bold-wrapped", "**[unpushed-commits-surface]**"),
        ("mixed case", "[Unpushed-Commits-Surface]"),
        ("non-ASCII slug", "[trabajo-señalado]"),
    ):
        body = ("\n2 item(s) at real LOST-WORK risk — push/back-up now.\n"
                + _rows("d", 10))
        full = HEAD_A + _rows("c", 10) + "\n\n" + header + "\n" + body
        os.environ[sr.STATE_DIR_ENV] = str(pathlib.Path(td) / f"sr-shape-{name}")
        sr.condense("shape", full)
        d = sr.condense("shape", full)
        check(f"{name}: digest is genuinely shorter", len(d) < len(full))
        check(f"{name}: the second section's verdict survives", "LOST-WORK" in d)

    print("condense(): body lines that only LOOK like headers are not split on")
    # Over-splitting costs a noisy line, never a verdict, so this leans
    # permissive -- but link and reference syntax is common enough in these
    # renders to be worth pinning, and an indented tag is body, not a header.
    for name, line in (
        ("markdown link", "[unpushed-commits-surface](http://x)"),
        ("reference def", "[unpushed-commits-surface]: http://x"),
        ("indented tag", "  [unpushed-commits-surface]"),
    ):
        full = (HEAD_A + _rows("c", 10) + "\n\n" + line
                + "\n2 item(s) at real LOST-WORK risk.\n" + _rows("d", 10))
        os.environ[sr.STATE_DIR_ENV] = str(pathlib.Path(td) / f"sr-neg-{name}")
        sr.condense("neg", full)
        d = sr.condense("neg", full)
        check(f"{name} is not treated as a section header", line not in d)

    print("condense(): the age clause covers EVERY section, not just the last")
    os.environ[sr.STATE_DIR_ENV] = str(pathlib.Path(td) / "sr-age")
    sr.condense("age", MULTI)
    aged = sr.condense("age", MULTI)
    check("multi-section age clause sits on its own trailing line",
          aged.splitlines()[-1].startswith("[unchanged"))
    check("...and each section keeps its own summary line above it",
          len([ln for ln in aged.splitlines() if ln.startswith("[")]) == 4)

    del os.environ[sr.STATE_DIR_ENV]

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("all standing_report controls passed")
