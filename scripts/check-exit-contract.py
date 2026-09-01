#!/usr/bin/env python3
"""check-exit-contract.py - every checker declares whether its findings can fail.

THE BUG CLASS THIS EXISTS FOR
    A checker that PRINTS a finding and then exits 0 is indistinguishable, to
    every automated caller, from a checker that found nothing. The exit code is
    the only channel a cron line, a hook, a CI step or a shell `&&` can read.
    Prose in the output is not a verdict.

    Measured on this repo at 8b9038b, across the 181 non-test CLI entries under
    scripts/. A sample of what a sweep found:

      scripts/drift-check.sh:319       prints `STATUS: OK` UNCONDITIONALLY, then
                                       `DRIFT_COUNT: <n>`, then exit 0 at :330.
                                       The documented STATUS vocabulary at :48
                                       is <OK | SKIPPED_TODAY | ERROR> and has
                                       no value meaning "drift". A fully
                                       drifted install reports STATUS: OK.
      scripts/check-rule-conflicts.py  :659 (--json) exits `1 if conflicts` --
                                       :663 (markdown, the default) exits 0 for
                                       the SAME conflicts. The verdict depended
                                       on which output format you asked for.
      scripts/check-utf8-file-io.py    :421 guarded on `if violations:` while
                                       its own sibling check-utf8-stdout.py:378
                                       guards the identical condition on
                                       `if violations or stale:`. A stale
                                       baseline row printed under `NOTE:` and
                                       returned 0 -- in a file whose own
                                       docstring at :73 says "The baseline is a
                                       BACKLOG, NOT A SET OF PARDONS".
      scripts/vault-backup.sh:505      `python3 "$checker" "$vault" || true`
                                       discards the canonical verdict, while
                                       :440-441 documents the opposite policy
                                       for the sibling subcommand.
      scripts/stale-rule-check.py:207  `return 2 if stale else 0` -- the
                                       `skipped` bucket (1414 entries on a live
                                       vault) never reaches the exit, so a
                                       corpus of unevaluable rules reads as a
                                       clean board.

    None of these are wrong to be quiet. Some are genuinely advisory: a
    heuristic queue for human review, a report generator whose stdout is the
    product, a SessionStart hook that must never block a session. ADR-0004
    already establishes hard-vs-advisory as a deliberate split, and
    ci-parity-fail-open-for-gateless-repos records why an over-strict gate is
    worse than an honest fail-open -- it teaches bypass, and the bypass then
    masks the real failures.

    The defect is not that they exit 0. The defect is that NOTHING SAYS SO, so
    a caller cannot tell a deliberate advisory from a gate that forgot to fail.
    Both emit the same silence. This gate closes that gap the only way prose
    cannot: by requiring the declaration to exist, in the file, mechanically.

THE RULE
    Every tracked, non-test CLI entry under scripts/ carries exactly one
    exit-contract marker in its first EXIT_CONTRACT_SCAN_LINES lines:

        # exit-contract: ENFORCING
        # exit-contract: ADVISORY -- <reason, >= MIN_REASON_CHARS chars>
        # exit-contract: NOT-A-CHECKER -- <reason, >= MIN_REASON_CHARS chars>

    ENFORCING      findings reach a non-zero exit. Checked structurally (below).
    ADVISORY       deliberately never fails on findings. The reason must say
                   WHY -- who consumes it, and what would break if it reddened.
    NOT-A-CHECKER  does work and narrates progress; has no finding verdict.

    ...or the file sits on UNDECLARED_BASELINE, which is CAPPED and may only
    shrink. That is the burn-down channel, not an amnesty list.

WHAT THE ENFORCING CHECK PROVES, PRECISELY
    That a non-zero exit TOKEN appears somewhere in the file's text. Nothing
    more. It is a very low floor, and saying so exactly is the point:

      * It does NOT exclude an argparse/usage path. A file whose only
        `sys.exit(2)` is a bad-flag branch passes.
      * It does NOT prove the token is reachable, or executable at all. A
        `return 1` inside a comment, a docstring or a heredoc passes.
      * It therefore does NOT catch every file that cannot fail. It catches
        the blatant case -- ENFORCING asserted over a file with no non-zero
        anywhere in it -- and that is all.

    The failure message says "carries no non-zero exit token", never "cannot
    fail", because the token is genuinely all it looked at. Reachability is
    what a per-script negative control proves; 21 of the 48 ENFORCING files
    carry a --self-test today, so that backstop is real for some of the
    population and absent for the rest. Treat this check as a declaration
    audit, not as verification.

WHY A MARKER AND NOT INFERENCE
    Deciding "can this finding path exit non-zero" is a dataflow question over
    182 files in two languages. A gate that infers it would be wrong quietly,
    which is the bug class it exists to prevent. Requiring an author to write
    one line moves the claim from a guess into the file, next to the code, where
    review can see it and a diff can change it.

ASCII-only output on purpose - this repo's console-encoding guards
(check-utf8-stdout.py) cover this file too.

Usage:
    check-exit-contract.py                 # the gate
    check-exit-contract.py --self-test     # the negative controls
    check-exit-contract.py --list          # undeclared files, as a worklist (exit 0)
"""
# exit-contract: ENFORCING

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# A marker must sit near the top, where a reader meets it. Deep in a 2000-line
# file it is documentation nobody sees.
EXIT_CONTRACT_SCAN_LINES = 120

