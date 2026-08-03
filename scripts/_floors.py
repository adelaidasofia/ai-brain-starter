#!/usr/bin/env python3
"""Floor vocabulary and frontmatter consistency, read from the vault itself.

The 34-floor list is NOT declared here, and must never be. ai-brain-starter
consumes the High-Rise framework (vendor/high-rise/floors.md) and generates one
note per floor under floors/; this module reads those notes back. Any vault
works, in any language, as long as its floor notes carry `floor_number`.

When no floor notes are readable there is no vocabulary, and every check is
skipped rather than guessed. An earlier version of this check carried its own
hardcoded tier boundaries, disagreed with the vault, and flagged a correct
entry as broken. Reading the vault is the whole point.

Python 3.9 compatible: no PEP 604 (`X | None`) annotations.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Folders that may hold floor notes, relative to the vault root. Every one that
# exists is read and merged — a vault may carry more than one layout.
FLOOR_NOTE_DIRS = (
    ("floors",),
    ("Notes", "Floors"),
    ("Notas", "Floors"),
    ("📝 Notas", "Floors"),
)

# Tier vocabulary normalises onto these three. Keys are accent-stripped
# lowercase. This is not a floor list: it is the three-way tier split, which
# the framework fixes at low/middle/high in every language.
TIER_ALIASES = {
    "low": "low", "bajo": "low", "baja": "low",
    "middle": "middle", "mid": "middle", "medio": "middle", "media": "middle",
    "high": "high", "alto": "high", "alta": "high",
}

# `aliases` carries positional entries like "Floor 9" and "Piso 9". Those are
# coordinates, not names; admitting them would let the string "piso 9" resolve
# as though a floor were named that.
_POSITIONAL_ALIAS = re.compile(r"^(floor|piso)\s*\d+$")


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", str(s))
                   if unicodedata.category(c) != "Mn")


def normalise_name(s):
    """Floor names compare accent-insensitively and case-insensitively."""
    if s is None:
        return ""
    return strip_accents(str(s).strip().lower())


def normalise_tier(value):
    """Any spelling of the three tiers -> 'low' | 'middle' | 'high'. None if unknown."""
    if value is None:
        return None
    return TIER_ALIASES.get(normalise_name(value))


def parse_frontmatter(text):
    """Line-based frontmatter reader (stdlib only — no YAML dependency)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta = {}
    for line in text[3:end].split("\n"):
        if ": " in line or line.rstrip().endswith(":"):
            k, _, v = line.partition(":")
            k = k.strip()
            if k and not k.startswith("#"):
                meta[k] = v.strip().strip("'\"")
    return meta


def parse_inline_list(v):
    """'[a, b, c]' -> ['a','b','c']; bare value -> [value]; empty/null -> []."""
    if v is None:
        return []
    s = str(v).strip()
    if not s or s.lower() in ("null", "none", '""', "[]"):
        return []
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
    return [s]


def landed_floor(meta):
    """The floor an entry landed on.

    The two list-shaped fields use OPPOSITE conventions, and conflating them
    silently corrupts every distribution built on top:

      * `floor_arc: [Peace, Boredom]` — an ordered path; the LAST element is
        where the day landed, and it equals the scalar `floor`.
      * legacy `floor: [Boredom, Peace]` — [primary, secondary]; the FIRST
        element is the landed floor.

    `floor` is therefore always the source of truth for where the day landed;
    `floor_arc` only describes the path taken to get there.
    """
    values = parse_inline_list(meta.get("floor"))
    return values[0] if values else None


class Floors:
    """The vault's floor vocabulary, read once from its floor notes."""

    def __init__(self, vault_root):
        self._names = {}   # normalised name -> floor number
        self._tiers = {}   # floor number -> canonical tier
        root = Path(vault_root)
        for parts in FLOOR_NOTE_DIRS:
            folder = root.joinpath(*parts)
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.md")):
                self._absorb(path)

    def _absorb(self, path):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        meta = parse_frontmatter(text)
        raw = meta.get("floor_number")
        if raw is None:
            return                      # tier-index and non-floor notes
        try:
            number = int(str(raw).strip())
        except ValueError:
            return
        names = [path.stem]
        if meta.get("floor_name"):
            names.append(meta["floor_name"])
        names.extend(parse_inline_list(meta.get("aliases")))
        for name in names:
            key = normalise_name(name)
            if key and not _POSITIONAL_ALIAS.match(key):
                self._names.setdefault(key, number)
        tier = normalise_tier(meta.get("floor_level") or meta.get("floor_tier"))
        if tier:
            self._tiers.setdefault(number, tier)

    def __bool__(self):
        """True when at least one floor NAME loaded. Tiers are separate."""
        return bool(self._names)

    def num(self, name):
        """Floor name or alias -> number. None when unknown."""
        if not name:
            return None
        return self._names.get(normalise_name(name))

    @property
    def has_tiers(self):
        """Tiers load independently of names — a vault may have one and not the other."""
        return bool(self._tiers)

    def tier(self, number):
        """Floor number -> 'low' | 'middle' | 'high'. None when undeclared."""
        return self._tiers.get(number)

    def check(self, meta, label=""):
        """Contradictions inside ONE entry's frontmatter, as English messages.

        Catches the class of error that survives human review because each
        field looks fine alone and only the pair is wrong. Returns [] when no
        vocabulary loaded — an unknown scale cannot judge anything.
        """
        issues = []
        where = " ({})".format(label) if label else ""
        primary = landed_floor(meta)
        number = self.num(primary) if primary else None

        if self._tiers and number is not None:
            declared = self._tiers.get(number)
            entry_tier = normalise_tier(meta.get("floor_level"))
            if declared and entry_tier and entry_tier != declared:
                issues.append(
                    "floor_level '{}' but the vault declares {} ({}) as '{}'{}".format(
                        meta.get("floor_level"), primary, number, declared, where))

        arc = parse_inline_list(meta.get("floor_arc"))
        if self._names and arc and primary and normalise_name(arc[-1]) != normalise_name(primary):
            issues.append("floor_arc ends at '{}' but floor says '{}'{}".format(
                arc[-1], primary, where))

        declared_num = meta.get("floor_num")
        if declared_num and number is not None:
            try:
                if int(str(declared_num).strip()) != number:
                    issues.append("floor_num says {} but {} is {}{}".format(
                        declared_num, primary, number, where))
            except ValueError:
                pass

        if self._names and primary and number is None:
            issues.append("floor '{}' is not in the vault's floor scale{}".format(
                primary, where))

        return issues
