#!/usr/bin/env python3
"""
extractors/_floors.py — floor NAME → floor NUMBER, in one place.

The journal tags every entry with the floor's NAME (`floor: Hope`,
`floor: Esperanza`); every floor-based computation downstream (the journal
extractor's `floor_num`, person floor co-occurrence, the insight engine's
lucky-charm / drag / deep-processing / baseline sections) wants its NUMBER.
This module is the only translation. journal.py, person.py and
vault-insight-engine.py all import from here, so they cannot disagree.

The numbers are the 34-floor High-Rise scale, mirrored from the pinned
canonical table in vendor/high-rise/floors.md (English + Spanish columns).
It is a copy, not a parse: the extractors are symlinked into a vault's own
scripts/extractors/ folder, where vendor/ is not present. The CI test
tests/integration/test_floor_name_map_canonical.sh fails when this map and the
vendored table disagree, so the copy cannot drift silently. Spanish names are
listed with and without accents so a hand-typed `floor: Alegria` still
resolves. Lookups are case-insensitive.

Infrastructure module (leading underscore): the dispatcher never loads it as
an extractor.
"""

FLOOR_NAME_TO_NUM = {
    # Low (1-18): reactive
    "disgust": 1, "asco": 1,
    "shame": 2, "vergüenza": 2, "verguenza": 2,
    "embarrassment": 3, "bochorno": 3,
    "guilt": 4, "culpa": 4,
    "apathy": 5, "apatía": 5, "apatia": 5,
    "resignation": 6, "resignación": 6, "resignacion": 6,
    "confusion": 7, "confusión": 7,
    "loneliness": 8, "soledad": 8,
    "boredom": 9, "aburrimiento": 9,
    "grief": 10, "duelo": 10,
    "disappointment": 11, "decepción": 11, "decepcion": 11,
    "hurt": 12, "herida": 12,
    "fear": 13, "miedo": 13,
    "frustration": 14, "frustración": 14, "frustracion": 14,
    "desire": 15, "deseo": 15,
    "anger": 16, "rabia": 16,
    "contempt": 17, "desprecio": 17,
    "pride": 18, "orgullo": 18,
    # Middle (19-24): transitional
    "courage": 19, "valentía": 19, "valentia": 19,
    "hope": 20, "esperanza": 20,
    "neutrality": 21, "neutralidad": 21,
    "willingness": 22, "disposición": 22, "disposicion": 22,
    "acceptance": 23, "aceptación": 23, "aceptacion": 23,
    "reason": 24, "razón": 24, "razon": 24,
    # High (25-34): generative
    "trust": 25, "confianza": 25,
    "compassion": 26, "compasión": 26, "compasion": 26,
    "humility": 27, "humildad": 27,
    "belonging": 28, "pertenencia": 28,
    "love": 29, "amor": 29,
    "gratitude": 30, "gratitud": 30,
    "excitement": 31, "entusiasmo": 31,
    "wonder": 32, "asombro": 32,
    "joy": 33, "alegría": 33, "alegria": 33,
    "peace": 34, "paz": 34,
}


def floor_num_from_name(raw):
    """Translate a floor NAME (str, or list of str) to its number, or None.

    A list means the entry names several floors; the LOWEST one is returned,
    matching what the journal extractor has always done (the day is scored by
    the floor it had to climb from). Unknown names resolve to None — never a
    guess, so a custom floor name simply stays out of floor-based findings.
    """
    if raw is None:
        return None
    vals = raw if isinstance(raw, list) else [raw]
    nums = []
    for v in vals:
        if not isinstance(v, str):
            continue
        n = FLOOR_NAME_TO_NUM.get(v.strip().lower())
        if n is not None:
            nums.append(n)
    return min(nums) if nums else None


def floor_num_from_fm(fm):
    """Floor number for a note's frontmatter: the NAME wins, a stored number is
    the fallback.

    The name is what the person wrote; `floor_num` is derived, may have been
    written by an older extractor on an older scale, and is only trusted when
    there is no name to translate (or the name is unknown).
    """
    n = floor_num_from_name(fm.get("floor"))
    if n is not None:
        return n
    stored = fm.get("floor_num")
    return stored if isinstance(stored, int) and not isinstance(stored, bool) else None
