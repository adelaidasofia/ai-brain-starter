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
from _floors import floor_num_from_fm

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

# Journals folder: self-locating, the same candidate list (and order) that
# scripts/build-journal-index.py uses for /weekly and /monthly. The setup
# interview creates a LOCALIZED folder on a non-English install ("📓 Diarios"
# on Spanish, "📓 Diário" on Portuguese), and a hardcoded "📓 Journals" here
# scanned a path that did not exist — silently: every person got
# person_journal_mention_count = 0 and an empty person_floor_cooccurrence,
# which in turn switched off the lucky-charm / drag-people / stale-relationship
# sections of the insight engine for the whole vault. Pick the first candidate
# that exists; fall back to the English default so the glob below still yields
# nothing (rather than crashing) on a vault with no journal folder at all.
_JOURNAL_CANDIDATES = (
    "📓 Journals", "Journals",       # en (Phase 3 default)
    "📔 Journal", "Journal",
    "📓 Diarios", "Diarios",         # es (what Phase 1 tells the installer to create)
    "📓 Diario", "Diario",           # es, singular variant
    "📓 Diário", "Diário",           # pt
)
JOURNALS_ROOT = next(
    (os.path.join(VAULT, c) for c in _JOURNAL_CANDIDATES
     if os.path.isdir(os.path.join(VAULT, c))),
    os.path.join(VAULT, _JOURNAL_CANDIDATES[0]),
)

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
        # The journal writes the floor's NAME (`floor: Hope` / `floor: Esperanza`);
        # `floor_num` only exists once the journal extractor has run, and on an
        # older scale if it ran long ago. Translate the name first, then fall
        # back to the stored number — otherwise co-occurrence is empty on every
        # vault whose journals were never extracted, and the insight sections
        # built on it never fire.
        floor_num = floor_num_from_fm(fm)
        if not date_iso:
            continue

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
