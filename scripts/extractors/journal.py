#!/usr/bin/env python3
"""
extractors/journal.py — structured metadata for daily journals.

Type: `journal`
Emits: smart_excerpt, concepts_extracted, people_mentioned, word_count,
       floor_num, date_iso.
"""
import glob
import os
import re

from _base import (
    VAULT, extract_first_prose_sentence, extract_section, match_people,
    count_words, iso_date_from, wikilinks_in, ExtractionResult,
)

# Auto-written fields, in render order. First one is the idempotency marker.
AUTO_FIELDS = (
    "smart_excerpt", "concepts_extracted", "people_mentioned",
    "word_count", "floor_num", "date_iso",
)

# Hawkins Map of Consciousness (Shame=1 → Enlightenment=17)
FLOOR_MAP = {
    "Shame": 1, "Guilt": 2, "Apathy": 3, "Grief": 4, "Fear": 5,
    "Desire": 6, "Anger": 7, "Pride": 8, "Courage": 9, "Hope": 9,
    "Neutrality": 10, "Willingness": 11, "Acceptance": 12, "Reason": 13,
    "Love": 14, "Joy": 15, "Excitement": 15, "Peace": 16, "Enlightenment": 17,
}

SKIP_FILENAME_PATTERNS = (
    "[AI Extract]", "Weekly", "Monthly Summary",
    "Knowledge Graph Report", "knowledge-graph",
)


_FLOOR_INDEX = None


def _load_floor_index():
    """Map floor name/alias (lowercased) -> floor_number, read from the vault's own
    Floors notes. Vaults define their own scale and vocabulary; FLOOR_MAP below is
    only a fallback for vaults that ship no Floors folder."""
    index = {}
    for fp in glob.glob(os.path.join(VAULT, "**", "Floors", "*.md"), recursive=True):
        try:
            content = open(fp, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        num = re.search(r"^floor_number:\s*(\d+)", content, re.MULTILINE)
        if not num:
            continue
        num = int(num.group(1))
        index[os.path.splitext(os.path.basename(fp))[0].lower()] = num
        aliases = re.search(r"^aliases:\s*\[([^\]]*)\]", content, re.MULTILINE)
        if aliases:
            for a in aliases.group(1).split(","):
                a = a.strip().strip("\"'").lower()
                if a:
                    index.setdefault(a, num)
    return index


def _floor_num(fm):
    global _FLOOR_INDEX
    raw = fm.get("floor")
    if not raw:
        return None
    if _FLOOR_INDEX is None:
        _FLOOR_INDEX = _load_floor_index()
    vals = raw if isinstance(raw, list) else [raw]
    nums = [_FLOOR_INDEX[str(v).strip().lower()]
            for v in vals if str(v).strip().lower() in _FLOOR_INDEX]
    if not nums:
        nums = [FLOOR_MAP[str(v)] for v in vals if str(v) in FLOOR_MAP]
    return min(nums) if nums else None


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
