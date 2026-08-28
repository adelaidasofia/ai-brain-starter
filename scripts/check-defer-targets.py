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

Exit 0 when every named target resolves. Exit 1, listing each
`path:line -> missing target`, when any dangling deferral is found.

Usage:
  check-defer-targets.py               # scan tracked hooks/ + scripts/ files
  check-defer-targets.py --self-test   # positive + negative control
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

REPO = Path(__file__).resolve().parent.parent
SELF_PATH = Path(__file__).resolve()

SCAN_DIRS = ("hooks", "scripts")

BASENAME = r"[A-Za-z0-9_.-]+\.(?:py|sh)"
STRUCTURAL_RE = re.compile(r"""defer_to\s*=\s*(['"])(""" + BASENAME + r""")\1""")
PROSE_RE = re.compile(
    r"\b(?:owned by|handled by|delegates to|deferred to)\s+(" + BASENAME + r")\b"
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
    return {Path(p).name for p in paths}


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
            if target not in known_basenames:
                out.append(f"{path}:{lineno} -> {target}")
    return out


def scan_repo() -> Tuple[List[str], Dict[str, List[Tuple[int, str]]], Set[str]]:
    """I/O shell: real discovery + real file reads, feeding the pure functions
    above. Returns (scanned paths, hits by path, resolvable basename set)."""
    all_paths = tracked_files()
    if all_paths is None:
        all_paths = walk_files()
    known = resolvable_basenames(all_paths)

    scanned = scan_targets(all_paths)
    hits_by_path: Dict[str, List[Tuple[int, str]]] = {}
    for rel in scanned:
        fp = REPO / rel
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = find_deferrals(text)
        if hits:
            hits_by_path[rel] = hits
    return scanned, hits_by_path, known


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()

    scanned, hits_by_path, known = scan_repo()
    problems = dangling(hits_by_path, known)

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

    # 5. SELF-EXCLUSION: this checker's own path must never be part of a real
    #    scan (its docstring/self-test are a guaranteed false positive).
    real_paths = tracked_files()
    if real_paths is None:
        real_paths = walk_files()
    real_scanned = scan_targets(real_paths)
    self_rel = SELF_PATH.relative_to(REPO).as_posix()
    if self_rel in real_scanned:
        fails.append("SELF-EXCLUSION: the checker's own path was included in a real scan")

    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK - check-defer-targets self-test passed "
          "(positive + negative + negative-control-check + detector + self-exclusion)")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
