#!/usr/bin/env python3
"""Single source of truth: vault path -> Claude Code per-project memory dir.

Claude Code keys per-project auto-memory under
``~/.claude/projects/<encoded-path>/memory/``. The encoding replaces every
character that is not ``[A-Za-z0-9-]`` with ``-`` (hyphens and alphanumerics
survive; ``/``, ``.``, spaces, and emoji codepoints each become a dash, with NO
collapsing of consecutive dashes). Verified against real keys:

    /Users/me/Brain         -> -Users-me-Brain
    /Users/me/Brain/⚙️ Meta  -> -Users-me-Brain----Meta

Two substrate scripts previously hand-rolled this and drifted
(``check-rule-conflicts.py`` replaced ``.`` too; ``hallucination-watch.py`` did
not). They now both import from here so the key they compute is the key the
linker creates the symlink at — otherwise the fix silently no-ops at the wrong
key (SILENT-NO-OP-ON-WRONG-KEY).

Importable + runnable:

    python3 scripts/_project_key.py "/path/to/vault"   # prints the encoded key
"""

from __future__ import annotations

import sys
import os
import re
from pathlib import Path

# Anything that is not an ASCII letter, digit, or hyphen becomes a dash.
# Hyphens in the original path are preserved (e.g. a username like "ada-lovelace").
_NON_KEY_CHAR = re.compile(r"[^A-Za-z0-9-]")


def encode_project_key(path: str | os.PathLike) -> str:
    """Encode an absolute filesystem path to Claude Code's project-dir key.

    Mirrors Claude Code's own sanitization: abspath, then every non
    ``[A-Za-z0-9-]`` char -> ``-`` (no dash collapsing). The leading ``/`` of an
    absolute path therefore becomes the leading ``-``.
    """
    abs_path = os.path.abspath(os.path.expanduser(str(path)))
    # abspath strips a trailing slash already, except for the filesystem root.
    return _NON_KEY_CHAR.sub("-", abs_path)


def claude_projects_root() -> Path:
    """``~/.claude/projects`` — overridable via CLAUDE_HOME for tests."""
    claude_home = os.environ.get("CLAUDE_HOME")
    base = Path(claude_home) if claude_home else (Path.home() / ".claude")
    return base / "projects"


def project_dir_for(vault: str | os.PathLike) -> Path:
    """Return the ``~/.claude/projects/<key>`` dir for a vault.

    Prefers the exact-encoding match. If that exact dir is absent, falls back to
    a glob over existing project dirs whose decoded name equals the vault path
    (defends against any future encoding nuance). When nothing exists yet — the
    normal case the first time Claude Code is run from a brand-new vault — it
    returns the exact-encoding path so the caller can create it.

    Raises ``RuntimeError`` only on a genuinely ambiguous match (two existing
    project dirs that both decode to this vault), which should never happen and
    must fail loud rather than guess.
    """
    vault_abs = os.path.abspath(os.path.expanduser(str(vault)))
    key = encode_project_key(vault_abs)
    root = claude_projects_root()
    exact = root / key
    if exact.is_dir():
        return exact

    if root.is_dir():
        # Glob fallback: a project dir whose own name re-encodes from the same
        # vault. We can't decode a key back to a path unambiguously (dashes are
        # lossy), so match on equality of the re-encoded basename instead.
        matches = [p for p in root.iterdir() if p.is_dir() and p.name == key]
        if len(matches) > 1:
            raise RuntimeError(
                f"ambiguous project dir for vault {vault_abs!r}: {[m.name for m in matches]}"
            )
        if matches:
            return matches[0]

    # Nothing on disk yet — return the canonical path for the caller to mkdir.
    return exact


def memory_dir_for(vault: str | os.PathLike, *, must_exist: bool = False) -> Path | None:
    """Return ``<project_dir>/memory`` for a vault.

    With ``must_exist=True`` returns ``None`` when the dir (following symlinks)
    is absent, so read-only consumers can cleanly skip a vault that has no
    memory yet.
    """
    mem = project_dir_for(vault) / "memory"
    if must_exist and not mem.is_dir():  # is_dir follows symlinks
        return None
    return mem


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    import sys

    if len(sys.argv) != 2:
        print("usage: _project_key.py <vault-path>", file=sys.stderr)
        sys.exit(2)
    print(encode_project_key(sys.argv[1]))
