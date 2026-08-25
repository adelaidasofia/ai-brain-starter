#!/usr/bin/env python3
"""SessionStart hook: surface session artifacts left UNCOMMITTED in a worktree.

A worktree's own checkout sits on a throwaway `claude/<slug>` branch. Anything
written there and not committed is discarded when the worktree is archived
("N uncommitted changes that will be permanently discarded"). Session files,
Decisions, Captures and Time Tracking belong in the MAIN vault.

detect-closing-signal.py resolves close-cascade writes to the main vault
(resolve_main_vault). This hook is the Layer 3 canary: at the NEXT session
start it scans every worktree for uncommitted session artifacts and surfaces
them loudly, so a slip-through is caught the next day instead of silently
discarded weeks later.

Complements surface-orphan-claude-branches.py:
  - that hook : committed-but-unmerged commits on claude/* branches.
  - this hook : uncommitted changes in a worktree working dir.

Output: silent when clean. One systemMessage block if any worktree holds
uncommitted session artifacts.

Performance: git status is pathspec-scoped to the Meta session dirs, so it
never walks the full vault tree.

Bypass: STRANDED_ARTIFACT_SURFACE_BYPASS=1 in env.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Session-artifact paths under the vault's Meta dir. A worktree holding an
# uncommitted change to any of these is at risk of losing it on archive.
ARTIFACT_SUBPATHS = (
    "Sessions",
    "Decisions",
    "Handoffs",
    "Pending Team Broadcasts",
    "Session Captures.md",
    "Time Tracking.md",
)


def find_vault_root(cwd: Path) -> Path | None:
    """Walk up to the vault root (a dir with .git/ and a Meta-ish folder).

    If cwd is inside a worktree, reset to the main vault first. Mirrors
    find_vault_root() in surface-orphan-claude-branches.py.
    """
    parts = cwd.parts
    if ".claude" in parts:
        idx = parts.index(".claude")
        if idx + 1 < len(parts) and parts[idx + 1] == "worktrees":
            if idx > 0:
                cwd = Path(*parts[:idx])

    p = cwd.resolve()
    for _ in range(8):
        if not p.is_dir():
            break
        if (p / ".git").exists():
            try:
                children = list(p.iterdir())
            except OSError:
                children = []
            for child in children:
                if child.is_dir() and child.name.endswith("Meta"):
                    return p
        parent = p.parent
        if parent == p:
            break
        p = parent
    return None


def find_meta_name(worktree: Path) -> str | None:
    """Return the Meta dir name inside a worktree (emoji prefix or plain)."""
    for name in ("⚙️ Meta", "Meta"):
        if (worktree / name).is_dir():
            return name
    return None


def stranded_in_worktree(worktree: Path) -> list[str]:
    """Return uncommitted session-artifact paths inside a worktree.

    git status is scoped via pathspec to the Meta session dirs only, so it
    does not walk the whole vault. Non-existent paths are filtered out up
    front so git is never handed a pathspec that matches nothing.
    """
    meta = find_meta_name(worktree)
    if meta is None:
        return []

    pathspecs = []
    for sub in ARTIFACT_SUBPATHS:
        if (worktree / meta / sub).exists():
            pathspecs.append(f"{meta}/{sub}")
    if not pathspecs:
        return []

    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "-c", "core.quotePath=false",
             "status", "--porcelain", "--"] + pathspecs,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []

    stranded = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        # Rename entries are "orig -> new"; the new path is what is at risk.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        stranded.append(path)
    return stranded


def _stalled_git_report() -> str | None:
    """Delegate to the standalone stalled-operation check.

    It lives in its own module so it stays independently testable (--test ships
    a negative control), but it is CALLED from here rather than wired as its own
    SessionStart entry: the footprint SLA gate budgets substrate cold-start
    fan-out, and SessionStart is at its ceiling. One more entry is one more
    process on every session start; composition costs nothing.

    Never raises -- a surfacing hook must not break session start.
    """
    try:
        import importlib.util

        path = Path(__file__).resolve().parent / "surface-stalled-git-operation.py"
        if not path.exists():
            return None
        spec = importlib.util.spec_from_file_location("_stalled_git_op", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if os.environ.get("STALLED_GIT_OP_BYPASS") == "1":
            return None
        return mod.build_report(os.getcwd())
    except Exception:
        return None


def _stranded_message() -> str | None:
    if os.environ.get("STRANDED_ARTIFACT_SURFACE_BYPASS"):
        return None

    vault = find_vault_root(Path.cwd())
    if vault is None:
        return None

    worktrees_dir = vault / ".claude" / "worktrees"
    if not worktrees_dir.is_dir():
        return None

    try:
        worktrees = sorted(d for d in worktrees_dir.iterdir() if d.is_dir())
    except OSError:
        return None

    findings = []
    for wt in worktrees:
        stranded = stranded_in_worktree(wt)
        if stranded:
            findings.append((wt.name, stranded))

    if not findings:
        return None

    total = sum(len(files) for _, files in findings)
    lines = []
    for slug, files in findings:
        shown = ", ".join(files[:6])
        if len(files) > 6:
            shown += f", +{len(files) - 6} more"
        lines.append(f"  - {slug}: {shown}")

    msg = (
        f"[stranded-artifacts] {total} session artifact(s) sit UNCOMMITTED "
        f"inside {len(findings)} worktree(s) - they will be discarded when the "
        f"worktree is archived:\n"
        + "\n".join(lines)
        + "\n\nMove each to the main vault now: rewrite it at the main-vault "
        "path, or `git -C <worktree> add` + commit then merge the branch home. "
        "detect-closing-signal.py resolves close-cascade writes to the main "
        "vault; this catches anything that slipped through. Sibling: "
        "surface-orphan-claude-branches.py (committed-but-unmerged half)."
    )
    return msg




def main() -> int:
    # SessionStart payload arrives on stdin (JSON). Drain so we never block.
    try:
        sys.stdin.read()
    except Exception:
        pass

    # Both checks always run. The stalled-operation check is deliberately NOT
    # gated behind the vault/worktree preconditions below it -- a stalled rebase
    # blocks any repo, vault or not, and that was the whole failure mode.
    parts = [m for m in (_stalled_git_report(), _stranded_message()) if m]
    if not parts:
        return 0
    print(json.dumps({"systemMessage": "\n\n".join(parts)}))
    return 0


if __name__ == "__main__":
    # This file carries non-ASCII source (the vault's Meta dir name) and prints
    # to the console. A Windows console defaults to cp1252 and dies mid-write
    # without this (ai-brain-starter#313). Previously carried as a baseline
    # exemption; paid down here rather than re-pinned.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
