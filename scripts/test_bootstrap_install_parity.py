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
        # NOTE: the consumer hook is named WITHOUT its literal filename on
        # purpose -- do not "helpfully" restore it. This file's name contains
        # "test", so check-hook-negative-control.py treats it as a test surface
        # and substring-matches hook names against its whole text. Spelling the
        # hook's path here makes that guard believe the hook HAS a test surface,
        # goes stale on its NO_TEST_BASELINE row, and fails CI -- while quietly
        # asserting coverage this file does not provide. Mentioned is not
        # tested, the same distinction this guard draws between a path
        # mentioned in a comment and a path actually written. Tracked as
        # MYC-4045.
        "MYC-4021 - REAL GAP. bootstrap.sh records every install step that did "
        "not finish; the first-week check-in hook READS this file and quietly "
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
    """Destination tokens this installer WRITES.

    Comment lines are dropped first. Both installers discuss paths in prose --
    bootstrap.sh's header explains .bootstrap.log three lines above the
    assignment that creates it. Counting a comment as coverage is the
    dangerous direction: a `# we deliberately skip .bootstrap.log on Windows`
    note in bootstrap.ps1 would make this guard report the gap as CLOSED while
    nothing writes the file. Mentioning a path is not writing it.
    """
    if not path.is_file():
        print(f"FAIL: {path} not found - this guard cannot run", file=sys.stderr)
        raise SystemExit(1)
    lines = [
        ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    return {m for m in pattern.findall("\n".join(lines))} - IGNORE

# -----------------------------------------------------------------------------
# SECOND CHECK: skill-list coverage.
#
# The destination check above deliberately IGNOREs "skills" (see IGNORE) --
# both installers write under .claude/skills, so the destination token matches
# on both sides and parity looks perfect. That is true and useless: WHICH
# skills get written is decided by four hardcoded name lists, and a skill
# missing from a list is invisible to a check that only compares destinations.
#
# Measured 2026-08-24 (MYC-4139): security-snapshot, vault-system and
# skillify-meta-loop shipped as complete skills under skills/ while appearing
# in none of the four lists, so a fresh clone installed them on no platform.
# optimize-brain had been added to bootstrap.sh only, leaving the Windows
# installer's own last instruction (/optimize-brain) broken. Both classes were
# invisible to every guard in this repo, including the one in this file.
#
# A skill dir is the SOURCE OF TRUTH. Adding skills/<name>/SKILL.md and
# forgetting the lists must fail RED here rather than ship an unreachable skill.
# -----------------------------------------------------------------------------

SKILLS_DIR = REPO / "skills"

# Names allowed in a VERIFY list without being in an INSTALL list, because
# something other than the sub-skill sync puts them on disk. Rationale-tagged,
# and ratcheted in both directions exactly like KNOWN_GAPS: a row that stops
# being needed fails as STALE, so this cannot become a parking lot.
VERIFY_ONLY: dict[str, str] = {
    "humanizer": (
        "installed from its own public fork repo, not from skills/ -- it has "
        "no skills/humanizer dir here, so the install loop must not name it."
    ),
    "ai-brain-starter": (
        "the starter itself, laid down at $SKILL_DIR before the sub-skill loop "
        "runs; verifying it proves the parent install landed."
    ),
}

# `for sub in a b c; do` (bootstrap.sh) and
# `foreach ($sub in @("a", "b")) {` (bootstrap.ps1).
SKILL_LIST_RE_SH = re.compile(r"^for sub in (.+?); do\s*$", re.MULTILINE)
SKILL_LIST_RE_PS = re.compile(r"^foreach \(\$sub in @\((.+?)\)\)\s*\{", re.MULTILINE)


def shipped_skills() -> set[str]:
    """Every skills/<name>/ that carries a SKILL.md.

    SKILL.md presence is the test, not directory presence: skills/_shared/ is
    support material, not an installable skill, and a bare dir left by a failed
    checkout must not be demanded of the installers.
    """
    if not SKILLS_DIR.is_dir():
        print(f"FAIL: {SKILLS_DIR} not found - this guard cannot run", file=sys.stderr)
        raise SystemExit(1)
    return {
        d.name for d in SKILLS_DIR.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    }


def skill_lists(text: str, kind: str) -> list[list[str]]:
    """The skill-name lists in one installer, in file order, WITH duplicates.

    Duplicates are preserved so the caller can report them; every other
    comparison is done on sets.
    """
    if kind == "sh":
        return [m.split() for m in SKILL_LIST_RE_SH.findall(text)]
    return [re.findall(r'"([^"]+)"', m) for m in SKILL_LIST_RE_PS.findall(text)]


def check_skill_lists(sh_text: str, ps_text: str, shipped: set[str]) -> list[str]:
    """Pure over its inputs so the self-test can drive it with synthetic text."""
    failures: list[str] = []
    sh_lists = skill_lists(sh_text, "sh")
    ps_lists = skill_lists(ps_text, "ps1")

    # Positive control. Without this an extraction that quietly stops matching
    # reports a perfect, entirely unmeasured pass -- the exact failure mode this
    # file's other check guards against.
    if len(sh_lists) != 2 or len(ps_lists) != 2:
        failures.append(
            f"  EXTRACTION BROKEN: expected 2 skill lists per installer, found "
            f"bootstrap.sh={len(sh_lists)}, bootstrap.ps1={len(ps_lists)}.\n"
            f"      The loop syntax changed and this guard is no longer reading the\n"
            f"      lists. Fix the regex - do not assume parity is clean."
        )
        return failures
    if any(len(lst) < 20 for lst in sh_lists + ps_lists):
        failures.append(
            "  EXTRACTION BROKEN: a skill list parsed to fewer than 20 names.\n"
            "      Both installers carry 30+; a short list means the regex is\n"
            "      truncating, not that a list shrank."
        )
        return failures

    labelled = [
        ("bootstrap.sh", "install", sh_lists[0]),
        ("bootstrap.sh", "verify", sh_lists[1]),
        ("bootstrap.ps1", "install", ps_lists[0]),
        ("bootstrap.ps1", "verify", ps_lists[1]),
    ]

    for fname, role, lst in labelled:
        # (a) THE BUG THIS EXISTS FOR: a shipped skill named in no list.
        for name in sorted(shipped - set(lst)):
            failures.append(
                f"  UNWIRED SKILL: skills/{name}/ ships but is missing from the\n"
                f"      {role} list in {fname}. The install loop copies only what its\n"
                f"      list names, so this skill reaches no user on that platform.\n"
                f"      Add it to the list."
            )
        # (b) a name with no skill dir: a typo, or a skill deleted without
        #     cleaning the lists. Allowed only with a VERIFY_ONLY reason.
        for name in sorted(set(lst) - shipped):
            if name in VERIFY_ONLY and role == "verify":
                continue
            why = (
                "it is VERIFY_ONLY but appears in an INSTALL list, where the loop "
                "would look for a skills/ dir that does not exist"
                if name in VERIFY_ONLY
                else "there is no skills/<name>/SKILL.md"
            )
            failures.append(
                f"  PHANTOM SKILL: {fname} {role} list names '{name}' but {why}."
            )
        # (c) hand-edited lists accumulate duplicates; harmless but a tell.
        dupes = sorted({n for n in lst if lst.count(n) > 1})
        if dupes:
            failures.append(
                f"  DUPLICATE in {fname} {role} list: {', '.join(dupes)}."
            )

    # (d) stale amnesty, same ratchet as KNOWN_GAPS.
    every_listed = {n for _, _, lst in labelled for n in lst}
    for name in sorted(set(VERIFY_ONLY) - every_listed):
        failures.append(
            f"  STALE AMNESTY: '{name}' is in VERIFY_ONLY but appears in no list.\n"
            f"      DELETE the row - a standing exemption nothing uses is how the\n"
            f"      next real gap gets waved through."
        )
    return failures


def _self_test() -> None:
    """Negative controls. A guard that has never failed is not known to work.

    These run on EVERY invocation, not behind a flag: a dead guard and a quiet
    one print the same thing, and this one is cheap enough that there is no
    reason to let that ambiguity exist.

    The fixtures carry EVERY VERIFY_ONLY name in their verify lists, because
    the stale-amnesty ratchet is live here too -- a row missing from the
    fixture fails these controls exactly as it would fail the real tree. That
    is intentional: adding a VERIFY_ONLY row you never use breaks the build.
    """
    core = [f"s{i}" for i in range(30)] + ["alpha"]
    verify_extra = sorted(VERIFY_ONLY)
    shipped = set(core)

    good_sh = (
        "for sub in " + " ".join(core) + "; do\n"
        "for sub in " + " ".join(core + verify_extra) + "; do\n"
    )
    good_ps = (
        "foreach ($sub in @(" + ", ".join(f'"{n}"' for n in core) + ")) {\n"
        "foreach ($sub in @(" + ", ".join(f'"{n}"' for n in core + verify_extra) + ")) {\n"
    )

    def fails(sh=good_sh, ps=good_ps, shp=shipped):
        return check_skill_lists(sh, ps, shp)

    assert not fails(), f"control: clean input must PASS, got {fails()}"

    # (1) the bug this guard exists for: a shipped skill named in no list.
    assert any("UNWIRED SKILL" in f for f in fails(shp=shipped | {"newskill"})), \
        "control: an unwired shipped skill must FAIL"

    # (2) the SECOND bug from the same incident: wired on one platform only.
    #     This is the one a destination-token guard cannot see at all.
    one_platform = good_ps.replace('", "alpha")) {', '")) {', 1)
    assert any("UNWIRED SKILL" in f for f in fails(ps=one_platform)), \
        "control: a skill missing from the Windows install list must FAIL"

    # (3) a vacuous pass must be impossible.
    assert any("EXTRACTION BROKEN" in f for f in fails(sh="", ps="")), \
        "control: unparseable installers must FAIL, never silently pass"
    short = "for sub in a b c; do\nfor sub in a b c; do\n"
    assert any("EXTRACTION BROKEN" in f for f in fails(sh=short)), \
        "control: a truncated list must FAIL rather than report clean parity"

    # (4) a listed name no skills/ dir backs (typo, or a deleted skill).
    assert any("PHANTOM SKILL" in f for f in fails(shp=shipped - {"alpha"})), \
        "control: a listed name with no skills/ dir must FAIL"

    # (5) a VERIFY_ONLY name in an INSTALL list is still wrong -- the loop
    #     would look for a skills/ dir that does not exist.
    bad_install = good_sh.replace(
        "for sub in " + " ".join(core) + "; do",
        "for sub in " + " ".join(core + verify_extra[:1]) + "; do", 1)
    assert any("PHANTOM SKILL" in f for f in fails(sh=bad_install)), \
        "control: a VERIFY_ONLY name in an install list must FAIL"

    # (6) duplicates from hand-editing.
    dup = good_sh.replace("for sub in " + " ".join(core) + "; do",
                          "for sub in " + " ".join(core + ["alpha"]) + "; do", 1)
    assert any("DUPLICATE" in f for f in fails(sh=dup)), \
        "control: a duplicated name must FAIL"

    # (7) the ratchet: an exemption nothing uses must be deleted, not kept.
    no_extra_sh = "for sub in " + " ".join(core) + "; do\n" * 1 + \
                  "for sub in " + " ".join(core) + "; do\n"
    no_extra_ps = (
        "foreach ($sub in @(" + ", ".join(f'"{n}"' for n in core) + ")) {\n"
        "foreach ($sub in @(" + ", ".join(f'"{n}"' for n in core) + ")) {\n"
    )
    assert any("STALE AMNESTY" in f for f in fails(sh=no_extra_sh, ps=no_extra_ps)), \
        "control: an unused VERIFY_ONLY row must FAIL as stale"


def main() -> int:
    _self_test()
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

    failures += check_skill_lists(
        SH.read_text(encoding="utf-8", errors="replace"),
        PS1.read_text(encoding="utf-8", errors="replace"),
        shipped_skills(),
    )

    if failures:
        print("bootstrap install-parity guard FAILED:\n", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1

    print(
        f"OK - bootstrap install parity: {len(sh_dests)} destination(s) in "
        f"bootstrap.sh, {len(gaps)} known gap(s) still open "
        f"({', '.join(sorted(gaps)) or 'none'}), 0 unlisted and 0 stale; "
        f"skill lists: {len(shipped_skills())} shipped skill(s) present in all "
        f"4 list(s), negative controls passed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
