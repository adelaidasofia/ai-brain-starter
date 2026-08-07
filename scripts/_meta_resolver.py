"""Shared Meta folder resolver for vault scripts.

Background:
    Vaults can have two folders ending in "Meta":
        - "⚙️ Meta"  human-readable rules + decisions (Decisions/, RESOLVER.md, etc.)
        - "Meta"     closed-loop machine memory (Learnings/, Promotion-Candidates/)
    Naive `for child in sorted(vault_root.iterdir()): if child.name.endswith("Meta")`
    picks plain "Meta" first because "M" sorts before emoji codepoints. Scripts
    that walk Decisions/ then silently return zero rules.

Fix:
    Each caller passes the subfolder it actually reads from. The resolver picks
    whichever Meta variant contains that subfolder. Falls back to first Meta
    found if neither variant has it (single-Meta vaults still work).

Usage:
    from _meta_resolver import find_meta_dir
    meta = find_meta_dir(vault_root)                            # default: Decisions
    meta = find_meta_dir(vault_root, ("graphify-out",))         # graphify scripts
    meta = find_meta_dir(vault_root, ("Workflows", "Exceptions", "Facts", "Decisions"))

Don't use this in scripts that intentionally want plain Meta/ (e.g.
promote-episodic-to-procedural reads Learnings/ which lives in plain Meta).
"""

from __future__ import annotations

import sys
from pathlib import Path


def find_meta_dir(
    vault_root: Path,
    prefer_subfolders: tuple[str, ...] = ("Decisions",),
) -> Path | None:
    """Auto-detect the Meta folder, preferring the variant containing
    `prefer_subfolders`.

    Returns the matching Meta path, or the first Meta-suffixed dir as fallback,
    or None if none exist.
    """
    if not vault_root.is_dir():
        return None
    candidates = [
        c for c in sorted(vault_root.iterdir())
        if c.is_dir() and c.name.endswith("Meta")
    ]
    for c in candidates:
        if any((c / sub).exists() for sub in prefer_subfolders):
            return c
    return candidates[0] if candidates else None


def _cli(argv: list[str]) -> int:
    """CLI shim so shell scripts share this ONE resolver instead of
    re-implementing the glob (which sorts plain "Meta" before "⚙️ Meta").

        meta=$(python3 _meta_resolver.py "$VAULT" Sessions Decisions) || meta="$VAULT/Meta"

    Prints the resolved Meta dir and exits 0; exits 1 with no output when the
    vault has no Meta-suffixed folder at all (caller applies its own fallback);
    exits 2 on a usage error.
    """
    # The resolved path usually contains the "⚙️ Meta" emoji. On a Windows
    # cp1252 console (or a C-locale pipe) print() raises UnicodeEncodeError, and
    # the caller then sees an empty result and misreads it as "no Meta folder".
    # Force UTF-8 on the CLI streams so the path always round-trips; PowerShell
    # and POSIX shell callers decode UTF-8 on their side.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    if not argv:
        print("usage: _meta_resolver.py VAULT_ROOT [SUBFOLDER ...]", file=sys.stderr)
        return 2
    prefer = tuple(argv[1:]) or ("Decisions",)
    resolved = find_meta_dir(Path(argv[0]), prefer)
    if resolved is None:
        return 1
    print(resolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
