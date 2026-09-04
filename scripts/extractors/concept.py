#!/usr/bin/env python3
"""
extractors/concept.py — structured metadata for concept notes.
Type: `concept`.

concept_mention_count + concept_last_mentioned_iso require a vault-wide backlink
scan. Cached per-run. Dormant flag = last mention >180 days ago.
"""
import glob
import os
import re
from datetime import date, datetime, timedelta

import yaml

from _base import (
    VAULT, SKIP_PARTS, iso_date_from, extract_section, wikilinks_in,
    ExtractionResult,
)

AUTO_FIELDS = (
    "concept_domain", "concept_related", "concept_first_seen_iso",
    "concept_mention_count", "concept_last_mentioned_iso", "concept_dormant",
)

DORMANT_THRESHOLD_DAYS = 180

# Per-run cache: concept_name → [(date_iso, file_rel_path), ...]
_BACKLINK_INDEX = None


def _build_backlink_index():
    """One-shot scan: every wikilink in every non-infra markdown file."""
    global _BACKLINK_INDEX
    if _BACKLINK_INDEX is not None:
        return _BACKLINK_INDEX
    _BACKLINK_INDEX = {}
    wikilink_re = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")

    for fp in glob.glob(os.path.join(VAULT, "**", "*.md"), recursive=True):
        parts = set(fp.split(os.sep))
        if parts & SKIP_PARTS:
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        file_date = None
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end != -1:
                try:
                    fm = yaml.safe_load(content[3:end]) or {}
                except Exception:
                    fm = {}
                file_date = (
                    fm.get("date_iso")
                    or iso_date_from(fm.get("creationDate"))
                )
        if not file_date:
            try:
                mtime = os.path.getmtime(fp)
                file_date = date.fromtimestamp(mtime).isoformat()
            except Exception:
                continue

        for m in set(wikilink_re.findall(content)):
            basename = os.path.basename(m.strip())
            if not basename:
                continue
            _BACKLINK_INDEX.setdefault(basename, []).append(file_date)
    return _BACKLINK_INDEX


def _domain_from_path(fp):
    """Infer concept domain from path emoji folder."""
    rel = fp.replace(VAULT, "").lstrip(os.sep)
    segments = rel.split(os.sep)
    mapping = {
        "📝 Notes": "notes", "🌱 Curiosities": "curiosities",
        "🏫 School": "school", "📚 Books": "books", "🧠 Psychology": "psychology",
        "💼 Business": "business",
        # Spanish counterparts of the six folders above. The setup interview
        # creates LOCALIZED folder names on a non-English install, and an
        # English-only mapping returns None for every note in them — so
        # `concept_domain` comes out empty for the whole vault, silently, and
        # every downstream grouping by domain sees one undifferentiated blob.
        "📝 Notas": "notes", "🌱 Curiosidades": "curiosities",
        "🏫 Escuela": "school", "📚 Libros": "books",
        "🧠 Psicología": "psychology", "💼 Negocios": "business",
    }
    for seg in segments:
        if seg in mapping:
            return mapping[seg]
    return None


def extract(filepath, body, fm, context):
    name = os.path.splitext(os.path.basename(filepath))[0]
    backlinks = _build_backlink_index()
    appearances = backlinks.get(name, [])

    last_iso = max(appearances) if appearances else None
    first_iso = min(appearances) if appearances else None

    dormant = False
    if last_iso:
        try:
            last_d = datetime.fromisoformat(last_iso).date()
            dormant = (date.today() - last_d) > timedelta(days=DORMANT_THRESHOLD_DAYS)
        except Exception:
            dormant = False

    # Related: wikilinks in ## Related / ## Connected / ## See Also
    related = []
    for header in (r"^##\s+Related", r"^##\s+Connected", r"^##\s+See Also"):
        section = extract_section(body, header)
        if section:
            for lnk in wikilinks_in(section):
                if lnk and lnk not in related:
                    related.append(lnk)
            break

    fields = {
        "concept_domain": _domain_from_path(filepath),
        "concept_related": related[:15],
        "concept_first_seen_iso": first_iso,
        "concept_mention_count": len(appearances),
        "concept_last_mentioned_iso": last_iso,
        "concept_dormant": dormant,
    }
    return ExtractionResult(fields, AUTO_FIELDS, auto_fields=AUTO_FIELDS)