# Long enough to force a real sentence. "advisory" alone is not a reason.
MIN_REASON_CHARS = 20

# Anchored at column 0 on purpose. `^[#\s]*` also matched an INDENTED line,
# which meant a file could not document the marker forms without the examples
# counting as declarations -- this file failed its own gate that way, and only
# passed while it was still untracked. A declaration is a top-level comment; an
# indented one is prose, a fixture, or a string literal.
MARKER_RE = re.compile(
    r"^#\s*exit-contract:\s*"
    r"(ENFORCING|ADVISORY|NOT-A-CHECKER)"
    r"(?:\s*--\s*(.*))?$",
    re.MULTILINE,
)

# A non-zero exit token, matched over the file's raw text. Deliberately
# generous: a false FAIL here would train people to route around the gate,
# which costs more than the false PASSes it lets through. It matches inside
# comments and strings too -- see the docstring; this is a declaration audit,
# not verification.
NONZERO_EXIT_RE = re.compile(
    r"(sys\.exit\(\s*[1-9]|"          # sys.exit(1)
    r"raise\s+SystemExit\(\s*[1-9]|"  # raise SystemExit(2)
    r"return\s+[1-9]\b|"              # return 1   (via sys.exit(main()))
    r"^\s*exit\s+[1-9]|"              # shell: exit 1
    r"return\s+rc\b|return\s+code\b|return\s+worst\b|"  # named status vars
    # A status carried in a returned struct, e.g. check-context-load.py's
    # `return {"verdict": ..., "exit": 1, ...}` dispatched through
    # `raise SystemExit(main(...))`. Verified empirically on that file: a
    # non-vault path exits 1. Without this branch the floor check reported a
    # false FAIL on a checker that reds correctly -- and a gate whose first
    # number is a false positive is one people learn to route around.
    r"[\"\']exit[\"\']\s*:\s*[1-9])",
    re.MULTILINE,
)

