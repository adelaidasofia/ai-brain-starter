"""Detect when the Agent Memory index cannot be fully loaded.

The memory index (MEMORY.md) is read into session context. Past a byte cliff the
reader silently stops, so entries below the cut are never seen — no error, no
warning, just a brain that quietly forgot part of itself. Write-time tooling
warns when the index GROWS; a session that only READS it gets a truncated list
and no signal at all. An index is proven by what LOADS, not by what was written
to it.

The invariant: **an indexed entry is either loaded, or its omission is
announced.** Never silently dropped.

Failure modes, all announced:

  OVER-CLIFF    an index file is larger than the readable budget, so some of its
                entries are already unread. Checked for tier 1 AND every tier-2
                file, because "split it into tier 2" is useless advice if the
                tier you move entries into is never measured.
  UNREACHABLE   a memory file exists that no index references.
  ORPHAN-INDEX  a tier-2 index that tier 1 does not link to. Nothing loads it,
                so the memories it exclusively lists are invisible even though
                a file on disk names them.
  UNREADABLE    a tier-2 index that cannot be read. Announced as itself — a
                failed read is not an empty answer, and must never be silently
                converted into "everything it listed is missing".

Two-tier aware. A corpus of ~1000 memories needs roughly 57KB of link markup
alone, several times the cliff, so a single flat index physically cannot list
everything. The supported shape is a capped tier-1 MEMORY.md plus `_index_*.md`
tier-2 files **that tier 1 links to**. A memory named by either tier is
reachable.

Reachability is deliberately generous about SYNTAX and strict about REACH: a
memory counts as indexed if its filename appears anywhere in a loaded index,
link or not. Markdown has many link spellings (`](./x.md)`, `](<x.md>)`,
`](x.md "t")`, `](x.md#a)`, reference-style, backticked bare names) and a
regex that recognises only the canonical one reports correctly-indexed
memories as missing. A false "your memory is invisible" is worse than counting
a filename mentioned in prose, which a reader can in fact find.

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

# The reader stops loading PAST this many bytes. Entries below the cut are
# silently unread, which is the whole bug. Band is (WARN, CLIFF].
READ_CLIFF_BYTES = 24_400

# Report before the cliff, not at it: at the cliff, entries are ALREADY lost.
WARN_BYTES = 17_000

# Cap the names listed so a vault with hundreds of unreachable memories still
# emits a readable message rather than a wall of text.
MAX_NAMED = 12

# Each finding carries the remediation that fixes IT, so the closing advice
# names only what actually fired. A blanket "trim the oversized index" printed
# for an index well under budget sends the reader to do work that is not needed
# — and an announcement that prescribes the wrong fix teaches them to discount
# the whole thing, which is exactly the silence this module exists to prevent.
FIX_SIZE = ("split the oversized index by topic into `_index_*.md` files "
            "linked from MEMORY.md")
FIX_UNREADABLE = ("fix the unreadable tier-2 index file(s) before trusting any "
                  "reachability result")
FIX_ORPHAN = "link the orphaned `_index_*.md` file(s) from MEMORY.md"
FIX_UNREACHABLE = ("add the unindexed memory file(s) to MEMORY.md or to a "
                   "linked `_index_*.md`")

# Structural link forms, used to find which tier-2 files tier 1 actually links
# to. Tolerates `<...>`, a title, an anchor and a path prefix.
LINK_RE = re.compile(r"\]\(\s*<?([^)>\s]+?\.md)(?:#[^)>\s]*)?>?(?:\s+[^)]*)?\)")


def _dedup_dirs(dirs):
    """Collapse aliases of the same underlying directory.

    Project-key directories are routinely symlinked to one shared memory dir.
    Measured on a real machine: 212 glob hits, 5 distinct inodes, 208 of them
    aliasing a single directory. Without this, the audit runs once per alias —
    a full second of stat/read per session start and 208 copies of every
    reported line injected into context. The guard warning about an oversized
    index must not itself be the oversized injection.
    """
    seen = set()
    out = []
    for d in dirs:
        try:
            st = d.stat()
            key = (st.st_dev, st.st_ino)
        except OSError:
            key = (None, str(d))
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def memory_dirs():
    """Candidate Agent Memory directories, de-aliased.

    An override wins outright so tests and non-default layouts never depend on
    guessing.
    """
    override = os.environ.get("AGENT_MEMORY_DIR")
    if override:
        return [Path(override)]
    found = [
        d for d in (Path.home() / ".claude" / "projects").glob("*/memory")
        if d.is_dir()
    ]
    return _dedup_dirs(found)


def _topic_files(mdir: Path):
    return sorted(
        p for p in mdir.glob("*.md")
        if p.name != "MEMORY.md" and not p.name.startswith("_index_")
    )


def _linked_tier2(mdir: Path, tier1_text: str):
    """Tier-2 index files that tier 1 actually links to, plus the orphans.

    An `_index_*.md` nothing links to is never loaded, so trusting it would let
    it mask a genuinely unreachable memory — the exact silent drop this module
    exists to prevent.
    """
    present = sorted(p for p in mdir.glob("_index_*.md") if p.is_file())
    referenced = {os.path.basename(m) for m in LINK_RE.findall(tier1_text)}
    linked, orphans = [], []
    for p in present:
        (linked if (p.name in referenced or p.name in tier1_text) else orphans).append(p)
    return linked, orphans


def audit(mdir: Path):
    """Problems for one memory dir, as text. Empty list = healthy."""
    return [text for text, _fix in _audit_tagged(mdir)]


def _audit_tagged(mdir: Path):
    """(problem text, remediation) pairs for one memory dir.

    The remediation travels WITH the finding so the caller can close on the
    fixes that actually fired instead of reciting every fix this module knows.
    """
    tier1 = mdir / "MEMORY.md"
    if not tier1.exists():
        return []
    try:
        text = tier1.read_text(encoding="utf-8", errors="replace")
        size = tier1.stat().st_size
    except OSError:
        return []

    problems = []
    problems.extend((t, FIX_SIZE) for t in _size_problems("MEMORY.md", size))

    linked, orphans = _linked_tier2(mdir, text)

    # Names reachable from tier 1 plus every LINKED tier-2 file. Filename
    # containment, not link syntax — see the module docstring.
    loaded_text = [text]
    unreadable = []
    for sub in linked:
        try:
            loaded_text.append(sub.read_text(encoding="utf-8", errors="replace"))
            problems.extend(
                (t, FIX_SIZE) for t in _size_problems(sub.name, sub.stat().st_size))
        except OSError as exc:
            unreadable.append("{} ({})".format(sub.name, exc.__class__.__name__))
    haystack = "\n".join(loaded_text)

    if unreadable:
        problems.append((
            "{} tier-2 index file(s) could not be READ, so what they list is "
            "unknown, not absent: {}. Fix the read before trusting any "
            "reachability result below.".format(len(unreadable), ", ".join(unreadable)),
            FIX_UNREADABLE,
        ))

    if orphans:
        problems.append((
            "{} tier-2 index file(s) are not linked from MEMORY.md, so nothing "
            "loads them and any memory only they list is invisible: {}.".format(
                len(orphans), ", ".join(p.name for p in orphans)),
            FIX_ORPHAN,
        ))

    unreachable = [p.name for p in _topic_files(mdir) if p.name not in haystack]
    if unreachable:
        shown = ", ".join(unreachable[:MAX_NAMED])
        more = (" and {} more".format(len(unreachable) - MAX_NAMED)
                if len(unreachable) > MAX_NAMED else "")
        tier_note = (
            " (checked MEMORY.md plus {} linked tier-2 index file(s))".format(len(linked))
            if linked
            else " (no linked tier-2 `_index_*.md` files; a large corpus needs them)"
        )
        problems.append((
            "{} memory file(s) are referenced by NO index{} — invisible to this "
            "and every future session: {}{}.".format(
                len(unreachable), tier_note, shown, more),
            FIX_UNREACHABLE,
        ))
    return problems


def _size_problems(label: str, size: int):
    """Size verdict for one index file. Band is (WARN, CLIFF]."""
    if size > READ_CLIFF_BYTES:
        return ["{} is {:,} bytes, past the {:,}-byte read cliff by {:,}. Entries "
                "past the cut are NOT being loaded — silently missing, not merely "
                "at risk.".format(label, size, READ_CLIFF_BYTES, size - READ_CLIFF_BYTES)]
    if size > WARN_BYTES:
        return ["{} is {:,} bytes, over the {:,}-byte budget (cliff {:,}). Still "
                "fully loaded, but the next few additions start dropping "
                "entries.".format(label, size, WARN_BYTES, READ_CLIFF_BYTES)]
    return []


def report() -> str:
    """Human-readable announcement, or '' when everything loads."""
    if os.environ.get("MEMORY_INDEX_TRUNCATION_BYPASS") == "1":
        return ""
    lines = []
    fixes = []  # fired remediations, first-seen order, deduped across dirs
    for mdir in memory_dirs():
        try:
            problems = _audit_tagged(mdir)
        except Exception:
            continue  # one bad dir must not suppress the others
        label = mdir.parent.name or str(mdir)
        for text, fix in problems:
            lines.append("  [{}] {}".format(label, text))
            if fix not in fixes:
                fixes.append(fix)
    if not lines:
        return ""
    return (
        "**[memory-index]** Something in the memory system will not reach a "
        "future session. An indexed entry is either loaded or its omission is "
        "announced — this is the announcement.\n\n" + "\n".join(lines) + "\n\n"
        "Fix: " + "; ".join(fixes) + " — so nothing is dropped in silence. "
        "Bypass: MEMORY_INDEX_TRUNCATION_BYPASS=1"
    )
