#!/usr/bin/env python3
"""check-template-purity.py — public-template-purity gate (MYC-1765).

The STRUCTURAL isolation plane: blocks POPULATED typed-category content (a real
floor / deal / counterparty / amount / tenant-id) from public-bound skill files.
Companion to personal-pii-scrub.yml (which catches NAMES + vault paths). The two
planes are non-overlapping. Detection logic lives in hooks/_lib/template_purity.py
so this CLI, CI, the local pre-push gate, and the write-time hook all share ONE
source of truth and cannot drift (the open-core-boundary.sh / ci.sh pattern).

Usage:
  check-template-purity.py <file> [<file> ...]   # scan specific files
  check-template-purity.py --skills              # scan all tracked skills/**/*.md (CI default)
  check-template-purity.py --staged              # scan git-staged public-bound files (pre-push)

Exit 0 = template-pure. Exit 1 = at least one populated file (each named with the
offending line + reason). Exit 2 = usage / fail-closed error.

Runs in: .github/workflows/template-purity.yml (CI) and locally pre-push.
Python 3.9 compatible.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # Python 3.7+
    except (AttributeError, ValueError):
        pass
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hooks"))

try:
    from _lib.template_purity import Violation, scan_file
except Exception as e:  # fail-closed: the guard must never silently no-op
    print("TEMPLATE-PURITY ERROR: cannot import detector (%s)" % e, file=sys.stderr)
    sys.exit(2)

_SCAN_EXTS = (".md", ".mdx", ".markdown")


def _git(args: list[str]) -> list[str]:
    try:
        out = subprocess.run(
            ["git"] + args, cwd=str(REPO), capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def _skills_files() -> list[str]:
    files = _git(["ls-files", "--", "skills/"])
    return [f for f in files if f.lower().endswith(_SCAN_EXTS)]


def _staged_files() -> list[str]:
    files = _git(["diff", "--cached", "--name-only", "--diff-filter=ACM", "--", "skills/"])
    return [f for f in files if f.lower().endswith(_SCAN_EXTS)]


def _report(findings: dict[str, list[Violation]]) -> None:
    print("PUBLIC-TEMPLATE-PURITY VIOLATION (MYC-1765 — Jackie's 2nd isolation plane).",
          file=sys.stderr)
    print(file=sys.stderr)
    print("These public-bound files carry POPULATED personal data where an empty",
          file=sys.stderr)
    print("template was required. Open artifacts must be templates, never real",
          file=sys.stderr)
    print("floors / deals / counterparties / amounts / tenant-ids:", file=sys.stderr)
    print(file=sys.stderr)
    for path in sorted(findings):
        print("  %s" % path, file=sys.stderr)
        for v in findings[path]:
            loc = ("L%d" % v.line) if v.line else "-"
            print("      %-5s %-22s %s" % (loc, v.rule, v.excerpt), file=sys.stderr)
    print(file=sys.stderr)
    print("Fix: replace the real value with a placeholder (<floor-name>, $<amount>,",
          file=sys.stderr)
    print("tnt_<id>, EXAMPLE, ...). The name-scrub (personal-pii-scrub.yml) covers",
          file=sys.stderr)
    print("names; this plane covers populated SHAPES. See docs / CLAUDE.md 'two",
          file=sys.stderr)
    print("isolation planes'.", file=sys.stderr)


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: check-template-purity.py [--skills | --staged | <file> ...]",
              file=sys.stderr)
        return 2

    if "--skills" in args:
        targets = _skills_files()
        # fail-closed: --skills must find a skills/ tree. An empty result means
        # either no skills dir or a broken git invocation — never a silent pass.
        if not targets and not (REPO / "skills").is_dir():
            print("TEMPLATE-PURITY ERROR: --skills but no skills/ directory at %s" % REPO,
                  file=sys.stderr)
            return 2
    elif "--staged" in args:
        targets = _staged_files()
    else:
        targets = [a for a in args if not a.startswith("--")]
        missing = [t for t in targets if not Path(t).exists()]
        if missing:
            for m in missing:
                print("TEMPLATE-PURITY ERROR: file not found: %s" % m, file=sys.stderr)
            return 2

    findings: dict[str, list[Violation]] = {}
    for path in targets:
        vs = scan_file(path)
        if vs:
            findings[path] = vs

    if findings:
        _report(findings)
        return 1

    n = len(targets)
    print("template-purity OK: %d public-bound file(s) are template-pure." % n)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # fail-closed
        print("TEMPLATE-PURITY ERROR: %s" % e, file=sys.stderr)
        sys.exit(2)
