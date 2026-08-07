#!/usr/bin/env python3
"""
auto-crm-from-mentions.py — auto-create CRM stubs for people mentioned in notes.

Scans a target file (or the whole vault) for Title-Cased wikilinks that look
like person names but have no corresponding CRM entry. Creates a minimal stub
for each so wikilinks aren't orphaned and second-brain-mapping's person
extractor picks them up on the next run.

Runs fast (no LLM). Safe to fire after every /journal save, after every
meeting import, or as a periodic sweep. Idempotent: won't recreate existing
entries.

Usage:
    python3 auto-crm-from-mentions.py                     # scan whole vault
    python3 auto-crm-from-mentions.py <file>              # scan one file
    python3 auto-crm-from-mentions.py --dry-run           # preview only
    python3 auto-crm-from-mentions.py --since 2026-04-20  # only files modified after
"""
import argparse
import glob
import os
import re
import sys
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "extractors"))

from _base import VAULT, CRM_ROOT, SKIP_PARTS, WIKILINK_RE, get_crm_names  # noqa: E402

# Words that disqualify a wikilink from being a person name.
# If any of these appear in the candidate, it's not auto-stubbed.
# (Conservative: false-negatives are OK; the user can add manually.)
NON_PERSON_WORDS = {
    # Structural / concept markers
    "The", "A", "An", "And", "Or", "But", "Of", "In", "On", "For", "With",
    "Framework", "Playbook", "Guide", "Tips", "Prep", "Review", "Sequence",
    "Board", "Panel", "Committee", "Workshop", "Workshop", "Session", "Meeting",
    "Hub", "Center", "Office", "Studio", "Academy", "Group", "Team",
    "Ltd", "Inc", "Corp", "Company", "LLC", "Co",
    "Advisory", "Council", "Network", "Circle", "Collective",
    "Protocol", "Pipeline", "Process", "Strategy", "Tactics", "Approach",
    "Plan", "Log", "Record", "Report", "Map", "System", "Model",
    "Day", "Week", "Month", "Year", "Quarter", "Morning", "Evening", "Night",
    "Tomorrow", "Today", "Yesterday",
    "Note", "Notes", "List", "Ideas", "Quiz", "Test", "Question", "Questions",
    # Very generic adjectives that appear in concept titles
    "High", "Low", "Good", "Bad", "New", "Old", "First", "Last", "Next",
    "Hidden", "Public", "Private", "Open", "Closed", "Full", "Empty",
    "Deep", "Shallow", "Strong", "Weak", "Light", "Dark",
    # Named domains / places that match Title Case
    "Silicon", "Valley", "Street", "Avenue", "Road", "Park",
    "York", "Angeles", "Francisco", "Diego", "Juan", "Paulo",
    # Generic business/emotion concept words that commonly look like names
    "Raising", "Money", "Seed", "Round", "Pitch", "Deck",
    "Loop", "Floor", "Elevator",
    # Time of day / abstract
    "Execution", "Presence", "Awareness", "Focus",
}

# Exact-match blacklist — phrases that look like a Title Case name but are
# concepts or place names, not people. Extend this set with your own
# project names, book titles, or vault-specific concepts that keep getting
# misclassified as people.
CONCEPT_BLACKLIST = {
    # Place names
    "Silicon Valley", "New York", "New York City",
    "Los Angeles", "San Francisco",
    # Generic product/tool names
    "Google Meet", "Google Drive", "Apple Health",
    # Generic concept phrases
    "Second Brain", "Ad Hoc", "About Me",
}

# Person name: exactly 2 or 3 Title-cased words, each 3-20 chars, no disqualifying word.
# Allows hyphenated last names: "Garcia-Lopez" is one word.
PERSON_NAME_RE = re.compile(r"^[A-Z][a-záéíóúñ]+(?:-[A-Z][a-záéíóúñ]+)?(?:\s[A-Z][a-záéíóúñ]+(?:-[A-Z][a-záéíóúñ]+)?){1,2}$")


def is_likely_person_name(candidate):
    """Tight heuristic: 2-3 Title-cased words, no concept keywords, known-name-like."""
    c = candidate.strip()
    if c in CONCEPT_BLACKLIST:
        return False
    if not PERSON_NAME_RE.match(c):
        return False
    words = c.replace("-", " ").split()
    # Any disqualifying word anywhere → reject
    if any(w in NON_PERSON_WORDS for w in words):
        return False
    # Each word must be 3-20 chars (filters initials, overly long tokens)
    if not all(3 <= len(w) <= 20 for w in words):
        return False
    return True


