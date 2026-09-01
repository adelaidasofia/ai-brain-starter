#!/usr/bin/env python3
"""check-py-import-precommit.py

PreToolUse(Bash) hook. Before a `git commit`, run `ruff check --select F821`
(undefined name — the rule that catches missing / typo'd imports) on the staged
.py files. Warn-block on any finding so a commit with an obvious NameError /
ImportError never lands.

WHY
---
Surface symptom of a real multi-session failure: a sibling session committed
in-progress fixes that referenced un-imported names, CI failed on the missing
imports, the session reverted to a clean state, and ~45 min of in-flight work
was wiped. F821 at commit time catches that class locally, before it reaches CI.

BEHAVIOR
--------
Fires only on commands that invoke `git commit`. Resolves the effective cwd
after any leading `cd` chain, confirms a git repo, lists staged .py files,
and runs ruff against the ones that still exist in the worktree. Exit 2 (block)
on findings; exit 0 (allow) on clean, on no staged .py, on missing ruff, or on
any internal error (fail-open — never break a commit on the hook's own fault).

If `ruff` is not on PATH the guard cannot enforce. Rather than fail silently
forever (a dead guard nobody notices), it emits a once-per-day stderr nudge
pointing at the install command, then allows the commit.

Bypass: PRECOMMIT_F821_BYPASS=1 (e.g. intentional forward-reference patterns
ruff cannot resolve, or committing a known-WIP checkpoint).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Inline-bypass consult: os.environ can't see a `PRECOMMIT_F821_BYPASS=1
# <cmd>` prefix that lives only in the command string, never the hook's own
# env (HOOK-READS-SESSION-ENV-NOT-COMMAND-ENV). Fail-open to env-only if the
# shared helper is unavailable.
sys.path.insert(0, str(Path(__file__).resolve().parent / "_lib"))
try:
    from cmd_env import inline_bypass
except Exception:
    def inline_bypass(command, var, value="1"):  # type: ignore
        return False

BYPASS_ENV = "PRECOMMIT_F821_BYPASS"
GIT_TIMEOUT_SEC = 8
RUFF_TIMEOUT_SEC = 20
MAX_FILES = 200
RUFF_NUDGE_INTERVAL_SEC = 86_400  # nudge about missing ruff at most once/day
RUFF_NUDGE_MARKER = os.path.expanduser(
    "~/.claude/.cache/check-py-import-precommit/ruff-missing.nudge"
)

# Tokens whose VALUE is a separate argument (consume two tokens, not one).
_VALUE_TAKING_OPTS = {"-C", "--git-dir", "--work-tree", "--namespace", "-c"}


def _git_subcommand(tokens):
    """First non-option token after `git` — the subcommand — or None."""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in _VALUE_TAKING_OPTS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t
    return None


def _invokes_git_commit(command: str) -> bool:
    """True if any separator-chained segment runs `git commit` (tolerates leading
    env assignments, inline comments, and git options before the subcommand)."""
    for sub in re.split(r"\s*(?:&&|\|\||;|\|)\s*", command):
        sub = sub.strip()
        if not sub:
            continue
        sub = re.sub(r"^(?:[A-Z_][A-Z0-9_]*=\S+\s+)+", "", sub)
        sub = re.sub(r"\s+#.*$", "", sub)
        if re.match(r"cd\s+", sub):
            continue
        tokens = sub.split()
        if not tokens or tokens[0] != "git":
            continue
        if _git_subcommand(tokens[1:]) == "commit":
            return True
    return False


def _effective_cwd(command: str, initial: str) -> str:
    """Resolve cwd after any leading `cd <path>` in the chain."""
    cwd = os.path.expanduser(initial) if initial else ""
    for chunk in re.split(r"\s*(?:&&|\|\||;)\s*", command):
        chunk = chunk.strip()
        mm = re.match(r"cd\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))", chunk)
        if mm:
            raw = next(g for g in mm.groups() if g is not None)
            p = os.path.expanduser(raw)
            cwd = p if os.path.isabs(p) else os.path.normpath(os.path.join(cwd, p))
    return cwd


def _git(args, cwd):
    try:
        r = subprocess.run(
            ["git", "-C", cwd] + args,
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _nudge_ruff_missing_once():
    """Emit a stderr nudge about missing ruff, at most once per RUFF_NUDGE_INTERVAL_SEC."""
    try:
        now = time.time()
        if os.path.exists(RUFF_NUDGE_MARKER) and (
            now - os.path.getmtime(RUFF_NUDGE_MARKER)
        ) < RUFF_NUDGE_INTERVAL_SEC:
            return
        os.makedirs(os.path.dirname(RUFF_NUDGE_MARKER), exist_ok=True)
        with open(RUFF_NUDGE_MARKER, "w", encoding="utf-8") as f:
            f.write(str(now))
    except OSError:
        # If we can't write the marker, still nudge this once rather than crash.
        pass
    sys.stderr.write(
        "[check-py-import-precommit] ruff not found on PATH — the F821 "
        "(undefined-name / missing-import) pre-commit guard is INACTIVE. "
        "Install to activate: `uv tool install ruff` (or `pipx install ruff`). "
        "This notice repeats at most once/day.\n"
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    if os.environ.get(BYPASS_ENV) == "1" or inline_bypass(command, BYPASS_ENV):
        return 0
    if "commit" not in command or not _invokes_git_commit(command):
        return 0

    initial_cwd = os.environ.get("CLAUDE_CWD", payload.get("cwd", "")) or os.getcwd()
    cwd = _effective_cwd(command, initial_cwd) or os.getcwd()
    if not os.path.isdir(cwd) or not _git(["rev-parse", "--git-dir"], cwd):
        return 0

    staged = _git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"], cwd)
    py_files = [ln.strip() for ln in staged.splitlines()
                if ln.strip().endswith(".py")][:MAX_FILES]
    if not py_files:
        return 0

    ruff = shutil.which("ruff")
    if not ruff:
        _nudge_ruff_missing_once()
        return 0  # fail-open: ruff not installed

    existing = [f for f in py_files if os.path.isfile(os.path.join(cwd, f))]
    if not existing:
        return 0

    try:
        r = subprocess.run(
            [ruff, "check", "--select", "F821", "--output-format=concise",
             "--no-cache", "--"] + existing,
            cwd=cwd, capture_output=True, text=True, timeout=RUFF_TIMEOUT_SEC,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        return 0  # fail-open

    if r.returncode == 0:
        return 0  # clean
    findings = (r.stdout or "").strip()
    if not findings:
        # ruff failed for a non-lint reason (config parse error, etc.) -> fail-open
        return 0

    lines = findings.splitlines()
    sys.stderr.write(
        "[warn-py-import-error-pre-commit]\n"
        "BLOCKED: staged Python has F821 (undefined name) findings — usually a "
        "missing or typo'd import. Committing this risks the sibling-session "
        "broken-commit-then-revert class (CI fails on the missing import, the "
        "fix gets reverted, in-flight work is wiped). Fix the import(s) or bypass.\n\n"
        + "\n".join("  " + ln for ln in lines[:30])
        + ("\n  ... (truncated)" if len(lines) > 30 else "")
        + f"\n\nBypass: {BYPASS_ENV}=1 (intentional forward refs ruff can't "
          "resolve, or a deliberate WIP checkpoint).\n"
    )
    return 2


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    # A hook that print()s non-ASCII raises UnicodeEncodeError on a cp1252
    # console: the gate then fails silently OPEN, or denies the tool call with
    # no legible cause. Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