# --- the ratchet -----------------------------------------------------------
# Files not yet carrying a marker. Intended to only ever shrink.
#
# What the cap ACTUALLY enforces, stated precisely because a ratchet that
# overstates itself is worse than none: appending a name WITHOUT raising
# UNDECLARED_MAX fails. Editing both constants in one commit passes. So the cap
# stops the accidental append and the drive-by amnesty; it does not stop a
# deliberate one, which is left visible in the diff for review to argue with.
# Every SHA-pinned baseline in this repo has that same property. Raise
# UNDECLARED_MAX only when deliberately deferring, and say why in the commit.
#
# Populated 2026-08-29 from the tracked non-test CLI population at 8b9038b,
# minus the files declared in the same change.
UNDECLARED_BASELINE: set[str] = {
    "scripts/ai-brain-auto-update.py",
    "scripts/ai-brain-auto-update.sh",
    "scripts/audit-guard-activation-roots.py",
    "scripts/auto-crm-from-mentions.py",
    "scripts/auto-wikilink.py",
    "scripts/backfill-journal-body-context.py",
    "scripts/bootstrap-restore.sh",
    "scripts/build-journal-index.py",
    "scripts/caveman_lint.py",
    "scripts/check-claude-md-drift.py",
    "scripts/check-context-load.py",
    "scripts/check-phase-python.py",
    "scripts/check-renderer-crashes.py",
    "scripts/check-shipped-version-drift.py",
    "scripts/check-template-purity.py",
    "scripts/check-utf8-stdout.py",
    "scripts/check-vault-backup.py",
    "scripts/check-worktree-on-vault.py",
    "scripts/claude_performance_digest.py",
    "scripts/closed-loop-daemon.py",
    "scripts/closed-loop-week-report.py",
    "scripts/compress-vault-doc.py",
    "scripts/context-audit.py",
    "scripts/crm-collision-check.py",
    "scripts/decision-retrospective.py",
    "scripts/dev-drift-report.py",
    "scripts/dev-hub-refresh.py",
    "scripts/dev-repo-reaper.py",
    "scripts/diagnose.ps1",
    "scripts/disambiguate_first_name.py",
    "scripts/drift-check.ps1",
    "scripts/drift-detection.py",
    "scripts/fix-plugin-hooks.sh",
    "scripts/gh-safe.py",
    "scripts/graph-to-neo4j.py",
    "scripts/graphify_apply_wikilinks.py",
    "scripts/graphify_canonicalize.py",
    "scripts/graphify_chunk.py",
    "scripts/graphify_coverage_audit.py",
    "scripts/graphify_dedupe_by_adjacency.py",
    "scripts/graphify_minimax_preprocess.py",
    "scripts/graphify_preextract_block.py",
    "scripts/graphify_prep.py",
    "scripts/graphify_prune_stale_cache.py",
    "scripts/graphify_stage_select.py",
    "scripts/graphify_wikilink_gaps.py",
    "scripts/ground-truth-wiki-maintain.py",
    "scripts/hallucination-sample-audit.py",
    "scripts/hallucination-watch.py",
    "scripts/hook_runner.py",
    "scripts/insight-fact-check.py",
    "scripts/instinct_lib.py",
    "scripts/journal-metadata-extract.py",
    "scripts/journal-preflight.py",
    "scripts/link-agent-memory.py",
    "scripts/mcp-config-check.py",
    "scripts/mcps/graph-query-server.py",
    "scripts/measure-plugin-load.py",
    "scripts/monthly-baseline.py",
    "scripts/nvidia_compare.py",
    "scripts/panel-trigger-hook.sh",
    "scripts/passive-capture.py",
    "scripts/post-commit-ff-worktrees.sh",
    "scripts/post-update-email-ask.py",
    "scripts/preflight.ps1",
    "scripts/proposed-update-drafter.py",
    "scripts/recover-last-close.py",
    "scripts/recover-orphan-claude-branches.py",
    "scripts/relocate-machinery-sidecar.ps1",
    "scripts/relocate-sweep.py",
    "scripts/relocate-vault.ps1",
    "scripts/repo-bundle.py",
    "scripts/resolver-branch-merge-prompt.py",
    "scripts/resolver-build.py",
    "scripts/resolver-conflict-report.py",
    "scripts/rotate-last-session.py",
    "scripts/rotate-meta-archives.py",
    "scripts/security-snapshot.py",
    "scripts/session-close-fallback.py",
    "scripts/session-close-runner.sh",
    "scripts/skill-usage-report.py",
    "scripts/skill-usage-tracker.sh",
    "scripts/stub_audit.py",
    "scripts/sync-high-rise.py",
    "scripts/sync-skills.py",
    "scripts/sync-skills.sh",
    "scripts/sync-vault-scripts.ps1",
    "scripts/token-usage-report.py",
    "scripts/undo-last-close.py",
    "scripts/update-check.ps1",
    "scripts/vault-backup.ps1",
    "scripts/vault-classify-untyped.py",
    "scripts/vault-hygiene.py",
    "scripts/vault-insight-engine.py",
    "scripts/vault-metadata-extract.py",
    "scripts/vault-nested-repos.py",
    "scripts/vault_maintenance.py",
    "scripts/wikilink_misfire_audit.py",
    "scripts/worktree-archive-prep.py",
    "scripts/write-hook.sh",
}
UNDECLARED_MAX = 100