def scan_file_for_names(filepath):
    """Return set of unique candidate person names from a file's wikilinks."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return set()
    candidates = set()
    for m in WIKILINK_RE.findall(content):
        base = os.path.basename(m.strip())
        if is_likely_person_name(base):
            candidates.add(base)
    return candidates


def create_stub(name, source_file, dry_run=False):
    """Create a minimal CRM stub. Returns path if created, None if skipped."""
    target = os.path.join(CRM_ROOT, f"{name}.md")
    if os.path.exists(target):
        return None
    if dry_run:
        return target

    today = date.today().isoformat()
    rel_source = os.path.relpath(source_file, VAULT) if source_file else "unknown"
    content = f"""---
type: person
creationDate: {today}
source: "auto-detected from mentions"
relationship: ""
company: ""
status: "needs-review"
last_interaction: ""
next_step: ""
priority: "unreviewed"
---

# [[{name}]]

*Auto-created stub. First mentioned in: `{rel_source}`.*

Fill in the relationship type, company, and priority when you confirm this is someone worth tracking. If this is not a person (e.g., a concept or place), delete this file and add `{name}` to the `CONCEPT_BLACKLIST` in `auto-crm-from-mentions.py`.

## Notes

"""
    os.makedirs(CRM_ROOT, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    return target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", help="File to scan. Omit for vault-wide (restricted by default, see --since).")
    ap.add_argument("--dry-run", action="store_true", help="Preview what would be created.")
    ap.add_argument("--since", help="Only scan files modified after YYYY-MM-DD. Default: 1 day ago in bulk mode.")
    ap.add_argument("--full-sweep", action="store_true",
                    help="Bulk mode: scan all files regardless of mtime. Requires --dry-run for safety on first use.")
    ap.add_argument("--cap", type=int, default=20,
                    help="Max number of stubs to create in one invocation (default 20). Bulk sweeps can override.")
    args = ap.parse_args()

    crm_existing = get_crm_names()

    # Determine file set
    if args.target:
        files = [os.path.abspath(args.target)]
    else:
        files = [fp for fp in glob.glob(os.path.join(VAULT, "**", "*.md"), recursive=True)
                 if not (set(fp.split(os.sep)) & SKIP_PARTS)]
        # Safety: bulk mode without --full-sweep defaults to "last 24 hours"
        if not args.full_sweep and not args.since:
            cutoff = datetime.now().timestamp() - 86400
            files = [f for f in files if os.path.getmtime(f) >= cutoff]
            print(f"  (bulk mode, default scope: files modified in last 24h. Use --full-sweep for vault-wide.)")

    if args.since:
        try:
            cutoff = datetime.fromisoformat(args.since).timestamp()
            files = [f for f in files if os.path.getmtime(f) >= cutoff]
        except Exception as e:
            print(f"Bad --since value: {e}", file=sys.stderr)
            sys.exit(2)

    # Safety on full-sweep first run
    if args.full_sweep and not args.dry_run:
        print("⚠  --full-sweep without --dry-run will create stubs for every Title-Cased")
        print("   wikilink in the whole vault. This can create hundreds of files.")
        print("   Re-run with --dry-run first, review the output, then commit.")
        sys.exit(4)

    # Aggregate candidates with source tracking
    new_candidates = {}  # name → first source file
    for fp in files:
        for name in scan_file_for_names(fp):
            if name in crm_existing or name in new_candidates:
                continue
            new_candidates[name] = fp

    if not new_candidates:
        print(f"auto-crm: scanned {len(files)} files. No new people to stub.")
        return

    print(f"auto-crm: {len(new_candidates)} new candidate(s) from {len(files)} files")

    # Cap for safety unless dry-run
    if not args.dry_run and len(new_candidates) > args.cap:
        print(f"⚠  {len(new_candidates)} candidates exceeds --cap {args.cap}. Refusing to auto-create.")
        print(f"   Run with --dry-run to preview, then re-run with --cap {len(new_candidates)} to accept.")
        sys.exit(5)

    created = skipped = 0
    for name in sorted(new_candidates):
        src = new_candidates[name]
        path = create_stub(name, src, dry_run=args.dry_run)
        if path:
            prefix = "[DRY] Would create" if args.dry_run else "CREATED"
            print(f"  {prefix}: 👤 CRM/{name}.md  (from: {os.path.basename(src)})")
            created += 1
        else:
            skipped += 1

    action = "would create" if args.dry_run else "created"
    print(f"\nDone. {created} {action}, {skipped} skipped.")
    if not args.dry_run and created:
        print("Next: review the new stubs, fill in relationship + priority, or delete if not real contacts.")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
