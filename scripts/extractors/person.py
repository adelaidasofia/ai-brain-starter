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

# Journals folder: self-locating. English installs use "📓 Journals";
# es-CO vaults use "📓 Diarios". Pick the first that actually exists so the
# mention index never silently scans a non-existent path (bug found 2026-07-29:
# a hardcoded English path produced person_journal_mention_count=0 vault-wide,
# which killed 4 of the 8 insight-engine sections).
_JOURNAL_CANDIDATES = ("📓 Journals", "📓 Diarios", "Journals", "Diarios")
JOURNALS_ROOT = next(
    (os.path.join(VAULT, c) for c in _JOURNAL_CANDIDATES
     if os.path.isdir(os.path.join(VAULT, c))),
    os.path.join(VAULT, _JOURNAL_CANDIDATES[0]),
)

# Per-run cache: person_name → [(journal_iso, floor_num), ...]
_JOURNAL_INDEX = None


# Floor name → number (34-floor High-Rise scale, ES + EN aliases).
# Bug found 2026-07-29: journals write `floor: Entusiasmo` (a NAME) while this
# extractor only read `floor_num` (a NUMBER), so floor co-occurrence was always
# empty and the lucky-charm / drag-people insight sections never fired.
FLOOR_NAME_TO_NUM = {
    "asco": 1, "disgust": 1, "verguenza": 2, "vergüenza": 2, "shame": 2,
    "bochorno": 3, "embarrassment": 3, "culpa": 4, "guilt": 4,
    "apatia": 5, "apatía": 5, "apathy": 5, "resignacion": 6, "resignación": 6,
    "resignation": 6, "confusion": 7, "confusión": 7, "soledad": 8,
    "loneliness": 8, "aburrimiento": 9, "boredom": 9, "duelo": 10, "grief": 10,
    "decepcion": 11, "decepción": 11, "disappointment": 11, "herida": 12,
    "hurt": 12, "miedo": 13, "fear": 13, "frustracion": 14, "frustración": 14,
    "frustration": 14, "deseo": 15, "desire": 15, "rabia": 16, "anger": 16,
    "desprecio": 17, "contempt": 17, "orgullo": 18, "pride": 18,
    "valentia": 19, "valentía": 19, "courage": 19, "esperanza": 20, "hope": 20,
    "neutralidad": 21, "neutrality": 21, "disposicion": 22, "disposición": 22,
    "willingness": 22, "aceptacion": 23, "aceptación": 23, "acceptance": 23,
    "razon": 24, "razón": 24, "reason": 24, "confianza": 25, "trust": 25,
    "compasion": 26, "compasión": 26, "compassion": 26, "humildad": 27,
    "humility": 27, "pertenencia": 28, "belonging": 28, "amor": 29, "love": 29,
    "gratitud": 30, "gratitude": 30, "entusiasmo": 31, "excitement": 31,
    "asombro": 32, "wonder": 32, "alegria": 33, "alegría": 33, "joy": 33,
    "paz": 34, "peace": 34,
}


def _floor_num_from(fm):
    """Prefer explicit floor_num; else translate the floor NAME to its number."""
    n = fm.get("floor_num")
    if isinstance(n, int):
        return n
    raw = fm.get("floor")
    if isinstance(raw, list):
        raw = raw[-1] if raw else None
    if not isinstance(raw, str):
        return None
    return FLOOR_NAME_TO_NUM.get(raw.strip().lower())


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
        floor_num = _floor_num_from(fm)
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