def _tracked_scripts() -> tuple[list[Path], int]:
    """Every tracked scripts/*.py|sh that is a CLI entry, excluding tests.

    Uses git rather than a filesystem walk so an untracked scratch file in a
    working tree cannot change the gate's verdict.
    """
    out = subprocess.run(
        ["git", "ls-files", "scripts/"],
        cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if out.returncode != 0:
        raise RuntimeError("git ls-files failed: " + (out.stderr or "").strip())

    paths = []
    excluded = 0
    for rel in out.stdout.split("\n"):
        rel = rel.strip()
        if not rel:
            continue
        p = REPO_ROOT / rel
        name = p.name
        # .ps1 included: drift-check.ps1 is the Windows twin of this gate's
        # own headline example and shipped the identical defect. Excluding it
        # by suffix would have let the fixed-on-POSIX / still-broken-on-Windows
        # split stay invisible.
        if p.suffix not in (".py", ".sh", ".ps1"):
            continue
        if name.startswith("_"):            # shared libs, not CLI entries
            excluded += 1
            continue
        if name.startswith(("test-", "test_")):   # suites, not checkers
            excluded += 1
            continue
        if not p.exists():                  # tracked but deleted in the tree
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            # NEVER `continue`. Dropping an unreadable file removes it from the
            # population and lets the gate report success over a checker it
            # could not look at -- the exact bug class this gate exists for,
            # inside the gate. Measured: `chmod 000` on one checker silently
            # took the count from 183 to 182 and still printed OK.
            raise RuntimeError(
                "cannot read tracked checker {}: {}".format(rel, e)) from e
        # A .py is a CLI only if it has an entrypoint; every .sh is runnable.
        if p.suffix == ".py" and "__main__" not in src:
            excluded += 1
            continue
        paths.append(p)
    return sorted(paths), excluded


def read_marker(src: str) -> tuple[str | None, str, int]:
    """Return (kind, reason, count) for the markers in the file's header."""
    head = "\n".join(src.split("\n")[:EXIT_CONTRACT_SCAN_LINES])
    found = MARKER_RE.findall(head)
    if not found:
        return None, "", 0
    kind, reason = found[0]
    return kind, (reason or "").strip(), len(found)


def has_nonzero_exit(src: str) -> bool:
    return bool(NONZERO_EXIT_RE.search(src))


def check_file(path: Path, src: str) -> list[str]:
    """Problems with ONE file's exit-contract declaration."""
    rel = path.relative_to(REPO_ROOT).as_posix()
    kind, reason, count = read_marker(src)
    problems = []

    if kind is None:
        if rel in UNDECLARED_BASELINE:
            return []
        problems.append(
            "{}: no exit-contract marker. Add one of:\n"
            "      # exit-contract: ENFORCING\n"
            "      # exit-contract: ADVISORY -- <why it never fails>\n"
            "      # exit-contract: NOT-A-CHECKER -- <what it does instead>"
            .format(rel))
        return problems

    if rel in UNDECLARED_BASELINE:
        problems.append(
            "{}: now declared ({}), but still on UNDECLARED_BASELINE. "
            "Drop the row -- a paid-off baseline entry that stays is how a "
            "ratchet rots into an allowlist.".format(rel, kind))

    if count > 1:
        problems.append(
            "{}: {} exit-contract markers in the header; exactly one is a "
            "declaration, more than one is an argument.".format(rel, count))

    if kind == "ENFORCING":
        if reason:
            pass  # a rationale on ENFORCING is welcome, never required
        if not has_nonzero_exit(src):
            problems.append(
                "{}: declared ENFORCING but carries no non-zero exit token. "
                "Either it cannot fail (declare ADVISORY, with the reason), or "
                "the finding path needs a non-zero exit.".format(rel))
    else:
        if len(reason) < MIN_REASON_CHARS:
            problems.append(
                "{}: declared {} with a {}-char reason; at least {} required. "
                "Say who consumes the exit code and what would break if this "
                "went red.".format(rel, kind, len(reason), MIN_REASON_CHARS))

    return problems


def check_all(paths: list[Path]) -> tuple[list[str], int, dict[str, int]]:
    problems: list[str] = []
    tally = {"ENFORCING": 0, "ADVISORY": 0, "NOT-A-CHECKER": 0, "undeclared": 0}

    # THE RATCHET, enforced. Without this the baseline is an amnesty list and
    # every name added to it buys a green. Checked before the per-file pass so
    # an over-cap baseline fails even on an otherwise clean tree.
    if len(UNDECLARED_BASELINE) > UNDECLARED_MAX:
        problems.append(
            "UNDECLARED_BASELINE has {} entries but UNDECLARED_MAX is {}. The "
            "baseline may only SHRINK; declare the file instead of listing it."
            .format(len(UNDECLARED_BASELINE), UNDECLARED_MAX))

    # Keyed on the repo-relative PATH, not the basename. On basenames, a brand
    # new silent checker added at scripts/sub/<name>.py inherited the pardon of
    # an unrelated scripts/<name>.py and the gate printed OK -- while its own
    # summary line said "94 undeclared (93/93 on the baseline)", arithmetic that
    # contradicted itself and that nothing acted on.
    names = {p.relative_to(REPO_ROOT).as_posix() for p in paths}
    for stale in sorted(UNDECLARED_BASELINE - names):
        problems.append(
            "UNDECLARED_BASELINE names {} which is not a tracked non-test CLI "
            "under scripts/. Drop the stale row.".format(stale))

    for p in paths:
        src = p.read_text(encoding="utf-8", errors="replace")
        kind, _reason, _count = read_marker(src)
        tally[kind if kind else "undeclared"] += 1
        problems.extend(check_file(p, src))

    return problems, len(paths), tally


# --- self-test: the negative controls --------------------------------------
# Each case constructs the defect and asserts this gate goes RED on it. A case
# that only ever passes proves the gate ran, not that it bites.

_ENFORCING = "# exit-contract: ENFORCING\nimport sys\nif x:\n    sys.exit(1)\n"
_ADVISORY = ("# exit-contract: ADVISORY -- stdout is the product; a cron "
             "caller reads the report, never the status\n")
_NOTCHECK = ("# exit-contract: NOT-A-CHECKER -- copies skill files into the "
             "install and narrates what it copied\n")


def self_test() -> int:
    import tempfile

    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp())

    def case(name: str, body: str, expect: str | None, filename: str = "x.py"):
        """expect=None means "must not bite"; otherwise the substring that the
        firing problem MUST contain.

        Binding each control to its SITE, not merely to "something fired", is
        load-bearing. Measured while writing this file: a control asserting only
        truthiness stayed green with the missing-marker branch disabled, because
        an undeclared file also trips the reason-length branch further down. The
        control passed for the wrong reason, which reads as more rigorous than a
        control that does not run at all. Same trap check-hook-negative-control.py
        documents from PR #610.
        """
        p = tmp / filename
        p.write_text(body, encoding="utf-8")
        # check_file wants a path under REPO_ROOT for its relative name; the
        # name is cosmetic, so borrow the real root and pass the text directly.
        got = check_file(REPO_ROOT / "scripts" / filename, body)
        if expect is None:
            if got:
                failures.append("{}: expected pass, got {}".format(name, got[:1]))
            return
        if not got:
            failures.append("{}: expected BITE, got pass".format(name))
        elif not any(expect in g for g in got):
            failures.append(
                "{}: bit for the WRONG reason -- expected a problem containing "
                "{!r}, got {!r}".format(name, expect, got))

    # --- the population this gate exists for -------------------------------
    case("no marker at all -> FAIL",
         "import sys\nprint('hi')\nsys.exit(1)\n", "no exit-contract marker")
    case("ENFORCING with no non-zero exit -> FAIL",
         "# exit-contract: ENFORCING\nprint('clean')\n",
         "no non-zero exit token")
    case("ADVISORY with no reason -> FAIL",
         "# exit-contract: ADVISORY\nprint('hi')\n", "0-char reason")
    case("ADVISORY with a too-short reason -> FAIL",
         "# exit-contract: ADVISORY -- advisory\nprint('hi')\n", "char reason")
    case("NOT-A-CHECKER with no reason -> FAIL",
         "# exit-contract: NOT-A-CHECKER\nprint('hi')\n", "0-char reason")
    case("two markers -> FAIL",
         _ENFORCING + _ADVISORY, "exit-contract markers in the header")
    # An INDENTED marker is documentation, not a declaration. Without the
    # column-0 anchor this file's own docstring -- which shows all three forms
    # -- read as three declarations, and the gate failed itself the moment it
    # became tracked. It passed review only because an untracked file is
    # invisible to `git ls-files`.
    case("indented marker in a docstring is NOT a declaration -> FAIL",
         '"""doc\n        # exit-contract: ADVISORY -- a documented example '
         'form, not a real declaration\n"""\nprint(1)\n',
         "no exit-contract marker")
    # A marker buried past the scan window is not a declaration a reader meets.
    # Fixture pinned to a LITERAL 200, not to EXIT_CONTRACT_SCAN_LINES + 5.
    # Built from the constant it tests, this case asserted only the tautology
    # "N+5 > N": raising the window to 100000 left the suite green, so it
    # proved the arithmetic and never the property. A control must not be
    # derived from the value it is controlling.
    case("marker below the scan window -> FAIL",
         "\n" * 200 + _ADVISORY,
         "no exit-contract marker")

    # --- the clean forms, which must NOT bite ------------------------------
    case("ENFORCING with sys.exit(1) -> pass", _ENFORCING, None)
    case("ENFORCING with `return 1` -> pass",
         "# exit-contract: ENFORCING\ndef main():\n    return 1\n", None)
    case("ADVISORY with a real reason -> pass", _ADVISORY, None)
    case("NOT-A-CHECKER with a real reason -> pass", _NOTCHECK, None)
    case("shell `exit 1` satisfies ENFORCING -> pass",
         "# exit-contract: ENFORCING\nif [ -n \"$x\" ]; then\n  exit 1\nfi\n",
         None, "x.sh")

    # --- the ratchet -------------------------------------------------------
    # Measured on the parent commit: without the cap check, appending a name to
    # UNDECLARED_BASELINE turned any red file green, which is exactly the
    # amnesty-list shape this gate is meant to refuse.
    global UNDECLARED_BASELINE, UNDECLARED_MAX
    saved_base, saved_max = UNDECLARED_BASELINE, UNDECLARED_MAX
    try:
        UNDECLARED_BASELINE, UNDECLARED_MAX = {"scripts/ghost-checker.py"}, 0
        probs, _n, _t = check_all([])
        if not any("UNDECLARED_MAX" in p for p in probs):
            failures.append("baseline OVER cap -> expected BITE, got pass")

        UNDECLARED_BASELINE, UNDECLARED_MAX = {"scripts/ghost-checker.py"}, 1
        probs, _n, _t = check_all([])
        if not any("stale row" in p for p in probs):
            failures.append("stale baseline row (file gone) -> expected BITE")

        # A file both declared AND baselined must fail: the row is paid off.
        UNDECLARED_BASELINE, UNDECLARED_MAX = {"scripts/self-test-declared.py"}, 1
        got = check_file(REPO_ROOT / "scripts" / "self-test-declared.py",
                         _ADVISORY)
        if not any("still on UNDECLARED_BASELINE" in g for g in got):
            failures.append("declared-but-still-baselined -> expected BITE")
    finally:
        UNDECLARED_BASELINE, UNDECLARED_MAX = saved_base, saved_max

    # --- the population selector, which had NO controls at all -------------
    # Both defects below were found by adversarial review, not by this suite,
    # and both lived in _tracked_scripts()/check_file rather than in the marker
    # logic the other cases exercise.

    # A baseline keyed on BASENAME pardoned a brand-new file at a NEW path: a
    # silent checker added at scripts/sub/gh-safe.py inherited the row for
    # scripts/gh-safe.py, and the summary printed "94 undeclared (93/93)" --
    # arithmetic that contradicted itself while the gate said OK.
    saved_base2, saved_max2 = UNDECLARED_BASELINE, UNDECLARED_MAX
    try:
        UNDECLARED_BASELINE, UNDECLARED_MAX = {"scripts/gh-safe.py"}, 1
        got = check_file(REPO_ROOT / "scripts" / "sub" / "gh-safe.py",
                         "import sys\nprint('finding')\n")
        if not any("no exit-contract marker" in g for g in got):
            failures.append(
                "a new file at a NEW path inherited the basename pardon of an "
                "unrelated file: expected BITE, got {}".format(got))
        # ...while the genuinely baselined PATH still passes.
        if check_file(REPO_ROOT / "scripts" / "gh-safe.py",
                      "import sys\nprint('finding')\n"):
            failures.append(
                "the baselined path itself should pass; the case above may be "
                "biting for the wrong reason")
    finally:
        UNDECLARED_BASELINE, UNDECLARED_MAX = saved_base2, saved_max2

    # An unreadable tracked checker must FAIL LOUD, never vanish from the
    # population. Measured before the fix: `chmod 000` on one checker took the
    # count from 183 to 182 and the gate still printed OK.
    import stat as _stat
    probe = tmp / "unreadable.py"
    probe.write_text("# exit-contract: ENFORCING\nimport sys\nsys.exit(1)\n",
                     encoding="utf-8")
    try:
        probe.chmod(0o000)
        try:
            probe.read_text(encoding="utf-8")
            unreadable_is_testable = False   # running as root, or a permissive FS
        except OSError:
            unreadable_is_testable = True
    finally:
        probe.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
    if not unreadable_is_testable:
        # Say so rather than reporting a pass: a control that could not run has
        # proven nothing, which is the whole subject of this file.
        print("  NOTE: unreadable-file control SKIPPED (this process can read a "
              "0o000 file, so the condition cannot be constructed here). The "
              "fail-loud path is unexercised on this box.")

    # --- a positive control on the SEARCH itself ---------------------------
    # If the marker regex silently stopped matching, every case above would
    # still "pass" its BITE expectation for the wrong reason. Prove the parser
    # can find a marker it should find.
    kind, reason, count = read_marker(_ADVISORY)
    if kind != "ADVISORY" or count != 1 or len(reason) < MIN_REASON_CHARS:
        failures.append(
            "positive control: the parser failed to read a valid ADVISORY "
            "marker (kind={!r} count={} reason={!r})".format(
                kind, count, reason))

    if failures:
        print("SELF-TEST FAIL:")
        for f in failures:
            print("  - {}".format(f))
        return 1
    print("OK - self-test: 8 defect shapes bite, 5 clean forms pass, the "
          "baseline cap / stale row / paid-off row all bite, and the marker "
          "parser is positively controlled.")
    return 0


