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
import sys
import yaml
from pathlib import Path

from _base import (
    VAULT, iso_date_from, count_words, ExtractionResult,
)

# hooks/_lib/safe_read.py is the one audited bounded-read primitive every
# recursive walker must reach (scripts/check-cloud-safe-file-walkers.py) --
# a hand-rolled open().read() is not trusted even inside a try/except. person.py
# sits at scripts/extractors/, one level below the scripts/ files that do
# `parent.parent`, so this needs the extra `.parent` to land on <repo>/hooks.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "hooks"))
from _lib.safe_read import safe_read_text  # noqa: E402

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
        result = safe_read_text(fp, encoding="utf-8")
        if not result.ok:
            continue
        content = result.text
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


def _coerce_text(value):
    """Frontmatter fields are author-written: the same key shows up as a string,
    a YAML list, or a number across vaults. Flatten to one lowercase string so
    callers never have to care which shape arrived."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(_coerce_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_coerce_text(v) for v in value.values())
    return str(value).lower().strip()


def _is_public_figure(fm):
    """True if relationship type or notes flag this as author/thinker/public figure."""
    rel = _coerce_text(fm.get("relationship"))
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
