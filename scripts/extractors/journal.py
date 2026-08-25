#!/usr/bin/env python3
"""
extractors/journal.py — structured metadata for daily journals.

Type: `journal`
Emits: smart_excerpt, concepts_extracted, people_mentioned, word_count,
       floor_num, date_iso.
"""
import os
import re

from _base import (
    extract_first_prose_sentence, extract_section, match_people,
    count_words, iso_date_from, wikilinks_in, ExtractionResult,
)
from _floors import floor_num_from_name

# Auto-written fields, in render order. First one is the idempotency marker.
AUTO_FIELDS = (
    "smart_excerpt", "concepts_extracted", "people_mentioned",
    "word_count", "floor_num", "date_iso",
)

SKIP_FILENAME_PATTERNS = (
    "[AI Extract]", "Weekly", "Monthly Summary",
    "Knowledge Graph Report", "knowledge-graph",
)


def _floor_num(fm):
    """`floor_num` on the 34-floor High-Rise scale, from the entry's `floor` NAME.

    Reads the name only (never a stale `floor_num` from an earlier run): this
    extractor OWNS floor_num, so it must always be re-derived from what the
    person wrote. Names resolve in English and Spanish, case-insensitively,
    through the one canonical map in _floors (which mirrors vendor/high-rise/
    floors.md). A list of floors scores as the lowest one, as before.
    """
    return floor_num_from_name(fm.get("floor"))


def _concepts(body):
    """Wikilinks from the ## Concepts section (manually curated)."""
    section = extract_section(body, r"^##\s+Concepts")
    if not section:
        return []
    seen = []
    for link in wikilinks_in(section):
        if link and link not in seen:
            seen.append(link)
    return seen[:30]


def extract(filepath, body, fm, context):
    basename = os.path.basename(filepath)
    if any(p in basename for p in SKIP_FILENAME_PATTERNS):
        return None

    excerpt = extract_first_prose_sentence(body)
    if not excerpt:
        return None

    fields = {
        "smart_excerpt": excerpt,
        "concepts_extracted": _concepts(body),
        "people_mentioned": match_people(body, context["crm_names"]),
        "word_count": count_words(body),
        "floor_num": _floor_num(fm),
        "date_iso": iso_date_from(fm.get("creationDate")),
    }
    return ExtractionResult(fields, AUTO_FIELDS, auto_fields=AUTO_FIELDS)
