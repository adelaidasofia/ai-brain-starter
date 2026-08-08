#!/usr/bin/env python3
"""Build a date index of all journal entries for fast lookup by the insights skill.

Reads YAML frontmatter from every .md file in your journal directory and emits
a sorted JSON index of {file, date, floor, floor_level, floor_arc} entries. The insights
skill (and any /weekly or /monthly review) uses this index to scan thousands
of journal entries in milliseconds without re-reading every file.

Run weekly via cron, or manually after a journaling session.

Usage:
    python3 build-journal-index.py [--vault-root .] [--journal-dir Journals]

The output is written to:
    <vault-root>/<meta-dir>/journal-index.json

The Meta folder (where the index is written) is auto-detected via the shared
_meta_resolver, so both "⚙️ Meta" and plain "Meta" layouts work. It must already
exist — the script fails loud rather than creating a stray folder. Layout:
    vault-root/
      Journals/                ← --journal-dir
        2026-04-11.md
        2026-04-10.md
      ⚙️ Meta/ (or Meta/)       ← auto-detected; where the index is written

Expected frontmatter fields (only `creationDate` is required):
    ---
    creationDate: 2026-04-11
    floor: Courage
    floor_level: middle
    floor_arc: [Fear, Frustration, Courage]   # optional — only on a day that moved
    ---

`floor_arc` (when present) is the ordered path of floors the entry moved
through, last element == the primary `floor`. It is indexed as a real list so
the insights movement report can read within-entry transitions (elevators).
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from _meta_resolver import find_meta_dir  # noqa: E402
from _lib.safe_read import safe_read_text  # noqa: E402

# Read bounds for the shared safe_read primitive. Journal frontmatter sits in
# the first lines; safe_read hands back the whole (size-capped) file. 1 MB is
# far beyond any real single-day entry; anything larger is surfaced as skipped,
# never silently dropped.
READ_TIMEOUT = 5.0
MAX_JOURNAL_BYTES = 1_000_000


def _parse_inline_list(v):
    """Parse a simple inline YAML flow list "[a, b, c]" -> ["a", "b", "c"].

    The frontmatter reader below is line-based (stdlib-only, no YAML dep), so a
    `floor_arc: [Fear, Frustration, Hope]` line arrives here as the raw string
    "[Fear, Frustration, Hope]". Return the parsed list so the index carries a
    real array the insights skill can iterate; return the value unchanged if it
    is not an inline list.
    """
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
    return v


JOURNAL_DIR_CANDIDATES = (
    "📓 Journals", "Journals",       # en (Phase 3 default)
    "📔 Journal", "Journal",
    "📓 Diario", "Diario",           # es
    "📓 Diário", "Diário",           # pt
)


def find_journal_dir(vault):
    """Auto-detect the journal folder, mirroring what find_meta_dir does for Meta.

    The --journal-dir default was the hardcoded English "Journals", but Phase 3
    creates a LOCALIZED folder on a non-English install ("📓 Diario" on es), and
    insights/SKILL.md invokes this script with NO arguments — so that default was
    the only thing ever consulted. Result: /weekly and /monthly died with
    "journal directory not found" on every non-English vault.

    Returns None when nothing matches, so the caller still fails loud rather
    than inventing a folder (same contract as the Meta resolver).

    NOTE: no `str | None` return annotation — insights/SKILL.md runs this with
    /usr/bin/python3, which is 3.9 on macOS, and this module has no
    `from __future__ import annotations`, so a PEP 604 union would crash at
    import time. (gate (a) of scripts/ci.sh guards this class.)
    """
    for name in JOURNAL_DIR_CANDIDATES:
        p = os.path.join(vault, name)
        if os.path.isdir(p):
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault-root", default=".",
                    help="Vault root directory (default: current working directory)")
    ap.add_argument("--journal-dir", default=None,
                    help="Journal subfolder relative to vault root. Default: auto-detect, "
                         "handling localized names ('📓 Journals', '📓 Diario', '📓 Diário'...).")
    ap.add_argument("--meta-dir", default=None,
                    help="Meta subfolder where the index is written. Default: auto-detect the "
                         "vault's Meta folder (handles '⚙️ Meta' and plain 'Meta'). The folder "
                         "must already exist; this script never creates it.")
    args = ap.parse_args()

    vault = os.path.abspath(args.vault_root)

    if args.journal_dir is not None:
        journal_dir = os.path.join(vault, args.journal_dir)
    else:
        journal_dir = find_journal_dir(vault)
        if journal_dir is None:
            print(
                f"journal directory not found under {vault} "
                f"(tried: {', '.join(JOURNAL_DIR_CANDIDATES)}). "
                f"Pass --journal-dir if yours is named differently.",
                file=sys.stderr,
            )
            sys.exit(1)

    if not os.path.isdir(journal_dir):
        print(f"journal directory not found: {journal_dir}", file=sys.stderr)
        sys.exit(1)

    # Resolve the Meta folder where the index is written. It must ALREADY exist —
    # never create it. A wrong --meta-dir (or the old hardcoded "Meta" default on an
    # emoji-prefixed "⚙️ Meta" vault) used to silently makedirs a stray folder and
    # let the real journal-index.json go stale. Fail loud instead.
    if args.meta_dir is not None:
        meta_dir = os.path.join(vault, args.meta_dir)
    else:
        resolved = find_meta_dir(Path(vault))
        meta_dir = str(resolved) if resolved is not None else ""

    if not meta_dir or not os.path.isdir(meta_dir):
        target = meta_dir or f"a 'Meta' or '⚙️ Meta' folder under {vault}"
        print(f"meta directory not found: {target}\n"
              f"Refusing to create it (silent creation is the stray-folder bug). Create the "
              f"Meta folder, or pass --meta-dir <name> for a non-standard layout.",
              file=sys.stderr)
        sys.exit(1)

    output_path = os.path.join(meta_dir, "journal-index.json")
    entries = []
    skipped = []

    # Recursive walk: indexes journals nested under year-month subfolders
    # (e.g. Journals/2026-04/2026-04-15.md), not just top-level files.
    for root, _dirs, files in os.walk(journal_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(root, fname)
            # Bounded, symlink-refusing read via the shared safe_read primitive
            # (regular files only, size-capped, timeout-bounded). Frontmatter is
            # in the first lines; safe_read hands back the whole capped file.
            res = safe_read_text(
                fpath, timeout=READ_TIMEOUT, max_bytes=MAX_JOURNAL_BYTES, errors="replace"
            )
            if not res.ok:
                skipped.append((os.path.relpath(fpath, journal_dir), res.status))
                continue
            in_fm = False
            meta = {}
            for i, line in enumerate(res.text.splitlines()):
                if i == 0 and line.strip() == "---":
                    in_fm = True
                    continue
                if in_fm:
                    if line.strip() == "---":
                        break
                    if ": " in line:
                        k, v = line.split(": ", 1)
                        meta[k.strip()] = v.strip().strip("'\"")
                if i > 15:
                    break
            if "creationDate" in meta:
                # Store path relative to journal_dir so subfoldered entries
                # with colliding basenames stay distinct.
                entry = {
                    "file": os.path.relpath(fpath, journal_dir),
                    "date": meta["creationDate"][:10],
                }
                if "floor" in meta:
                    entry["floor"] = meta["floor"]
                if "floor_level" in meta:
                    entry["floor_level"] = meta["floor_level"]
                if "floor_arc" in meta:
                    entry["floor_arc"] = _parse_inline_list(meta["floor_arc"])
                entries.append(entry)

    if skipped:
        preview = ", ".join(f"{f} [{s}]" for f, s in skipped[:5])
        more = " ..." if len(skipped) > 5 else ""
        print(f"  note: skipped {len(skipped)} unreadable file(s): {preview}{more}",
              file=sys.stderr)
    entries.sort(key=lambda x: x["date"])
    output = {
        "total": len(entries),
        "last_updated": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "entries": entries,
    }
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Indexed {len(entries)} entries → {output_path}")
    if entries:
        print(f"  date range: {entries[0]['date']} → {entries[-1]['date']}")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
