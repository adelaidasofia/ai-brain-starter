"""Detect when the Agent Memory index cannot be fully loaded.

The memory index (MEMORY.md) is read into session context. Past a byte cliff the
reader silently stops, so entries below the cut are never seen — no error, no
warning, just a brain that quietly forgot part of itself. Write-time tooling
warns when the index GROWS; a session that only READS it gets a truncated list
and no signal at all. An index is proven by what LOADS, not by what was written
to it.

The invariant: **an indexed entry is either loaded, or its omission is
announced.** Never silently dropped.

Two failure modes:

  OVER-CLIFF   the index is larger than the readable budget, so some entries are
               already unread this session.
  UNREACHABLE  a memory file exists that no index references, so it is invisible
               regardless of size.

Two-tier aware. A corpus of ~1000 memories needs roughly 57KB of link markup
alone, several times the cliff, so a single flat index physically cannot list
everything. The supported shape is a capped tier-1 MEMORY.md plus `_index_*.md`
tier-2 files it links to. A memory indexed in EITHER tier is reachable; checking
tier 1 alone would report almost every memory in a split vault as missing.

Consumed by `hooks/session-start-context.py` — the loader announces what it
could not load. It lives here rather than in its own SessionStart hook because
that event is at its cold-start fan-out budget, and a housekeeping check does
not earn a new subprocess on every session for the life of the install.

Read-only: never edits an index or a memory file.
Bypass: MEMORY_INDEX_TRUNCATION_BYPASS=1
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# The reader stops loading past this many bytes. Entries below the cut are
# silently unread, which is the whole bug.
READ_CLIFF_BYTES = 24_400

# Report before the cliff, not at it: at the cliff, entries are ALREADY lost.
WARN_BYTES = 17_000

# Cap the names listed so a vault with hundreds of unreachable memories still
# emits a readable message rather than a wall of text.
MAX_NAMED = 12

LINK_RE = re.compile(r"\]\(([^)]+?\.md)\)")


def memory_dirs() -> list:
    """Candidate Agent Memory directories.

    An override wins outright so tests and non-default layouts never depend on
    guessing. A symlinked directory resolves fine because glob follows it.
    """
    override = os.environ.get("AGENT_MEMORY_DIR")
    if override:
        return [Path(override)]
    return [
        d for d in (Path.home() / ".claude" / "projects").glob("*/memory")
        if d.is_dir()
    ]


def _topic_files(mdir: Path) -> list:
    return sorted(
        p for p in mdir.glob("*.md")
        if p.name != "MEMORY.md" and not p.name.startswith("_index_")
    )


def _reachable(mdir: Path, tier1_text: str):
    """Names reachable from tier 1 plus every tier-2 file, and the tier-2 count."""
    names = set(LINK_RE.findall(tier1_text))
    tier2 = sorted(p for p in mdir.glob("_index_*.md") if p.is_file())
    for sub in tier2:
        try:
            names |= set(LINK_RE.findall(sub.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return names, len(tier2)


def audit(mdir: Path) -> list:
    """Problems for one memory dir. Empty list = healthy."""
    tier1 = mdir / "MEMORY.md"
    if not tier1.exists():
        return []
    try:
        size = tier1.stat().st_size
        text = tier1.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    problems = []
    if size > READ_CLIFF_BYTES:
        problems.append(
            "MEMORY.md is {:,} bytes, past the {:,}-byte read cliff by {:,}. "
            "Entries past the cut are NOT being loaded — silently missing, not "
            "merely at risk.".format(size, READ_CLIFF_BYTES, size - READ_CLIFF_BYTES)
        )
    elif size > WARN_BYTES:
        problems.append(
            "MEMORY.md is {:,} bytes, over the {:,}-byte budget (cliff {:,}). "
            "Still fully loaded, but the next few additions start dropping "
            "entries.".format(size, WARN_BYTES, READ_CLIFF_BYTES)
        )

    reachable, tier2_count = _reachable(mdir, text)
    unreachable = [p.name for p in _topic_files(mdir) if p.name not in reachable]
    if unreachable:
        shown = ", ".join(unreachable[:MAX_NAMED])
        more = (" and {} more".format(len(unreachable) - MAX_NAMED)
                if len(unreachable) > MAX_NAMED else "")
        tier_note = (
            " (checked MEMORY.md plus {} tier-2 index file(s))".format(tier2_count)
            if tier2_count
            else " (no tier-2 `_index_*.md` files present; a large corpus needs them)"
        )
        problems.append(
            "{} memory file(s) are referenced by NO index{} — invisible to this "
            "and every future session: {}{}.".format(
                len(unreachable), tier_note, shown, more)
        )
    return problems


def report() -> str:
    """Human-readable announcement, or '' when everything loads."""
    if os.environ.get("MEMORY_INDEX_TRUNCATION_BYPASS") == "1":
        return ""
    lines = []
    for mdir in memory_dirs():
        try:
            problems = audit(mdir)
        except Exception:
            continue  # one bad dir must not suppress the others
        label = mdir.parent.name or str(mdir)
        for p in problems:
            lines.append("  [{}] {}".format(label, p))
    if not lines:
        return ""
    return (
        "**[memory-index]** The memory index cannot be fully loaded. An indexed "
        "entry is either loaded or its omission is announced — this is the "
        "announcement.\n\n" + "\n".join(lines) + "\n\n"
        "Trim the always-loaded index, or split it into a capped MEMORY.md plus "
        "`_index_*.md` tier-2 files it links to, so nothing is dropped in silence. "
        "Bypass: MEMORY_INDEX_TRUNCATION_BYPASS=1"
    )
