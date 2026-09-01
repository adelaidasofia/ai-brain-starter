#!/usr/bin/env python3
"""Every scripts/*.py check CI requires must also run in the local gate.

`ci-test` exists so a push is not the test loop. It can only do that if the
local gate runs what CI runs. Measured 2026-09-01: `.github/workflows/lint.yml`
invoked 20 `scripts/*.py` checks and `scripts/ci.sh` ran 9 of them, so `ci-test`
reported GREEN on a commit CI then rejected -- the local gate was silently
NARROWER than the required check it stands in for, which is exactly how a gate
teaches push-and-see. Found the honest way: a push went red on
check-hook-negative-control.py, a 0.2s pure-Python scan there was no reason to
omit.

This is the ratchet that keeps it closed. Adding a check to lint.yml and not to
ci.sh now fails HERE, at review time, instead of surfacing as a mystery CI red
on someone else's unrelated PR.

EXCLUSIONS are deliberate and must state a REASON. An exclusion is a claim that
a check CANNOT run locally -- not that it was inconvenient. "It failed on my
box" is a reason to fix the check or the box, not to add a line here.

Run: python3 scripts/check-local-gate-parity.py [--self-test]
"""
# exit-contract: ENFORCING
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LINT = REPO / ".github" / "workflows" / "lint.yml"
CI = REPO / "scripts" / "ci.sh"

# name -> why it cannot run in the local gate. Keep this SHORT; every entry is
# coverage the local gate does not have.
EXCLUSIONS = {
    "check-shipped-version-drift.py":
        "compares the repo against the DEPLOYED install, so it is a property of "
        "the developer's machine, not of the commit; it fails locally by design",
    "stale-rule-check.py":
        "returns exit 3 (DECLINED, not a failure) when it cannot date rules "
        "locally, and a gate must not treat a refusal-to-measure as a verdict",
}


def lint_invocations(text: str) -> set[str]:
    """scripts/*.py files lint.yml references outside a comment.

    Deliberately NOT a `run:`-prefix scan. lint.yml uses 11 block scalars
    (`run: |`), and two checks appear ONLY inside one, so a line-prefix parser
    silently under-reports -- a parity check that misses a required check is a
    false clean, the exact failure this file exists to prevent. My own
    self-test caught that on the first draft.

    The two error directions are not symmetric. Over-detecting (counting a path
    that is not an invocation) demands a check ci.sh does not need: LOUD, and
    fixed by an EXCLUSIONS entry. Under-detecting hides a real gap: SILENT. So
    this leans permissive and takes every non-comment mention.
    """
    found: set[str] = set()
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        found.update(re.findall(r"scripts/([A-Za-z0-9_.-]+\.py)", line))
    return found


def ci_invocations(text: str) -> set[str]:
    """scripts/*.py files ci.sh RUNS. A mention inside a comment does not count.

    That distinction is the whole point: ci.sh referenced
    check-hook-negative-control.py in a comment for months while never running
    it, which reads identically to coverage in a grep.
    """
    found: set[str] = set()
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        found.update(re.findall(r"scripts/([A-Za-z0-9_.-]+\.py)", s))
    return found


def _self_test() -> int:
    """Both directions, so a pass here cannot be vacuous."""
    inline = 'jobs:\n  x:\n    steps:\n      - run: python3 scripts/check-alpha.py\n'
    block = ('jobs:\n  x:\n    steps:\n      - name: t\n        run: |\n'
             '          python3 scripts/check-beta.py --self-test\n')
    lint_comment = '# scripts/check-gamma.py was removed\njobs: {}\n'
    ok = 'set -e\n"$PY" scripts/check-alpha.py\n'
    commented = 'set -e\n# see scripts/check-alpha.py for why\n'
    problems = []
    if lint_invocations(inline) != {"check-alpha.py"}:
        problems.append("missed an INLINE `- run:` invocation")
    if lint_invocations(block) != {"check-beta.py"}:
        problems.append("missed an invocation inside a `run: |` BLOCK SCALAR — "
                        "lint.yml has 11 of these and two checks live only there")
    if lint_invocations(lint_comment):
        problems.append("counted a YAML comment as an invocation")
    if ci_invocations(ok) != {"check-alpha.py"}:
        problems.append("ci_invocations missed a real invocation")
    if ci_invocations(commented):
        problems.append("a COMMENT was counted as an invocation — the exact "
                        "false-coverage this check exists to catch")
    if problems:
        for p in problems:
            print(f"  self-test FAIL: {p}")
        return 1
    print("check-local-gate-parity self-test OK (inline + block-scalar `run:` "
          "both detected; comments on either side count as neither)")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()
    try:
        required = lint_invocations(LINT.read_text(encoding="utf-8"))
        local = ci_invocations(CI.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"check-local-gate-parity: cannot read a gate file: {exc}")
        return 2  # fail LOUD; a parity check that cannot read is not a pass

    stale = sorted(n for n in EXCLUSIONS if n not in required)
    missing = sorted(required - local - set(EXCLUSIONS))

    if missing:
        print(f"local gate parity FAILED — {len(missing)} check(s) run in CI's "
              f"lint job but NOT in scripts/ci.sh:")
        for n in missing:
            print(f"  - scripts/{n}")
        print("\nci-test would report GREEN on a commit CI then rejects. Add each "
              "to scripts/ci.sh with the SAME invocation lint.yml uses (read it — "
              "the flags differ per check), or add an EXCLUSIONS entry stating why "
              "it cannot run locally.")
        return 1

    if stale:
        print(f"local gate parity FAILED — {len(stale)} EXCLUSIONS entr(ies) name "
              f"a check lint.yml no longer runs:")
        for n in stale:
            print(f"  - {n}  (remove it; a stale exclusion hides a real gap)")
        return 1

    print(f"local gate parity OK — {len(required)} lint check(s); "
          f"{len(required) - len(EXCLUSIONS)} run locally, "
          f"{len(EXCLUSIONS)} documented exclusion(s).")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
