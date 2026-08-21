#!/usr/bin/env python3
"""
extractors/person.py — structured metadata for CRM entries.
Type: `person`.

Cross-vault fields: mention count + last-journal-iso are computed by scanning
all journals for backlinks to this person. Expensive per-file, cached per-run.
"""
import glob
import os
import re
import yaml

from _base import (
    VAULT, iso_date_from, count_words, ExtractionResult,
)

AUTO_FIELDS = (
    "person_relationship_type", "person_company", "person_is_public_figure",
    "person_last_journal_iso", "person_journal_mention_count",
    "person_floor_cooccurrence", "person_priority", "person_next_step",
    "word_count",
)

# Relationship-type strings that mark a CRM entry as a public figure / author
# rather than a personal contact. These get person_is_public_figure: true and
# are excluded from friend-group insights (drag people / lucky charm).
PUBLIC_FIGURE_RELATIONSHIP_HINTS = {
    "author", "author/thinker", "thinker", "writer", "public figure",
    "celebrity", "influencer", "podcaster", "researcher", "speaker",
    "teacher", "public intellectual", "academic",
}

# Journals folder: override with JOURNALS_FOLDER env var, else auto-detect.
# Same pattern as CRM_ROOT in _base.py. This used to be a bare
# os.path.join(VAULT, "📓 Journals"): any vault that names the folder in another
# language (e.g. "📓 Diarios") made the glob below match nothing, so EVERY person
# got person_journal_mention_count: 0 — silently, with no error. That empties the
# people sections of the insight engine (lucky-charm, drag people, contacts going
# cold) no matter how many journals exist. Found 2026-08-20.
JOURNALS_ROOT = os.environ.get("JOURNALS_FOLDER")
if not JOURNALS_ROOT:
    for _candidate in ("📓 Journals", "📓 Diarios", "Journals", "Diarios",
                       "📔 Journal", "Journal", "Daily"):
        _p = os.path.join(VAULT, _candidate)
        if os.path.isdir(_p):
            JOURNALS_ROOT = _p
            break
    if not JOURNALS_ROOT:
        JOURNALS_ROOT = os.path.join(VAULT, "📓 Journals")

# Per-run cache: person_name → [(journal_iso, floor_num), ...]
_JOURNAL_INDEX = None


def _build_journal_index():
    """Scan every journal once, extract (name_mentioned, date_iso, floor_num)."""
    global _JOURNAL_INDEX
    if _JOURNAL_INDEX is not None:
        return _JOURNAL_INDEX

    _JOURNAL_INDEX = {}
    wikilink_re = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")

    for fp in glob.glob(os.path.join(JOURNALS_ROOT, "**", "*.md"), recursive=True):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue
        if not content.startswith("---"):
            continue
        end = content.find("\n---", 3)
        if end == -1:
            continue
        try:
            fm = yaml.safe_load(content[3:end]) or {}
        except Exception:
            continue

        date_iso = fm.get("date_iso") or iso_date_from(fm.get("creationDate"))
        floor_num = fm.get("floor_num")
        if not date_iso:
            continue
        # Normalize to str. PyYAML parses an unquoted `date_iso: 2026-08-12` into
        # a datetime.date but leaves a quoted one as str, so a vault with both
        # styles yields a mixed list and `max()` in extract() dies with
        # "'>' not supported between instances of 'str' and 'datetime.date'".
        # ISO-8601 sorts identically as text, so string form is safe to compare.
        if not isinstance(date_iso, str):
            date_iso = date_iso.isoformat()

        body = content[end + 4:]
        # Find every wikilink in the body, Title-Cased
        seen_in_this_file = set()
        for m in wikilink_re.findall(body):
            basename = os.path.basename(m.strip())
            if not basename or not basename[0].isupper():
                continue
            if basename in seen_in_this_file:
                continue
            seen_in_this_file.add(basename)
            _JOURNAL_INDEX.setdefault(basename, []).append((date_iso, floor_num))
    return _JOURNAL_INDEX


def _priority(fm):
    p = fm.get("priority")
    if not p:
        return None
    p = str(p).lower().strip()
    return p if p in ("high", "mid", "medium", "low") else None


def _is_public_figure(fm):
    """True if relationship type or notes flag this as author/thinker/public figure."""
    rel = (fm.get("relationship") or "").lower().strip()
    if rel in PUBLIC_FIGURE_RELATIONSHIP_HINTS:
        return True
    # Also check if any hint word appears within a longer descriptor
    for hint in PUBLIC_FIGURE_RELATIONSHIP_HINTS:
        if hint in rel:
            return True
    return False


def extract(filepath, body, fm, context):
    person_name = os.path.splitext(os.path.basename(filepath))[0]
    journal_idx = _build_journal_index()
    appearances = journal_idx.get(person_name, [])

    # Last journal mention
    if appearances:
        last_iso = max(a[0] for a in appearances)
    else:
        last_iso = None

    # Floor co-occurrence (ordered, most common first, top 5)
    floor_counts = {}
    for (_, fn) in appearances:
        if fn is not None:
            floor_counts[fn] = floor_counts.get(fn, 0) + 1
    top_floors = [str(f) for f, _ in sorted(floor_counts.items(), key=lambda x: -x[1])[:5]]

    fields = {
        "person_relationship_type": fm.get("relationship"),
        "person_company": fm.get("company"),
        "person_is_public_figure": _is_public_figure(fm),
        "person_last_journal_iso": last_iso,
        "person_journal_mention_count": len(appearances),
        "person_floor_cooccurrence": top_floors,
        "person_priority": _priority(fm),
        "person_next_step": fm.get("next_step"),
        "word_count": count_words(body),
    }
    return ExtractionResult(fields, AUTO_FIELDS, auto_fields=AUTO_FIELDS)
