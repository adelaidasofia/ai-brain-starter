#!/usr/bin/env python3
"""Guard: a git worktree is living INSIDE the Obsidian-watched vault tree.

The melt
--------
The Claude Desktop app has a per-session "worktree" checkbox. On a CODE repo a
worktree under `.claude/worktrees/<slug>/` is cheap and correct. On an OBSIDIAN
VAULT (repo-root == vault-root, thousands of files) that checkbox drops a FULL
second checkout of the vault INSIDE Obsidian's watched file tree. Obsidian's
renderer indexes the doubled tree and OOM/crashes (the EXC_BREAKPOINT renderer
melt measured 2026-06-06), and the worktree can be silently archived mid-session,
taking any worktree-only files with it.

Why relocation is dead (the 2026-06-11 diagnosis)
-------------------------------------------------
You cannot fix this by moving the worktree OUT of the watched tree:
  * a SYMLINK out is followed back IN — Obsidian's file watcher follows symlinks,
    so the churn re-enters the watched tree even though a cloud-sync daemon would
    not (this is why the CLOUD_SYNC.md sidecar fixes sync but NOT this melt);
  * a WorktreeCreate redirect is NOT honored — the Desktop app does not take a
    relocation hint for where it creates the per-session worktree.
And the `tengu_worktree_mode` flag does NOT gate Desktop worktree creation, so
"set the flag" is FALSE remediation. The only honest fix is to never create the
worktree: launch the vault PLAIN with the per-session worktree box UNCHECKED.

This check is the DIAGNOSE half of that posture (the runtime half is the
`warn-vault-session-in-worktree.py` SessionStart/tool tripwire). It is a
DIAGNOSTIC, not an install gate: by the time `.claude/worktrees/` exists the
melt already happened, so diagnose treats a hit as a WARN with the
launch-unchecked remedy, never a hard FAIL.

The fire condition (all three, mirroring the runtime hook's gates)
------------------------------------------------------------------
  1. the path is an OBSIDIAN vault  (`.obsidian/` at the root) — a code repo
     worktree is fine, so `.obsidian/` is the discriminator;
  2. the vault is a GIT repo        (`.git` present / inside a work tree) — the
     Desktop worktree feature requires git, so a non-git vault cannot melt;
  3. there is worktree EVIDENCE     — a `.claude/worktrees/<slug>/` checkout
     physically present inside the vault, OR this very process is running from a
     worktree cwd under the vault.

Usage:
  check-worktree-on-vault.py [VAULT]            # human-readable verdict + remedy
  check-worktree-on-vault.py --porcelain [VAULT] # one machine-readable line

Exit codes:
  0  OK   — no worktree melting the vault tree (or not the melt class)
  1  HIT  — a git worktree is inside the Obsidian-watched vault tree
  2  USAGE — bad arguments

Porcelain first token:
  OK_NOT_VAULT | OK_NOT_GIT | OK_NO_WORKTREES | WORKTREE_ON_VAULT:<count>

Best-effort + fail-OPEN on its own errors: a diagnostic must never crash the
report it is part of. Anything it cannot evaluate degrades to OK_NO_WORKTREES
(the caller can still run every other check). It NEVER writes or deletes.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

USAGE = "usage: check-worktree-on-vault.py [--porcelain] [VAULT]"

# Single source of truth for the worktree segment + cwd detection. Reuse the
# shared lib that the cleanup hooks already trust so this surface can never drift
# from them; fall back to inline equivalents if the lib is not importable (the
# check still works standalone, which is how diagnose runs it on an end-user box).
WORKTREES_SEG = ".claude/worktrees"
_current_worktree = None
_HERE = Path(__file__).resolve().parent
for _cand in (_HERE.parent / "hooks", _HERE.parent,
              Path.home() / ".claude" / "skills" / "ai-brain-starter" / "hooks"):
    if (_cand / "_lib" / "worktree_safety.py").is_file():
        sys.path.insert(0, str(_cand))
        try:
            from _lib.worktree_safety import WORKTREES_SEG as _SEG  # type: ignore
            from _lib.worktree_safety import current_worktree as _cw  # type: ignore
            WORKTREES_SEG = _SEG
            _current_worktree = _cw
        except Exception:
            pass
        break


def _fallback_current_worktree(cwd):
    """Inline equivalent of worktree_safety.current_worktree for the no-lib path."""
    marker = "/" + WORKTREES_SEG + "/"
    s = str(cwd)
    if marker not in s:
        return None
    head, tail = s.split(marker, 1)
    slug = tail.split("/", 1)[0]
    if not slug:
        return None
    return Path(head + marker + slug), slug


def current_worktree(cwd):
    fn = _current_worktree or _fallback_current_worktree
    try:
        return fn(Path(cwd).resolve())
    except Exception:
        return None


def is_obsidian_vault(vault: Path) -> bool:
    """The `.obsidian/` dir is what makes the tree Obsidian-watched. Its presence
    is the discriminator between a vault (melts) and a plain code repo (fine)."""
    try:
        return (vault / ".obsidian").is_dir()
    except OSError:
        return False


def is_git_repo(vault: Path) -> bool:
    """True if `vault` is the root of (or inside) a git work tree. Bounded git
    call; a plain `.git` dir/file short-circuits without spawning git."""
    try:
        if (vault / ".git").exists():
            return True
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["git", "-C", str(vault), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=3,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except Exception:
        return False


def count_worktree_checkouts(vault: Path) -> int:
    """Number of worktree checkouts physically present under the vault's
    `.claude/worktrees/`. The physical footprint is what Obsidian indexes, so we
    count directories on disk (registered or orphaned) rather than asking git —
    an orphaned-but-present checkout melts the renderer just the same. Read-only;
    an unreadable listing degrades to 0 (reported as OK upstream)."""
    wt_dir = vault / ".claude" / "worktrees"
    try:
        if not wt_dir.is_dir():
            return 0
        return sum(1 for e in wt_dir.iterdir() if e.is_dir())
    except OSError:
        return 0


def evaluate(vault: Path):
    """Return (token, count). token is the porcelain first field."""
    if not is_obsidian_vault(vault):
        return "OK_NOT_VAULT", 0
    if not is_git_repo(vault):
        return "OK_NOT_GIT", 0

    count = count_worktree_checkouts(vault)

    # cwd channel: this very run is happening from inside a worktree that belongs
    # to THIS vault (e.g. /diagnose launched from the worktree session). Counts as
    # at least one live checkout even if the on-disk scan raced or was unreadable.
    cw = current_worktree(os.getcwd())
    if cw is not None:
        wt_path = cw[0]
        try:
            owner = Path(str(wt_path).split("/" + WORKTREES_SEG + "/", 1)[0]).resolve()
            if owner == vault.resolve() and count == 0:
                count = 1
        except Exception:
            pass

    if count > 0:
        return "WORKTREE_ON_VAULT:{}".format(count), count
    return "OK_NO_WORKTREES", 0


def _print_human(token: str, count: int, vault: Path) -> None:
    if token == "OK_NOT_VAULT":
        print("OK    Not an Obsidian vault (no .obsidian/); the worktree-melt class does not apply.")
    elif token == "OK_NOT_GIT":
        print("OK    Vault is not a git repo; the Desktop worktree feature cannot create a checkout here.")
    elif token == "OK_NO_WORKTREES":
        print("OK    No git worktree inside the Obsidian-watched vault tree.")
    elif token.startswith("WORKTREE_ON_VAULT:"):
        print("WARN  {} git worktree checkout(s) live INSIDE the vault at {}/.claude/worktrees/."
              .format(count, vault))
        print("      The Desktop per-session worktree checkbox dropped a full-vault checkout inside")
        print("      Obsidian's watched tree -> renderer OOM/crash, and the worktree can be silently")
        print("      deleted mid-session. Relocation is DEAD: a symlink out is followed back in by")
        print("      Obsidian's watcher, and a WorktreeCreate redirect is not honored. The flag does")
        print("      NOT gate Desktop worktree creation, so do NOT 'set the flag'.")
        print("      FIX: launch the vault PLAIN with the per-session worktree box UNCHECKED:")
        print("           cd \"{}\" && claude".format(vault))
        print("      See docs/VAULT_WORKTREE_MELT.md.")
    else:
        print("WARN  Could not evaluate worktree-on-vault melt risk ({}).".format(token))


def main(argv):
    porcelain = "--porcelain" in argv
    args = [a for a in argv if a != "--porcelain"]
    if len(args) > 1:
        print(USAGE, file=sys.stderr)
        return 2
    raw = args[0] if args else os.environ.get("VAULT_PATH") or os.getcwd()
    try:
        vault = Path(raw).expanduser().resolve()
    except Exception:
        vault = Path(raw)
    if not vault.is_dir():
        # Fail-open: a bad path is "nothing to flag", not a crash of the report.
        if porcelain:
            print("OK_NO_WORKTREES")
        else:
            print("OK    Vault path not a directory; nothing to evaluate.")
        return 0

    token, count = evaluate(vault)

    if porcelain:
        print(token)
    else:
        _print_human(token, count, vault)
    return 1 if token.startswith("WORKTREE_ON_VAULT:") else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        # Fail-open: a diagnostic must never crash the report it runs inside.
        try:
            if "--porcelain" in sys.argv:
                print("OK_NO_WORKTREES")
        except Exception:
            pass
        sys.exit(0)