def main(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("--")}

    if "--self-test" in flags:
        return self_test()

    try:
        paths, excluded = _tracked_scripts()
    except (RuntimeError, OSError) as e:
        # Fail LOUD, never clean: a gate that cannot read its inputs must not
        # report success.
        print("UNEVALUATED: could not enumerate tracked scripts ({}). "
              "Nothing was checked -- this is not a pass.".format(e),
              file=sys.stderr)
        return 2

    if not paths:
        print("UNEVALUATED: zero tracked non-test CLI scripts found under "
              "scripts/. An empty enumeration is not a clean tree.",
              file=sys.stderr)
        return 2

    if "--list" in flags:
        undeclared = [p.relative_to(REPO_ROOT).as_posix() for p in paths
                      if read_marker(p.read_text(encoding="utf-8",
                                                 errors="replace"))[0] is None]
        for rel in undeclared:
            print(rel)
        print("\n{} undeclared file(s).".format(len(undeclared)))
        return 0

    problems, n, tally = check_all(paths)

    # A check's NAME is a claim about its SCOPE, so the scope is stated rather
    # than implied. Without this line the summary read as though it covered
    # everything under scripts/, while filters silently dropped ~120 tracked
    # files -- including every .ps1, among them drift-check.ps1, the Windows
    # twin of this gate's own headline example.
    print("exit contract: {} tracked non-test CLI script(s) -- {} enforcing, "
          "{} advisory, {} not-a-checker, {} undeclared "
          "({}/{} on the shrinking baseline).".format(
              n, tally["ENFORCING"], tally["ADVISORY"], tally["NOT-A-CHECKER"],
              tally["undeclared"], len(UNDECLARED_BASELINE), UNDECLARED_MAX))
    print("  scope: tracked scripts/*.{{py,sh,ps1}}; EXCLUDED are _*.py shared "
          "libs, test-*/test_* suites, and .py files with no __main__ "
          "entrypoint ({} tracked file(s) under scripts/ excluded by those "
          "filters).".format(excluded))

    if problems:
        print("\nexit-contract FAILED -- {} problem(s):\n".format(len(problems)))
        for p in problems:
            print("  - {}".format(p))
        print("\nWhy this is a gate: the exit code is the only channel a cron "
              "line, hook, or CI step can read. A checker that prints a "
              "finding and exits 0 is, to every caller, a checker that found "
              "nothing.")
        return 1

    print("exit contract OK -- every checker declares whether its findings "
          "can fail.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
