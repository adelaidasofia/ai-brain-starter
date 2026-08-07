"""vault_root — single source of truth for session-close vault-root resolution.

Used by every consumer in the session-close cascade:
  - hooks/detect-closing-signal.py            (Layer 1, UserPromptSubmit)
  - hooks/verify-session-close-cascade.py     (Layer 3, Stop)
  - hooks/verify-discoverability-on-close.py  (Layer 3, Stop)

Each of these previously computed its own vault root independently from
`os.environ.get("VAULT_ROOT", ...)`, with no awareness of cwd or which repo
the session was actually working in.

Bug this fixes: a machine-wide VAULT_ROOT default (set once, e.g. in Claude
Code's settings.json `env` block, so every hook subprocess always sees it
set) permanently wins over cwd — `os.environ.get("VAULT_ROOT") or cwd` never
reaches the `or cwd` branch once VAULT_ROOT is set globally. A session
working inside a SEPARATE vault-shaped repo (its own CLAUDE.md, its own
Session End/Close cascade, its own Sessions/Decisions folders — e.g. a
standalone vault repo, or a team folder with its own cascade) had its
session artifacts silently resolved against the unrelated default vault
instead, and any Stop-hook verifier checked the wrong vault for the
artifacts Layer 1 told the model to write — a fix to Layer 1 alone would
have turned that silent mis-filing into a false hard-block, since the
verifiers would look for files that now correctly exist somewhere else.

resolve_vault_root() restores cwd-derived detection as the HIGHER-priority
signal: it walks up from cwd looking for the nearest self-contained vault
(a CLAUDE.md that declares its own Session End/Close cascade + an existing
Meta folder) and uses that when found. The VAULT_ROOT env var / cwd fallback
is now exactly that — a fallback for when no such repo is found (untracked
locations, or a session already rooted in the default vault itself, which
never needs the walk-up to win since it's reached via the fallback anyway).

TWO RESOLVERS, TWO SURFACES (MYC-3529)
    resolve_vault_root(cwd, env)  — SESSION-scoped. "Which vault does THIS
        session's close cascade write to?" Keyed on a CLAUDE.md that declares
        its own Session End/Close cascade, because that is the thing being
        asked about. Always returns a Path (cwd is the floor).

    vault_root_for(target)        — PER-TARGET. "Which vault governs THIS
        file / directory?" Keyed on a Meta-suffixed folder, the same signature
        scripts/_meta_resolver.py uses, because a hook fires on paths in ANY
        vault and most of them never declare a close cascade. Returns None
        when no vault can be identified, so a caller SKIPS rather than
        guessing at `~/vault` — the #375/#404 defect shape.

    The distinction is the whole point of MYC-2505's severity tagging: a
    SCRIPT serves one vault, a SESSION has one vault, but a HOOK fires on
    files in any vault and must resolve per target or it silently answers for
    the wrong one. `$VAULT_ROOT` is the FALLBACK in both, never the primary.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = [
    "collapse_worktree",
    "find_meta_vault_root",
    "find_repo_vault_root",
    "resolve_vault_root",
    "vault_root_for",
]

# Matches "## Session End", "## Session end", "# Session Close", etc. Deliberately
# does NOT match "Session Protocol" or other session-adjacent headings — a vault
# can discuss sessions without declaring itself the owner of a close cascade for
# THIS purpose, and headings like that are exactly how the default/fallback
# vault stays reachable only through the fallback path, never a false walk-up win.
_SESSION_HEADING_RE = re.compile(r"^#+\s*Session\s+(?:End|Close)\b", re.IGNORECASE | re.MULTILINE)

# Defensive bound on the walk-up, independent of the $HOME/filesystem-root
# stop conditions below — cheap insurance against an unexpected filesystem
# structure (symlink loop, cwd reported outside $HOME) turning a hook that
# has a documented <500ms budget into an unbounded stat loop.
_MAX_WALKUP_LEVELS = 25

_WORKTREE_MARKER = "/.claude/worktrees/"


def collapse_worktree(path: Path) -> Path:
    """Collapse a `<vault>/.claude/worktrees/<slug>/...` path to `<vault>`.

    A worktree checkout carries the full tree, including CLAUDE.md — so an
    unqualified walk-up from inside one would stop AT the worktree and
    strand session artifacts on its throwaway `claude/<slug>` branch,
    exactly the bug find_repo_vault_root must not reintroduce. Always
    collapse before searching.
    """
    # Normalize separators so the marker matches Windows paths too.
    text = str(path).replace("\\", "/")
    if _WORKTREE_MARKER in text:
        return Path(text.split(_WORKTREE_MARKER, 1)[0])
    return path


def _has_meta_dir(candidate: Path) -> bool:
    """True iff `candidate` has a "⚙️ Meta" or Meta-suffixed directory.

    Read-only existence probe (decorated name checked first, matching the
    convention every consumer's own meta-dir resolver already uses, so a
    machine-memory plain "Meta" can't shadow "⚙️ Meta" here either) — this
    does not create anything; it only decides whether `candidate` looks
    like an already-established vault worth trusting.
    """
    for name in ("⚙️ Meta", "Meta"):
        if (candidate / name).is_dir():
            return True
    try:
        return any(
            child.is_dir() and child.name.endswith("Meta")
            for child in candidate.iterdir()
        )
    except OSError:
        return False


def _declares_own_session_close_cascade(candidate: Path) -> bool:
    """True iff `candidate` is a self-contained vault for session-close purposes.

    Both signals required:
      - a CLAUDE.md documenting its OWN "Session End"/"Session Close" cascade
        (heading match, case-insensitive), AND
      - an existing Meta folder to write into.
    Either alone is too weak: a CLAUDE.md can mention sessions without owning
    a cascade for this purpose (a default vault's own CLAUDE.md may use a
    different heading — e.g. "Session Protocol" — precisely so it keeps
    reaching itself through the fallback, not a walk-up match); a Meta
    folder can exist without any CLAUDE.md ever declaring it canonical.
    """
    claude_md = candidate / "CLAUDE.md"
    if not claude_md.is_file():
        return False
    try:
        text = claude_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not _SESSION_HEADING_RE.search(text):
        return False
    return _has_meta_dir(candidate)


def find_repo_vault_root(start: Path) -> Path | None:
    """Walk up from `start` (inclusive) for the nearest self-contained vault.

    Stops at $HOME (checked, then not exceeded) or the filesystem root,
    whichever comes first, bounded defensively at _MAX_WALKUP_LEVELS.
    Returns None when nothing matches — callers fall back to their
    pre-existing default. This function only ever ADDS a higher-priority
    match; it never removes the old behavior.
    """
    home = Path.home()
    current = start
    for _ in range(_MAX_WALKUP_LEVELS):
        if _declares_own_session_close_cascade(current):
            return current
        if current == home:
            break
        parent = current.parent
        if parent == current:  # reached filesystem root
            break
        current = parent
    return None


def find_meta_vault_root(start: Path) -> Path | None:
    """Nearest ancestor of `start` (inclusive) holding a Meta-suffixed folder.

    This is the PER-TARGET vault signature, and it is deliberately weaker than
    _declares_own_session_close_cascade: a hook fires on files in any vault,
    and most vaults never declare a close cascade, so keying per-file detection
    on that heading would leave the hook inert on exactly the vaults it exists
    to cover. A Meta-suffixed folder is the same signature
    scripts/_meta_resolver.py and hooks/validate-handoff-frontmatter.py already
    key on, so all three agree on what "a vault" means.

    `start` may be a FILE: iterdir() on a non-directory raises OSError, which
    is caught, so the walk simply continues at the parent. That makes one
    function correct for both a target file and a target directory (a cwd),
    which is what the hook surface actually needs.

    Bounded by _MAX_WALKUP_LEVELS and stopped at the filesystem root, for the
    same reason find_repo_vault_root is: a hook has a sub-500ms budget and must
    not turn into an unbounded stat loop on an odd filesystem. Deliberately NOT
    stopped at $HOME — a vault can live outside the home directory (an external
    drive, D:\\ on Windows), and unlike the session-close walk there is no
    default-vault fallback that would otherwise reach it.
    """
    try:
        current = start.resolve()
    except (OSError, RuntimeError):
        return None
    for _ in range(_MAX_WALKUP_LEVELS):
        try:
            if any(c.is_dir() and c.name.endswith("Meta") for c in current.iterdir()):
                return current
        except (OSError, ValueError):
            pass
        parent = current.parent
        if parent == current:  # reached filesystem root
            break
        current = parent
    return None


def vault_root_for(target: Path) -> Path | None:
    """Vault root governing `target`: detected FROM the target, else $VAULT_ROOT.

    The per-target resolver for the hook surface, extracted from
    hooks/validate-handoff-frontmatter.py (the #375/#404 fix) so the other
    hooks in that class share one implementation instead of twelve.

    Detection comes FIRST on purpose. $VAULT_ROOT names ONE vault, but a
    machine routinely has several (a personal vault plus one per project) and
    the variable is routinely exported machine-wide. If the env var won, a hook
    firing on a path in any OTHER vault would resolve to a root that path does
    not sit under, its containment check would return False, and it would
    silently SKIP the very file it exists to check — failing open with no
    signal. Per-target detection is correct for all of them.

    Worktree paths collapse first: `<vault>/.claude/worktrees/<slug>/...` is
    governed by `<vault>`, not by the throwaway checkout, which carries a full
    copy of the tree (Meta folder included) and would otherwise win the walk-up.

    Returns None when no vault can be identified at all. Callers must treat
    that as "no vault here, do nothing" — NOT as a reason to fall back to
    `Path.home() / "vault"`, which is the exact default that made this whole
    class of guard inert on every vault not literally named "vault".
    """
    found = find_meta_vault_root(collapse_worktree(target))
    if found is not None:
        return found
    env = (os.environ.get("VAULT_ROOT") or "").strip()
    if env:
        try:
            return collapse_worktree(Path(env).expanduser()).resolve()
        except (OSError, RuntimeError):
            return None
    return None


def resolve_vault_root(cwd: Path, env_vault_root: str | None) -> Path:
    """Single source of truth: which vault does THIS session's close cascade write to?

    Priority:
      1. The nearest ancestor of `cwd` (worktree-collapsed) that declares its
         own Session End/Close cascade — even when a global VAULT_ROOT
         default is configured. This is what makes a session rooted in its
         own vault-shaped repo resolve to itself instead of an unrelated
         default vault.
      2. VAULT_ROOT env var, if set.
      3. cwd itself (worktree-collapsed) — today's behavior when no env
         override exists at all.
    """
    base = collapse_worktree(cwd)
    repo_match = find_repo_vault_root(base)
    if repo_match is not None:
        return repo_match
    return collapse_worktree(Path(env_vault_root) if env_vault_root else cwd)
