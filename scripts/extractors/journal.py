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


# Vault-defined floors: the canonical map above is a fixed 34-floor scale, and
# its own contract says an unknown name "simply stays out of floor-based
# findings". A vault that defines its own Floors notes (custom names, or
# aliases in another language) therefore scored nothing. Read those notes as a
# FALLBACK so a custom name resolves, while the canonical map stays primary.
#
# Read with a plain bounded open(), NOT the shared hooks/_lib primitive: the
# extractors are copied/symlinked into a vault standalone, without hooks/, so
# importing from there makes the whole extractor fail to load — and the
# dispatcher swallows that into silently-missing fields rather than an error.
_FLOOR_INDEX = None
_FLOOR_NOTE_MAX_BYTES = 64 * 1024


def _load_floor_index():
    """Map floor name/alias (lowercased) -> floor_number from the vault's own Floors notes."""
    index = {}
    for fp in glob.glob(os.path.join(VAULT, "**", "Floors", "*.md"), recursive=True):
        try:
            if os.path.getsize(fp) > _FLOOR_NOTE_MAX_BYTES:
                continue
            with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read(_FLOOR_NOTE_MAX_BYTES)
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
    """`floor_num` on the 34-floor High-Rise scale, from the entry's `floor` NAME.

    Reads the name only (never a stale `floor_num` from an earlier run): this
    extractor OWNS floor_num, so it must always be re-derived from what the
    person wrote. Names resolve in English and Spanish, case-insensitively,
    through the one canonical map in _floors (which mirrors vendor/high-rise/
    floors.md). A list of floors scores as the lowest one, as before.
    """
    global _FLOOR_INDEX
    raw = fm.get("floor")
    num = floor_num_from_name(raw)
    if num is not None:
        return num
    # Canonical map did not know this name: fall back to the vault's own Floors.
    if raw is None:
        return None
    if _FLOOR_INDEX is None:
        _FLOOR_INDEX = _load_floor_index()
    vals = raw if isinstance(raw, list) else [raw]
    nums = [_FLOOR_INDEX[str(v).strip().lower()]
            for v in vals if str(v).strip().lower() in _FLOOR_INDEX]
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
