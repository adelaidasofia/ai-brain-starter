#!/usr/bin/env python3
"""
check-phase-python.py - lint the Python embedded in phases/*.md for undefined
names (pyflakes / ruff F821).

WHY THIS EXISTS
---------------
The Phase 2 plugin installer is a `python3` heredoc *inside a Markdown file*.
The `ci` job's py_compile pass only sees tracked *.py files, so it never lints
this code - and py_compile would not catch it anyway, because an undefined name
is a RUNTIME error (NameError), not a syntax error. A bare `VAULT_DIR` typo
therefore shipped to users and crashed the installer on every platform
(ai-brain-starter install outage, 2026-07-07). windows-install.yml runs
bootstrap.ps1 end-to-end but stubs Obsidian and never executes this block, so
it did not catch it either.

This gate closes that hole: it extracts every Python block from the phase docs
(both ```python fences and `python3 ... <<'DELIM'` heredocs) and runs the
undefined-name check (ruff F821, the same check the repo already relies on for
its F821 path) over each. A reintroduced undefined name turns the gate red
before it reaches a user.

FAILS CLOSED: if neither ruff nor pyflakes is importable it exits non-zero. A
silent skip would make the gate vacuous exactly when the linter is missing
(fail-loud-not-silent-noop).

Run: python3 scripts/check-phase-python.py   (exit 0 clean, 1 on any finding)
Wired into scripts/ci.sh so the local pre-push gate and CI run the same check.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PHASES_DIR = Path(__file__).resolve().parent.parent / "phases"

# A block may opt out with this exact marker on its own line (e.g. a deliberately
# partial snippet that references names defined in surrounding prose).
SKIP_MARKER = "# check-phase-python: skip"

FENCE_OPEN = re.compile(r"^\s*```(?:python|py)\s*$")
FENCE_CLOSE = re.compile(r"^\s*```\s*$")
HEREDOC = re.compile(r"<<\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?")


def extract_blocks(md_text, fname):
    """Yield (label, code) for each Python block in one phase doc."""
    lines = md_text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if FENCE_OPEN.match(line):
            start = i + 1
            j = start
            while j < n and not FENCE_CLOSE.match(lines[j]):
                j += 1
            yield (f"{fname}:{start + 1} (```python)", "\n".join(lines[start:j]))
            i = j + 1
            continue
        m = HEREDOC.search(line)
        if m and "python" in line:
            delim = m.group(1)
            start = i + 1
            j = start
            while j < n and lines[j].strip() != delim:
                j += 1
            yield (f"{fname}:{start + 1} (<<{delim})", "\n".join(lines[start:j]))
            i = j + 1
            continue
        i += 1


def lint_block_ruff(label, code):
    """Return list of finding lines via ruff F821. ruff exits 1 on findings."""
    res = subprocess.run(
        ["ruff", "check", "--select", "F821", "--no-cache",
         "--output-format", "concise", "--stdin-filename", label, "-"],
        input=code, capture_output=True, text=True,
    )
    out = (res.stdout + res.stderr).strip()
    if res.returncode == 0:
        return []
    return [ln for ln in out.splitlines() if "F821" in ln or "undefined" in ln.lower()]


def lint_block_pyflakes(label, code):
    """Fallback: pyflakes reports `undefined name 'X'`; relabel to the block."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
        tf.write(code)
        tmp = tf.name
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pyflakes", tmp],
            capture_output=True, text=True,
        )
    finally:
        Path(tmp).unlink(missing_ok=True)
    findings = []
    for ln in (res.stdout + res.stderr).splitlines():
        if "undefined name" in ln:
            findings.append(ln.replace(tmp, label))
    return findings


def pick_linter():
    if shutil.which("ruff"):
        return "ruff", lint_block_ruff
    try:
        import pyflakes  # noqa: F401
        return "pyflakes", lint_block_pyflakes
    except ImportError:
        # pyflakes not installed. A broken-but-present pyflakes should raise
        # loudly rather than be swallowed into the fail-closed path below.
        return None, None


def main():
    name, lint = pick_linter()
    if lint is None:
        print("::error::check-phase-python needs ruff or pyflakes to lint phase-doc "
              "Python (undefined-name check). Install one: pip install ruff", file=sys.stderr)
        return 2  # fail closed - never silently pass

    phase_docs = sorted(PHASES_DIR.glob("*.md"))
    total_blocks = 0
    findings = []
    for doc in phase_docs:
        for label, code in extract_blocks(doc.read_text(encoding="utf-8"), doc.name):
            if SKIP_MARKER in code:
                continue
            total_blocks += 1
            for f in lint(label, code):
                findings.append(f)

    if findings:
        print(f"check-phase-python ({name}): undefined names in phase-doc Python:\n")
        for f in findings:
            print(f"::error::{f}")
        print(f"\n{len(findings)} finding(s) across {total_blocks} block(s) in "
              f"{len(phase_docs)} phase doc(s). An undefined name is a NameError at "
              f"install time - fix the reference before it ships.")
        return 1

    print(f"check-phase-python ({name}): OK - {total_blocks} Python block(s) across "
          f"{len(phase_docs)} phase doc(s), no undefined names (F821).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
