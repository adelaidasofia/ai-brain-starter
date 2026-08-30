#!/usr/bin/env python3
"""
test_claude_project_key.py — stdlib-only tests for hooks/_lib/claude_project_key.py.

Run: python3 tests/test_claude_project_key.py
No pytest dependency. Exits non-zero on any failure. Writes nothing.

Why this file exists
--------------------
Six call sites in this repo hand-rolled the ~/.claude/projects directory key and
no two agreed. Two of them produced a DOUBLE leading dash (`--Users-...` instead
of `-Users-...`) and therefore matched NOTHING, for any path, on any platform —
for months, with no error, because every caller reads an absent directory as
"no data yet".

That is the trap this file is built around. A resolver that matches nothing
passes any pure unit test you write against hand-typed expectations, because the
expectations are written by the same person who wrote the bug. So the load-
bearing test here is not `assert key(x) == "-Users-x"` — it is the POSITIVE
CONTROL below, which asserts the key resolves a directory that actually exists
on the machine running the test.

  A search earns trust only by matching something it should match.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from _lib.claude_project_key import (  # noqa: E402
    claude_project_dir,
    claude_project_key,
    projects_root,
)

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")


# ── 1. the transform itself ───────────────────────────────────────────────────

check("plain posix path",
      claude_project_key("/Users/me/app"), "-Users-me-app")

# The exact shape the two dead resolvers got wrong: ONE leading dash, not two.
key = claude_project_key("/Users/me/app")
check("exactly one leading dash", key.startswith("--"), False)

# Dots dashed — the case that silently broke every worktree session, where
# `.claude` must become `--claude` (dash for the slash, dash for the dot).
check("worktree dot-directory",
      claude_project_key("/Users/me/v/.claude/worktrees/x"),
      "-Users-me-v--claude-worktrees-x")

check("version dots",
      claude_project_key("/Users/me/MDv0.3.0"), "-Users-me-MDv0-3-0")

# Spaces and @ — the cloud-sync vault path shape.
check("space and at-sign",
      claude_project_key("/Users/me/Drive-a@b.com/My Vault"),
      "-Users-me-Drive-a-b-com-My-Vault")

check("windows path",
      claude_project_key(r"C:\Users\me\app"), "C--Users-me-app")

# Non-ASCII is NOT alphanumeric for this purpose — Claude Code dashes it.
check("emoji dashed",
      claude_project_key("/Users/me/\U0001F680 Team"), "-Users-me---Team")

check("accented letter dashed",
      claude_project_key("/Users/me/caf\u00e9"), "-Users-me-caf-")

check("underscore dashed",
      claude_project_key("/Users/me/my_app"), "-Users-me-my-app")

# Accepts a Path, not just str.
check("accepts Path",
      claude_project_key(Path("/Users/me/app")), "-Users-me-app")

# Output alphabet is closed: only [A-Za-z0-9-] can come out.
for probe in ("/Users/me/a b.c@d", r"C:\x\y", "/\U0001F344/x"):
    leftover = re.sub(r"[a-zA-Z0-9-]", "", claude_project_key(probe))
    check(f"output alphabet closed for {probe!r}", leftover, "")

# home is injectable, and the dir is root + key.
fake_home = Path("/tmp/fake-home")
check("claude_project_dir composes",
      claude_project_dir("/Users/me/app", home=fake_home),
      fake_home / ".claude" / "projects" / "-Users-me-app")


# ── 2. POSITIVE CONTROL — the test that a dead resolver cannot pass ───────────
#
# Every assertion above is written against MY understanding of the rule. If that
# understanding were wrong the whole block would still pass, which is exactly how
# the six broken resolvers survived. So: take a directory Claude Code actually
# created, invert it back to a plausible cwd, and require the resolver to land on
# it. If the resolver is dead, this fails.
#
# Skips (does not fail) when the machine has no ~/.claude/projects — CI sandboxes
# legitimately have none. A skip is REPORTED, never silent, so a permanently
# skipped control cannot masquerade as a passing one.

root = projects_root()
real_dirs = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []

if not real_dirs:
    print(f"  POSITIVE CONTROL SKIPPED — no project dirs at {root} "
          f"(expected in a sandbox; NOT evidence the resolver works)")
else:
    # Claude Code's own output must already satisfy the closed alphabet. If this
    # trips, the upstream rule changed and this module is now the stale one.
    offenders = [d for d in real_dirs if re.sub(r"[a-zA-Z0-9-]", "", d)]
    check(f"all {len(real_dirs)} real dirs use the closed alphabet "
          f"(else upstream changed the rule)", offenders[:3], [])

    # Round-trip: home's own key must be a directory-name shape we'd produce,
    # and the deepest real dir we can invert must resolve back to itself.
    home_key = claude_project_key(Path.home())
    matches = [d for d in real_dirs if d == home_key or d.startswith(home_key + "-")]
    check(f"resolver reproduces at least one of {len(real_dirs)} real dirs "
          f"(prefix {home_key!r}) — a dead resolver matches ZERO here",
          len(matches) > 0, True)

    print(f"  positive control: {len(real_dirs)} real dirs, "
          f"{len(matches)} reachable from the resolver")


# ── 3. CLASS-LEVEL GUARD — nobody hand-rolls this key again ───────────────────
#
# Fixing five call sites does not stop a sixth. This scans the repo for the
# pattern that caused the bug: a file that builds a ~/.claude/projects path AND
# hand-rolls the encoding with .replace("/", "-") instead of importing the
# helper. Deliberately narrow — a bare .replace("/", "-") elsewhere (a repo slug,
# a branch name) is legitimate and must NOT trip this.

import subprocess  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ALLOWED = {                      # the helper itself, and this test
    "hooks/_lib/claude_project_key.py",
    "tests/test_claude_project_key.py",
}

try:
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "*.py"],
        capture_output=True, text=True, timeout=30,
    ).stdout.split()
except Exception as exc:                                  # pragma: no cover
    tracked = []
    print(f"  GUARD SKIPPED — git unavailable ({type(exc).__name__})")

handrolled = []
for rel in tracked:
    if rel in ALLOWED:
        continue
    try:
        body = (REPO / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    # Match every spelling of the projects path actually used in this repo:
    #   "~/.claude/projects"          (literal)
    #   Path(...) / ".claude" / "projects"   (slash-joined)
    #   Path(..., ".claude", "projects")     (comma-joined)
    # An earlier version checked only the first two and MISSED context-audit.py,
    # under-reporting by one file -- a guard whose scope is narrower than its name.
    builds_key = (
        ".claude/projects" in body
        or ('".claude"' in body and '"projects"' in body)
    )
    hand_rolls = 'replace("/", "-")' in body or "replace('/', '-')" in body
    if builds_key and hand_rolls:
        handrolled.append(rel)

# Positive control: the scan must be able to SEE files. A tracked-file list that
# came back empty would make this guard pass vacuously forever.
check("guard can see the repo (control: >20 tracked .py files)",
      len(tracked) > 20, True)

# Files still carrying the old hand-rolled key. This list is CLOSED and may only
# SHRINK. It is not an appendable suppression list: adding a 6th file fails the
# equality check below, and so does fixing one without deleting its row (a stale
# row is a lie about the codebase). These four also carry pre-existing naive
# VAULT_ROOT reads pinned in scripts/vault-root-read-baseline.txt, and that
# ratchet requires adopting _resolve_vault_root() before their bytes may change --
# a behaviour change (which vault the script operates on) that needs its own
# review. They are fixed together in a follow-up, not smuggled in here.
PENDING_MIGRATION = sorted([
    "scripts/context-audit.py",
    "scripts/hallucination-sample-audit.py",
    "scripts/passive-capture.py",
    "scripts/token-usage-report.py",
])

check("hand-rolled key sites are EXACTLY the known-pending set "
      "(a new one is a regression; a fixed one must delete its row)",
      sorted(handrolled), PENDING_MIGRATION)

if tracked:
    print(f"  class guard: scanned {len(tracked)} tracked .py files, "
          f"{len(handrolled)} hand-rolled")


# ── report ───────────────────────────────────────────────────────────────────

if FAILURES:
    print(f"\nFAIL — {len(FAILURES)} check(s):\n")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)

print("PASS — claude_project_key")
