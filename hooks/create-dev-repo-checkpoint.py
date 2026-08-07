#!/usr/bin/env python3
"""Auto-checkpoint hook for ~/dev/* code repos.

Stop / SubagentStop event: when the current session's cwd is under
~/dev/<repo>, stash the working tree as a recoverable checkpoint without
modifying state. Closes the gap from CLAUDE.md (see canonical rule)
where 60+ exchange Claude Code sessions in code repos can lose uncommitted
work on branch switches — auto-snapshot.sh only covers the vault.

Recover any checkpoint via `git stash list | grep claude-checkpoint` then
`git stash apply stash@{N}` to keep it or `git stash pop` to drop on apply.

Pattern source: carlrannaberg/claudekit MIT
(cli/hooks/create-checkpoint.ts). Reimplemented clean in Python per
⚙️ Meta/rules/license-hygiene.md (read in browser, take notes, close tab,
write fresh).

Bypass: DEV_CHECKPOINT_BYPASS=1
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _lib.vault_root import vault_root_for  # noqa: E402
except Exception:  # fail-open: never break the Stop chain on an import error
    def vault_root_for(target: Path):  # type: ignore
        return None

BYPASS_ENV = "DEV_CHECKPOINT_BYPASS"
PREFIX = "claude-checkpoint"
MAX_CHECKPOINTS = 20
DEV_ROOT = Path.home() / "dev"


def run_git(repo: Path, args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    """Run `git -C <repo> <args>` with bounded timeout. Never raises."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # Return a sentinel "failed" CompletedProcess so callers stay simple
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="")


def is_git_repo(path: Path) -> Optional[Path]:
    """Return repo root if path is under a git repo, else None."""
    res = run_git(path, ["rev-parse", "--show-toplevel"], timeout=5)
    if res.returncode != 0:
        return None
    return Path(res.stdout.strip())


def has_changes(repo: Path) -> bool:
    """True if `git status --porcelain` shows anything."""
    res = run_git(repo, ["status", "--porcelain"])
    return res.returncode == 0 and bool(res.stdout.strip())


def checkpoint(repo: Path, message: str) -> bool:
    """Stash working tree without modifying it. Return True on success."""
    # Stage everything (needed for `stash create` to capture untracked files).
    # SAFE in ~/dev/* repos (~1K files); explicitly banned in the 60K-file
    # vault by block-raw-vault-git.py — this hook gates on DEV_ROOT first.
    add = run_git(repo, ["add", "-A"], timeout=30)
    if add.returncode != 0:
        return False

    # `git stash create` returns a SHA but does NOT modify the working tree
    # or the stash ref list — purely a snapshot object.
    create = run_git(repo, ["stash", "create", message], timeout=30)
    sha = create.stdout.strip()
    if create.returncode != 0 or not sha:
        # Nothing to stash, or create failed. Unstage and bail.
        run_git(repo, ["reset"])
        return False

    # Store the snapshot in the stash list so `git stash list` shows it.
    store = run_git(repo, ["stash", "store", "-m", message, sha])
    if store.returncode != 0:
        run_git(repo, ["reset"])
        return False

    # Unstage; working tree is unchanged from start.
    run_git(repo, ["reset"])
    return True


def rotate_checkpoints(repo: Path, max_count: int) -> None:
    """Drop oldest claude-checkpoint stashes beyond max_count.

    `git stash list` shows newer stashes at lower indices. We keep the first
    max_count entries matching our prefix and drop the rest, highest-index
    first so lower indices stay valid during iteration.
    """
    res = run_git(repo, ["stash", "list"])
    if res.returncode != 0:
        return

    ours_indices: list[int] = []
    for i, line in enumerate(res.stdout.splitlines()):
        if PREFIX in line:
            ours_indices.append(i)

    to_drop = ours_indices[max_count:]
    for idx in sorted(to_drop, reverse=True):
        run_git(repo, ["stash", "drop", f"stash@{{{idx}}}"])


def main() -> int:
    if os.environ.get(BYPASS_ENV) == "1":
        return 0

    # Hook payload from stdin per Anthropic hook spec; never block on bad input
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    # Determine cwd from payload, env, or process
    cwd_str = payload.get("cwd") or os.environ.get("PWD") or os.getcwd()
    try:
        cwd = Path(cwd_str).resolve()
    except (OSError, RuntimeError):
        return 0

    # Defense-in-depth: NEVER fire on vault paths (handled by auto-snapshot.sh).
    #
    # MYC-3529 — resolved PER CWD, not from $VAULT_ROOT. This gate is what keeps
    # `git add -A` (below, in checkpoint()) away from a 60K-file vault, and
    # block-raw-vault-git.py bans exactly that op there. The old code compared
    # against a single env-named vault with a ~/vault default, so a SECOND vault
    # sailed through: a vault checked out under ~/dev (e.g. ~/dev/mycelium-vault,
    # which this repo's own hooks document as a real vault-namespace repo) passes
    # the ~/vault test AND the DEV_ROOT test below, and gets the full-tree stage
    # the vault guards exist to prevent. vault_root_for detects the vault from
    # the cwd itself, so any vault is excluded regardless of what the env says.
    vault = vault_root_for(cwd)
    if vault is not None:
        try:
            cwd.relative_to(vault)
            return 0  # cwd is under A vault, skip
        except ValueError:
            pass  # Not under that vault, continue

    # Only fire under ~/dev/<repo>
    try:
        cwd.relative_to(DEV_ROOT.resolve())
    except ValueError:
        return 0

    repo = is_git_repo(cwd)
    if repo is None:
        return 0

    # Re-assert the repo path itself is under ~/dev (catches symlink escapes)
    try:
        repo.resolve().relative_to(DEV_ROOT.resolve())
    except ValueError:
        return 0

    if not has_changes(repo):
        return 0

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    message = f"{PREFIX}: {timestamp} ({repo.name})"

    ok = checkpoint(repo, message)
    if ok:
        rotate_checkpoints(repo, MAX_CHECKPOINTS)

    # Silent — don't pollute session-end output. Recovery path is documented
    # in the file docstring; user discovers via `git stash list`.
    return 0


if __name__ == "__main__":
    sys.exit(main())
