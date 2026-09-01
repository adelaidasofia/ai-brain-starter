#!/usr/bin/env python3
"""check-defer-targets.py - a named deferral target must actually exist.

SCOPE (the check's NAME is a claim about its SCOPE - state it, do not let the
code silently narrow it): scans tracked files under hooks/ and scripts/ ONLY.
A deferral naming a basename anywhere else (docs/, tests/, phases/) is out of
scope for this checker.

THE BUG CLASS THIS EXISTS FOR
    A hook or script routinely defers a responsibility to a named sibling --
    "MEMORY.md remediation is owned by X", `defer_to="X"` -- so it can measure
    or half-handle something without duplicating a warning some OTHER file
    already owns. That is a legitimate pattern (context-budget-measure.py does
    it on purpose, to avoid double-nagging on MEMORY.md). But the deferral is
    only true while X actually SHIPS in this repo. Caught live:
    context-budget-measure.py deferred MEMORY.md's cliff warning to
    check-memory-md-cap.py -- a file that has 0 hits in `git log --all` and 0
    files on disk in this repo, and exists only in a private, remote-less,
    machine-local repo. Every install of this public repo read "handled
    elsewhere" and got nothing, silently, forever.

    Bug class: DEFERRAL-TO-AN-UNSHIPPED-OWNER. A dangling deferral is WORSE
    than a dormant guard: a dormant guard is absent and looks absent; a
    dangling deferral is absent and looks COVERED.

THE RULE
    Two ways a file can name a deferral target:
      (a) STRUCTURAL - a `defer_to="<basename>"` / `defer_to='<basename>'`
          keyword-argument string literal.
      (b) PROSE - "owned by <basename>", "handled by <basename>",
          "delegates to <basename>", "deferred to <basename>", where
          <basename> matches [A-Za-z0-9_.-]+\\.(py|sh).

    A target RESOLVES if a tracked file ANYWHERE in the repo (not just
    hooks/scripts/) has that basename -- built from `git ls-files`, so a
    renamed or relocated owner still resolves. No git available -> fall back
    to walking the tree from the repo root.

    This checker's OWN path is excluded from the scan: its docstring and
    self-test necessarily contain example deferral strings, and scanning
    itself would be a guaranteed false positive on every run.

KNOWN LIMITS (this gate is a FLOOR, not a ceiling -- state them so the next
reader does not mistake a green for proof of absence):
  - Detection is LINE-BY-LINE. A deferral whose verb and filename wrap onto
    different lines is NOT seen.
  - Only string LITERALS are seen. `defer_to=SOME_CONST` is not.
  - A BARE target resolves by basename anywhere in the repo, so a deferral to
    "x.py" resolves against an unrelated tests/fixtures/x.py. A PATH-qualified
    target is matched against tracked paths exactly, so it does not.
  - Documentation that quotes a real-looking deferral string will trip this
    check. Write examples as `<owner>.py` -- the angle brackets fall outside
    the basename class, so they cannot parse as a real target.

Three states, distinguished by exit code (a failed read is not an empty
answer): 0 = every named target resolves; 1 = a dangling deferral was found,
listing each `path:line -> missing target`; 2 = the scan was INCOMPLETE
because a file could not be read, so no clean verdict is claimable.

Usage:
  check-defer-targets.py               # scan tracked hooks/ + scripts/ files
  check-defer-targets.py --self-test   # positive + negative control
"""
# exit-contract: ENFORCING

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

REPO = Path(__file__).resolve().parent.parent
SELF_PATH = Path(__file__).resolve()

# This walker rglobs a tree and reads what it finds, so it MUST go through the
# shared cloud-safe reader rather than Path.read_text: a vault under Google
# Drive / iCloud / OneDrive can hand back an offline placeholder, a FIFO, or a
# mount that stalls forever. Enforced by tests/integration/test_cloud_safe_file_walkers.sh.
sys.path.insert(0, str(REPO / "hooks"))
from _lib.safe_read import safe_read_text  # noqa: E402

READ_TIMEOUT_S = 5.0
READ_MAX_BYTES = 4_000_000

SCAN_DIRS = ("hooks", "scripts")

# Extensions actually shipped under hooks/ + scripts/ in this repo: py (335),
# sh (80), ps1 (11). A deferral to a .ps1 owner used to be invisible in BOTH
# directions -- never flagged dangling, never confirmed resolved.
_EXT = r"(?:py|sh|ps1)"
# A target may be written bare ("session-start-context.py") or path-qualified
# ("hooks/session-start-context.py"). The path-qualified form is the natural way
# to write it -- and used to match NOTHING, so a deferral to a path that did not
# exist produced zero hits and the run reported "all resolve".
_SEG = r"[A-Za-z0-9_.-]+"
TARGET = r"(?:" + _SEG + r"/)*" + _SEG + r"\." + _EXT
STRUCTURAL_RE = re.compile(r"""defer_to\s*=\s*(['"])(""" + TARGET + r""")\1""")
PROSE_RE = re.compile(
    r"\b(?:owned by|handled by|delegates to|deferred to)\s+(" + TARGET + r")\b"
)


