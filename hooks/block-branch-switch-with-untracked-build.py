#!/usr/bin/env python3
"""Block destructive git ops in ~/dev/* when an in-flight build is uncommitted.

Catches: `git checkout <branch>`, `git switch`, `git stash` (push), `git pull`,
`git reset --hard`, `git clean -fdx` — any op that can lose untracked work
on a branch transition.

Refuses if the active dev repo has 3+ untracked source files in a single
2-segment-prefix directory (the "module-in-flight" pattern). The block
points at the recovery command (commit explicit paths or stash with -u
explicitly) and the bypass var.

Bypass: `BRANCH_SWITCH_BYPASS=1` env var, OR commit/stash explicitly first.

Why this exists: 2026-05-09 voice-module session ended with 13 new src/voice/
files + 2 new tests/voice/ files green-tested but uncommitted. A subsequent
branch-switch by another session stashed the work, and the working tree
showed only `__pycache__` artifacts. Recovery required reflog spelunking
through `928f307 untracked files on harden-digest-pipeline`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Path bootstrap so the shared scanner module imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from _lib.dev_repo_scan import find_modules_in_flight, ModuleInFlight  # type: ignore
except Exception:
    # If the shared module fails to import, FAIL OPEN (do not block the user).
    print(json.dumps({}))
    sys.exit(0)

# Inline-bypass consult: os.environ can't see a `BRANCH_SWITCH_BYPASS=1 <cmd>`
# prefix that lives only in the command string, never the hook's own env
# (HOOK-READS-SESSION-ENV-NOT-COMMAND-ENV). Fail-open to env-only if the
# shared helper is unavailable -- never let a missing _lib escalate this into
# a block. This file already fails the whole hook open above when _lib is
# unreachable, so the import is safe to fold into the same try/except.
try:
    from _lib.cmd_env import inline_bypass  # type: ignore
except Exception:
    def inline_bypass(command, var, value="1"):  # type: ignore
        return False


DANGEROUS_PATTERNS = [
    # checkout to a branch (NOT `checkout -- <file>` which is revert)
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?checkout\s+(?!--\s)(?!-p\b)\S"),
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?switch\s+\S"),
    # stash push (default subcommand of `git stash`)
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?stash(?:\s+push)?(?:\s|$)"),
    # destructive pulls + resets
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?pull\b(?!.*--ff-only)"),
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?reset\s+--hard"),
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?clean\s+-[fdx]+"),
]

DEV_REPO_PATH_RE = re.compile(r"/Users/[^/]+/dev/([^/\s'\"]+)")
HOME_DEV_PATH_RE = re.compile(r"~/dev/([^/\s'\"]+)")


def _bypass(cmd: str) -> bool:
    if os.environ.get("BRANCH_SWITCH_BYPASS") == "1":
        return True
    return inline_bypass(cmd, "BRANCH_SWITCH_BYPASS")


def _command_is_dangerous(cmd: str) -> bool:
    return any(p.search(cmd) for p in DANGEROUS_PATTERNS)


def _scope_repo_from_command(cmd: str) -> Path | None:
    """Extract the target repo from a `cd <repo> && git ...` or `git -C <repo>` form."""
    # Explicit -C <path>
    m = re.search(r"\bgit\s+-C\s+(\S+)", cmd)
    if m:
        return Path(os.path.expanduser(m.group(1).strip("'\"")))
    # cd <path> in a chain
    m = re.search(r"\bcd\s+(\S+)", cmd)
    if m:
        return Path(os.path.expanduser(m.group(1).strip("'\"")))
    # ~/dev/<repo>/... or /Users/.../dev/<repo>/...
    m = HOME_DEV_PATH_RE.search(cmd)
    if m:
        return Path(os.path.expanduser(f"~/dev/{m.group(1)}"))
    m = DEV_REPO_PATH_RE.search(cmd)
    if m:
        return Path(f"/Users/{Path.home().name}/dev/{m.group(1)}")
    return None


def _allow():
    print(json.dumps({}))
    sys.exit(0)


def _block(reason: str):
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))
    sys.exit(0)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        _allow()

    if data.get("tool_name") != "Bash":
        _allow()

    cmd = (data.get("tool_input") or {}).get("command", "") or ""
    if not _command_is_dangerous(cmd):
        _allow()

    if _bypass(cmd):
        _allow()

    # Determine which repo to scan. Prefer the repo named on the command line.
    scoped_repo = _scope_repo_from_command(cmd)
    if scoped_repo is None or not (scoped_repo / ".git").exists():
        # Without a clear scope, scan the user's most recently active dev repos
        # within the last hour. find_active_repos handles that.
        from _lib.dev_repo_scan import find_active_repos  # local import
        candidates = find_active_repos()
    else:
        candidates = [scoped_repo]

    findings: list[ModuleInFlight] = []
    for repo in candidates:
        findings.extend(find_modules_in_flight(repo))

    if not findings:
        _allow()

    # Build a focused block message
    lines = [
        "**[block-branch-switch-with-untracked-build]**",
        "",
        f"Refusing this command — the working tree contains "
        f"{len(findings)} module-in-flight pattern(s) "
        f"(3+ untracked source files in a single directory). "
        f"Branch-switching, stashing, hard-reset, or pull on top of this "
        f"will silently lose the untracked .py/.ts/.go/etc files; only "
        f"`__pycache__` artifacts may survive.",
        "",
        "Found:",
    ]
    for f in findings[:5]:
        lines.append(
            f"- `{f.repo}` :: `{f.module_dir}` "
            f"({f.file_count} untracked source files)"
        )
        for relpath in f.files[:6]:
            lines.append(f"    - {relpath}")
        if f.file_count > 6:
            lines.append(f"    - ... and {f.file_count - 6} more")
    if len(findings) > 5:
        lines.append(f"- ... and {len(findings) - 5} more module(s)")

    lines += [
        "",
        "**Recovery (pick one, then re-run the command):**",
        "",
        "1. Commit the work explicitly on the current branch:",
        "   `cd <repo> && git checkout -b feat/<slug> && git add <explicit paths> && git commit -m 'feat: ...'`",
        "",
        "2. Stash WITH untracked files (use `-u`, default `git stash` excludes them):",
        "   `cd <repo> && git stash push -u -m 'wip: <module slug>'`",
        "",
        "3. If the destructive op is intentional and the work IS expendable:",
        "   `BRANCH_SWITCH_BYPASS=1 <your-original-command>`",
        "",
        "Permanent-fix-pattern guard. Codified 2026-05-10 after the voice-module "
        "near-loss. `~/.claude/hooks/block-branch-switch-with-untracked-build.py`. "
        "See `⚙️ Meta/rules/branch-switch-safety.md` for the full rule.",
    ]

    _block("\n".join(lines))


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
    main()
