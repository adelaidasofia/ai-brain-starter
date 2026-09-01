"""claude_project_key — single source of truth for the ~/.claude/projects directory key.

Claude Code stores per-project state (transcripts, agent memory) under
`~/.claude/projects/<key>`, where <key> is the absolute cwd with EVERY
non-alphanumeric character replaced by a dash — the leading slash included:

    /Users/me/app            -> -Users-me-app
    /Users/me/MDv0.3.0       -> -Users-me-MDv0-3-0
    /Users/me/My Vault       -> -Users-me-My-Vault
    /Users/me/v/.claude/wt/x -> -Users-me-v--claude-wt-x
    C:\\Users\\me\\app          -> C--Users-me-app

One rule, every platform.

WHY THIS MODULE EXISTS
──────────────────────
Six call sites in this repo each hand-rolled this key, and no two agreed.
Measured 2026-08-24 against 784 real directories in a live ~/.claude/projects
(0 of which contain any character outside [a-zA-Z0-9-]):

    hooks/context-budget-measure.py      0/4 sampled cwds matched
    scripts/passive-capture.py           0/4
    scripts/token-usage-report.py        2/4
    scripts/hallucination-sample-audit   2/4
    scripts/context-audit.py             2/4
    (this module)                        4/4

Two distinct defects were in play:

1. DOUBLE LEADING DASH — dead for every path, on every platform.
   `"-" + str(Path(cwd)).replace("/", "-")` produces `--Users-...`, because
   `str(Path(cwd))` already begins with `/` which becomes the first dash and
   the `"-" +` prepends a second. The real key has ONE leading dash. The tell
   that this was a bug and not a choice: context-budget-measure.py built its
   key that way and then, on the very next line, matched it against the regex
   `(.*)--claude-worktrees-[^/]+$` — the CORRECT double-dash spelling that its
   own key could never produce. A function inconsistent with itself.

2. ONLY SLASHES DASHED — dots, spaces, `@` and non-ASCII survive. Silently
   misses every worktree session (`.claude` -> `-claude`, not `--claude`) and
   any vault living under a cloud-sync path with an `@` or a space in it.

Neither failed loudly. An absent directory reads as "no data yet" to every
caller, so a broken key is indistinguishable from an empty project. That is
the same silent-false-clean signature as an inert regex: the check runs, finds
nothing, and reports healthy.

USE THIS, never a hand-rolled `.replace("/", "-")`. `tests/test_claude_project_key.py`
carries a POSITIVE CONTROL asserting the key resolves a directory that actually
exists on the running machine — a resolver that matches nothing would otherwise
pass a pure unit test forever.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["claude_project_key", "claude_project_dir", "projects_root"]

# Every character that is not ASCII alphanumeric becomes a dash. Deliberately
# NOT \w (which matches underscore and, under re.UNICODE, accented letters —
# both of which Claude Code dashes).
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")


def claude_project_key(cwd: str | Path) -> str:
    """Return the ~/.claude/projects directory name for an absolute cwd.

    Pure string transform: no filesystem access, so it is safe to call for a
    path that does not exist yet (callers that go on to CREATE the directory
    must create the one Claude Code will actually read).
    """
    return _NON_ALNUM.sub("-", str(cwd))


def projects_root(home: Path | None = None) -> Path:
    """The ~/.claude/projects root. `home` is injectable for tests."""
    return (home or Path.home()) / ".claude" / "projects"


def claude_project_dir(cwd: str | Path, home: Path | None = None) -> Path:
    """Full path to a cwd's Claude project directory.

    Returns the CURRENT spelling unconditionally — there is no legacy fallback
    here on purpose. This repo's own broken resolvers created directories under
    several wrong spellings; a fallback that accepted them would keep reading
    our own stale copies instead of what Claude Code writes.
    """
    return projects_root(home) / claude_project_key(cwd)