def tracked_files() -> Optional[List[str]]:
    """All tracked repo paths (relative, forward-slash) via `git ls-files`, or
    None when git itself is unavailable (not: repo empty, which is a real
    empty list and must NOT fall back)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return [p for p in result.stdout.split("\0") if p]


def walk_files() -> List[str]:
    """Fallback file listing when git is unavailable: walk the tree, skip .git."""
    out: List[str] = []
    for p in REPO.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(REPO)
        if ".git" in rel.parts:
            continue
        out.append(rel.as_posix())
    return out


def resolvable_basenames(paths: List[str]) -> Set[str]:
    """Both spellings a deferral may use: every tracked path verbatim, and every
    tracked basename. A path-qualified target ("hooks/x.py") must match a real
    PATH -- matching it on basename alone would let a wrong directory pass."""
    out: Set[str] = set()
    for p in paths:
        out.add(p)
        out.add(Path(p).name)
    return out


def target_resolves(target: str, known: Set[str]) -> bool:
    """Path-qualified -> must match a tracked path exactly (a wrong directory is
    a real defect). Bare basename -> must match some tracked basename."""
    if "/" in target:
        return target in known
    return target in known


def scan_targets(paths: List[str]) -> List[str]:
    """Tracked paths under hooks/ or scripts/ (any depth), self excluded."""
    out = []
    for p in paths:
        parts = Path(p).parts
        if not parts or parts[0] not in SCAN_DIRS:
            continue
        if (REPO / p).resolve() == SELF_PATH:
            continue
        out.append(p)
    return out


def find_deferrals(text: str) -> List[Tuple[int, str]]:
    """Pure detection: (1-indexed line, target basename) for every deferral
    reference in `text` -- structural and prose combined, in line order. No
    I/O here; the self-test drives this directly against synthetic strings so
    no real repo file is ever touched."""
    hits: List[Tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in STRUCTURAL_RE.finditer(line):
            hits.append((lineno, m.group(2)))
        for m in PROSE_RE.finditer(line):
            hits.append((lineno, m.group(1)))
    return hits


def dangling(hits_by_path: Dict[str, List[Tuple[int, str]]],
             known_basenames: Set[str]) -> List[str]:
    """Pure: given {path: [(line, target), ...]} and the resolvable basename
    set, return one 'path:line -> missing target' string per dangling hit, in
    stable (path, line) order. The one predicate the whole checker rests on:
    membership in `known_basenames`."""
    out = []
    for path in sorted(hits_by_path):
        for lineno, target in sorted(hits_by_path[path]):
            if not target_resolves(target, known_basenames):
                out.append(f"{path}:{lineno} -> {target}")
    return out


def scan_repo() -> Tuple[List[str], Dict[str, List[Tuple[int, str]]], Set[str], List[str]]:
    """I/O shell: real discovery + real file reads, feeding the pure functions
    above. Returns (scanned paths, hits by path, resolvable basenames, skipped)."""
    all_paths = tracked_files()
    if all_paths is None:
        all_paths = walk_files()
    known = resolvable_basenames(all_paths)

    scanned = scan_targets(all_paths)
    hits_by_path: Dict[str, List[Tuple[int, str]]] = {}
    skipped: List[str] = []
    for rel in scanned:
        result = safe_read_text(
            REPO / rel, timeout=READ_TIMEOUT_S, max_bytes=READ_MAX_BYTES,
            encoding="utf-8", errors="replace",
        )
        if not result.ok:
            # A file we could not read is a file we did not scan. Silence here
            # would print "all resolve" over an unread file that may hold a
            # dangling deferral -- a false clean. Surface it and fail.
            detail = (" (" + result.detail + ")") if result.detail else ""
            skipped.append("%s [%s]%s" % (rel, result.status, detail))
            continue
        hits = find_deferrals(result.text or "")
        if hits:
            hits_by_path[rel] = hits
    return scanned, hits_by_path, known, skipped


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()

    scanned, hits_by_path, known, skipped = scan_repo()
    problems = dangling(hits_by_path, known)

    if skipped:
        # INCOMPLETE is not CLEAN. Exit 2 so a partial scan can never be read
        # as a pass (a scanner states how much of its input it actually read).
        # Print any dangling targets found in the files we DID read, so an
        # unreadable file never costs the operator a second round-trip to
        # discover a real finding that was already in hand.
        print(f"::error::incomplete scan - {len(skipped)} file(s) could not be "
              f"read, so this run cannot claim every deferral resolves:")
        for s in skipped:
            print(f"  {s}")
        if problems:
            print(f"::error::also, {len(problems)} dangling defer target(s) in "
                  f"the files that WERE read:")
            for pr in problems:
                print(f"  {pr}")
        return 2

    if problems:
        print(f"::error::dangling defer target(s) ({len(problems)}) - named as "
              f"the owner of a responsibility but no tracked file in the repo "
              f"has that basename:")
        for p in problems:
            print(f"  {p}")
        return 1

    total_hits = sum(len(v) for v in hits_by_path.values())
    print(f"OK - {len(scanned)} tracked hooks/+scripts/ file(s) scanned, "
          f"{total_hits} defer target(s) found, all resolve.")
    return 0


def self_test() -> int:
    """POSITIVE (a dangling target must be reported) + NEGATIVE (a target that
    resolves must be silent) + a check that the negative control itself would
    catch a checker that reports every hit regardless of the known set, plus a
    detector round-trip and the self-exclusion invariant. Everything below
    drives the pure find_deferrals()/dangling() functions against synthetic
    strings and injected sets -- no real repo file is read or written."""
    fails: List[str] = []

    # 1. POSITIVE: a deferral naming a basename nothing in the known set ships
    #    must come back as a dangling hit, one per occurrence.
    positive_src = (
        'x = 1  # defer_to="ghost-guard.py"\n'
        "prose: MEMORY.md remediation is owned by ghost-guard.py\n"
    )
    hits = {"hooks/fake.py": find_deferrals(positive_src)}
    known_without_ghost = {"fake.py", "other.py"}  # ghost-guard.py deliberately absent
    problems = dangling(hits, known_without_ghost)
    if len(problems) != 2:
        fails.append(f"POSITIVE: expected 2 dangling hits, got {len(problems)}: {problems}")
    elif not all("ghost-guard.py" in p for p in problems):
        fails.append(f"POSITIVE: dangling hits did not name the missing target: {problems}")

    # 2. NEGATIVE: deferrals naming basenames that DO exist in the known set
    #    must be silent -- one case per prose verb plus the structural form.
    negative_src = (
        'add(x, defer_to="session-start-context.py")\n'
        "handled by auto-snapshot.sh\n"
        "delegates to worktree-footprint-signal.py\n"
        "deferred to check-py39-annotations.py\n"
    )
    hits2 = {"hooks/real.py": find_deferrals(negative_src)}
    known_all_present = {
        "session-start-context.py", "auto-snapshot.sh",
        "worktree-footprint-signal.py", "check-py39-annotations.py",
        "unrelated-decoy.py",
    }
    problems2 = dangling(hits2, known_all_present)
    if problems2:
        fails.append(f"NEGATIVE: known targets wrongly reported dangling: {problems2}")

    # 3. NEGATIVE CONTROL CHECK: prove the negative control above would
    #    actually catch a checker that reports every hit regardless of the
    #    known set (the "reports everything" failure mode named in the brief).
    #    Re-run the SAME hits against an EMPTY known set: every one of the 4
    #    must now come back dangling. If dangling() ignored `known_basenames`
    #    and always returned nothing, this would still show 0 and the
    #    negative control above would be proven not to discriminate.
    problems2_empty = dangling(hits2, set())
    if len(problems2_empty) != 4:
        fails.append(
            f"NEGATIVE-CONTROL-CHECK: against an empty known set, expected all "
            f"4 hits to be dangling, got {len(problems2_empty)} - the negative "
            f"control above cannot be trusted to discriminate"
        )

    # 4. DETECTOR: both quote styles + all four prose verbs fire; a bare
    #    filename mention with none of the trigger phrases must NOT fire.
    detector_src = (
        "a = defer_to='single.py'\n"
        "b = defer_to=\"double.sh\"\n"
        "owned by a1.py\n"
        "handled by a2.py\n"
        "delegates to a3.sh\n"
        "deferred to a4.py\n"
        "see also unrelated.py for context\n"  # must NOT fire -- no trigger phrase
    )
    found = {target for _, target in find_deferrals(detector_src)}
    expected = {"single.py", "double.sh", "a1.py", "a2.py", "a3.sh", "a4.py"}
    if found != expected:
        fails.append(f"DETECTOR: expected {sorted(expected)}, got {sorted(found)}")

    # 4b. PATH-QUALIFIED + .ps1 RESOLUTION. Regression pins for an adversarial
    #     review finding: a directory-qualified target used to match NOTHING, so
    #     `owned by hooks/does-not-exist.py` produced zero hits and the run
    #     printed "all resolve" -- reproducing the exact reads-as-covered failure
    #     this guard exists to catch. A wrong directory must also NOT resolve.
    qualified_src = (
        'add(x, defer_to="hooks/session-start-context.py")\n'
        "owned by hooks/does-not-exist-at-all.py\n"
        'defer_to="relocate-vault.ps1"\n'
        "handled by WRONGDIR/session-start-context.py\n"
    )
    qhits = find_deferrals(qualified_src)
    qtargets = [tg for _, tg in qhits]
    q_expected = ["hooks/session-start-context.py", "hooks/does-not-exist-at-all.py",
                  "relocate-vault.ps1", "WRONGDIR/session-start-context.py"]
    if qtargets != q_expected:
        fails.append(f"PATH-QUALIFIED: expected {q_expected}, got {qtargets}")
    else:
        q_known = {"hooks/session-start-context.py", "session-start-context.py",
                   "scripts/relocate-vault.ps1", "relocate-vault.ps1"}
        q_dangling = dangling({"hooks/q.py": qhits}, q_known)
        q_missing = sorted(d.split(" -> ")[1] for d in q_dangling)
        q_want = sorted(["hooks/does-not-exist-at-all.py",
                         "WRONGDIR/session-start-context.py"])
        if q_missing != q_want:
            fails.append(
                f"PATH-QUALIFIED: expected exactly {q_want} to be dangling, got "
                f"{q_missing} - a path-qualified target must resolve as a PATH, "
                f"so a right basename in a wrong directory does not pass")

    # 5. SELF-EXCLUSION: this checker's own path must never be part of a real
    #    scan (its docstring/self-test are a guaranteed false positive).
    real_paths = tracked_files()
    if real_paths is None:
        real_paths = walk_files()
    real_scanned = scan_targets(real_paths)
    self_rel = SELF_PATH.relative_to(REPO).as_posix()
    if self_rel in real_scanned:
        fails.append("SELF-EXCLUSION: the checker's own path was included in a real scan")

    # 6. INCOMPLETE-SCAN control: the skipped-read path must actually trigger
    #    on something unreadable, and must NOT trigger on an ordinary file.
    #    Without this, the exit-2 branch is a sentence nobody has ever run.
    #    Uses a FIFO (never a regular file) in a temp dir -- the repo is untouched.
    import os
    import tempfile
    tmpd = tempfile.mkdtemp(prefix="defer-targets-selftest-")
    try:
        # os.mkfifo does not exist on Windows. Guarded the way this repo already
        # guards it in check-sync-folder-machinery.py, test_safe_read.py,
        # test_worktree_cloud_safe_recovery.py and test-relocate-sweep.py.
        if hasattr(os, "mkfifo"):
            fifo = os.path.join(tmpd, "a-fifo")
            os.mkfifo(fifo)
            fifo_result = safe_read_text(fifo, timeout=1.0, max_bytes=READ_MAX_BYTES,
                                         encoding="utf-8", errors="replace")
            if fifo_result.ok:
                fails.append("INCOMPLETE-CONTROL: a FIFO read as ok - the skipped-read "
                             "branch would never fire and a partial scan would print clean")
        else:
            # No FIFO available: use a directory, which is also not a regular file.
            notfile = os.path.join(tmpd, "a-dir")
            os.mkdir(notfile)
            dir_result = safe_read_text(notfile, timeout=1.0, max_bytes=READ_MAX_BYTES,
                                        encoding="utf-8", errors="replace")
            if dir_result.ok:
                fails.append("INCOMPLETE-CONTROL: a directory read as ok - the "
                             "skipped-read branch would never fire")
        plain = os.path.join(tmpd, "plain.py")
        with open(plain, "w", encoding="utf-8") as fh:
            fh.write('defer_to="whatever.py"\n')
        plain_result = safe_read_text(plain, timeout=1.0, max_bytes=READ_MAX_BYTES,
                                      encoding="utf-8", errors="replace")
        if not plain_result.ok or "whatever.py" not in (plain_result.text or ""):
            fails.append("INCOMPLETE-CONTROL: an ordinary file did not read back "
                         "cleanly - every scan would report INCOMPLETE")
    except (OSError, NotImplementedError) as exc:
        fails.append(f"INCOMPLETE-CONTROL: could not run ({exc.__class__.__name__})")
    finally:
        for root, _dirs, files in os.walk(tmpd, topdown=False):
            for f in files:
                try:
                    os.unlink(os.path.join(root, f))
                except OSError:
                    pass
        try:
            os.rmdir(tmpd)
        except OSError:
            pass

    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK - check-defer-targets self-test passed "
          "(positive + negative + negative-control-check + detector + "
          "self-exclusion + incomplete-scan)")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
