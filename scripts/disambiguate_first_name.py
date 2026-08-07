#!/usr/bin/env python3
"""
disambiguate_first_name.py — surgically wikilink first-name references to canonical CRM notes.

For first names with multiple distinct people in CRM (e.g., two Alexes, three
Davids), auto-yes wikilink application is unsafe — see graphify_apply_wikilinks.py
detect_ambiguity() docstring for the underlying incident class.

This script operates on a SINGLE first name at a time. For each occurrence in
the vault, it decides:

  1. "First <KnownLastName>" → wrap as [[First LastName]] (canonical)
  2. "First <UnknownLastName>" → leave plain (different person / public figure)
  3. Bare "First" near canonical-A's markers → [[First LastA|First]]
  4. Bare "First" near canonical-B's markers → [[First LastB|First]]
  5. Bare "First" near MULTIPLE canonicals' markers → leave plain (ambiguous)
  6. Bare "First" with NO marker context → leave plain (truly ambiguous)

The script never fabricates canonicals. Pass your own configuration via the
RULES dict — each entry maps first_name → list of canonicals with markers.

The provided RULES dict is empty by default. Add your own entries based on the
people in your CRM and the era / company / role / handle markers that uniquely
identify each one. See EXAMPLE_RULES below for the structure, then copy and
edit RULES.

Codified after an auto-yes wikilink pass that applied a common first name to
~100 files spanning 6+ distinct people (some in CRM, some public figures from
book references, some not in CRM at all). The same pattern applies to any
common first name — the safe path is always: identify per-person markers
(unique surname / company / role / email handle / Instagram), then surgically
link only occurrences within window of those markers.

Usage:
    Edit RULES below, then:
    python3 disambiguate_first_name.py <first_name> [--dry-run] [--vault-root PATH]
"""

import argparse
import re
import sys
from pathlib import Path

EXCLUDE_DIRS = ("/_Archive/", "/⚙️ Meta/", "/.git/", "/.smart-env/", "/worktrees/", "/.floor-audit/")

EXISTING_LINK_RE = re.compile(r'\[\[[^\]]+\]\]')
EXCLUSION_SPANS = [
    re.compile(r'```.*?```', re.DOTALL),
    re.compile(r'`[^`\n]+`'),
    re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
    re.compile(r'https?://[^\s)\]>]+'),
    re.compile(r'(?<!\w)//[^\s)\]>]+'),
    re.compile(r'\]\([^)]*\)'),
    re.compile(r'\[\[dayone-moment:[^\]]+\]\]'),
]


def collect_protected(text: str):
    spans = []
    for pat in EXCLUSION_SPANS:
        spans.extend((m.start(), m.end()) for m in pat.finditer(text))
    spans.extend((m.start(), m.end()) for m in EXISTING_LINK_RE.finditer(text))
    return spans


def in_protected(start, end, spans):
    return any(s <= start and end <= e for s, e in spans)


# --------------------------------------------------------------------------
# RULES — extend per first name. Empty by default.
#
# Each first name maps to a config dict with:
#   canonicals: list of {link_target, markers, window} entries
#     - link_target: filename without .md, used as wikilink target (e.g., "Alex Smith")
#     - markers: list of regex patterns to find within `window` chars of bare first name
#                (e.g., r"\bSmith\b", r"\bAcme Corp\b", r"\balex@acme\b")
#     - window: char window on each side of the first-name occurrence (200-500 typical)
#   public_figure_lastnames: skip "FirstName <Lastname>" if lastname here
#                            (e.g., book authors, athletes — never link)
#   canonical_lastnames: link "FirstName <Lastname>" as [[link_target]] if lastname here
#                        (covers explicit full-name plain text)
#
# See EXAMPLE_RULES for the full structure with anonymized markers.
# --------------------------------------------------------------------------

RULES: dict = {}


# --------------------------------------------------------------------------
# EXAMPLE_RULES — illustrative, not used. Copy + edit into RULES above.
# --------------------------------------------------------------------------

EXAMPLE_RULES = {
    "Alex": {
        "canonicals": [
            {
                # Person 1: identified by surname + their company + their email handle
                "link_target": "Alex Smith",
                "markers": [
                    r"\bSmith\b",
                    r"\bAcme Corp\b",
                    r"\balex@acme\b",
                ],
                "window": 300,
            },
            {
                # Person 2: identified by surname + a project they worked on
                "link_target": "Alex Jones",
                "markers": [
                    r"\bJones\b",
                    r"\bRedesign 2024\b",
                ],
                "window": 250,
            },
        ],
        # Skip "Alex <Lastname>" entirely if lastname is one of these
        # (e.g., public-figure book authors who appear in book notes but
        # aren't personal contacts you want CRM entries for)
        "public_figure_lastnames": ["Honnold", "Trebek"],
        # Direct "Alex Lastname" → "[[Alex Lastname]]" canonical wraps
        "canonical_lastnames": {
            "Smith": "Alex Smith",
            "Jones": "Alex Jones",
        },
    },
}


def find_canonical_match(window: str, canonicals: list[dict]) -> list[str]:
    """Return list of canonical link_targets whose markers match in window."""
    hits = []
    for c in canonicals:
        for marker in c["markers"]:
            if re.search(marker, window, re.IGNORECASE):
                hits.append(c["link_target"])
                break
    return hits


def disambiguate(
    vault: Path,
    first_name: str,
    rules: dict,
    dry_run: bool = False,
) -> dict:
    """Apply disambiguation across the vault for one first name."""
    cfg = rules.get(first_name)
    if not cfg:
        sys.exit(f"No rules for '{first_name}'. Edit RULES in {__file__}.")

    canonicals = cfg["canonicals"]
    public_lastnames = set(cfg.get("public_figure_lastnames", []))
    canonical_lastnames = cfg.get("canonical_lastnames", {})

    first_re = re.compile(rf"\b{re.escape(first_name)}(?:\s+([A-ZÁÉÍÓÚÑa-záéíóúñ][a-záéíóúñ]+))?\b")
    self_files = {vault / "👤 CRM" / f"{c['link_target']}.md" for c in canonicals}

    stats = {
        "files_modified": 0,
        "canonical_wraps": 0,           # FirstName Lastname → [[FirstName Lastname]]
        "alias_wraps": {},              # link_target → count
        "skipped_public_figure": {},    # lastname → count
        "skipped_ambiguous_no_marker": 0,
        "skipped_ambiguous_multi_marker": 0,
        "files_with_partial": 0,
    }

    for md in vault.rglob("*.md"):
        p = str(md)
        if any(e in p for e in EXCLUDE_DIRS):
            continue
        if md in self_files:
            continue
        try:
            text = md.read_text()
        except Exception:
            continue

        matches = list(first_re.finditer(text))
        if not matches:
            continue

        protected = collect_protected(text)
        replacements = []  # (start, end, replacement)
        total_eligible = 0

        for m in matches:
            if in_protected(m.start(), m.end(), protected):
                continue
            total_eligible += 1
            lastname = m.group(1)

            # Rule 1+2: FirstName Lastname pattern
            if lastname:
                if lastname in canonical_lastnames:
                    target = canonical_lastnames[lastname]
                    replacements.append((m.start(), m.end(), f"[[{target}]]"))
                elif lastname in public_lastnames:
                    stats["skipped_public_figure"][lastname] = \
                        stats["skipped_public_figure"].get(lastname, 0) + 1
                # else: lastname is unknown — could be a different person we don't know
                # Skip safely.
                continue

            # Rule 3-6: bare FirstName — check window
            window_size = max(c["window"] for c in canonicals)

            # Per-canonical: each has its own window, but we use max window for slicing
            # then re-test per canonical with its specific window.
            hits = []
            for c in canonicals:
                cw = c["window"]
                cws = max(0, m.start() - cw)
                cwe = min(len(text), m.end() + cw)
                cwindow = text[cws:cwe]
                for marker in c["markers"]:
                    if re.search(marker, cwindow, re.IGNORECASE):
                        hits.append(c["link_target"])
                        break

            if len(hits) == 1:
                target = hits[0]
                replacement = f"[[{target}|{first_name}]]"
                replacements.append((m.start(), m.end(), replacement))
            elif len(hits) > 1:
                stats["skipped_ambiguous_multi_marker"] += 1
            else:
                stats["skipped_ambiguous_no_marker"] += 1

        if not replacements:
            continue

        # Apply end → start
        new_text = text
        for start, end, rep in reversed(replacements):
            new_text = new_text[:start] + rep + new_text[end:]

        if not dry_run:
            md.write_text(new_text)
        stats["files_modified"] += 1
        for _, _, rep in replacements:
            # Alias form has '|' before display, canonical form does not.
            #   canonical: [[First Last]]
            #   alias:     [[First Last|First]]
            if "|" in rep:
                target = rep[2:rep.index("|")]
                stats["alias_wraps"][target] = stats["alias_wraps"].get(target, 0) + 1
            else:
                stats["canonical_wraps"] += 1
        if len(replacements) < total_eligible:
            stats["files_with_partial"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("first_name", help="First name to disambiguate (must be in RULES)")
    parser.add_argument("--vault-root", default=".",
                        help="Vault root (default: cwd)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned changes without writing")
    args = parser.parse_args()

    vault = Path(args.vault_root).resolve()
    print(f"Disambiguating '{args.first_name}' in {vault}")
    if args.dry_run:
        print("[DRY RUN — no files will be modified]\n")

    if not RULES:
        sys.exit(
            f"RULES dict is empty. Edit {__file__} and add an entry for "
            f"'{args.first_name}'. See EXAMPLE_RULES at the top of the file "
            f"for the structure."
        )

    stats = disambiguate(vault, args.first_name, RULES, dry_run=args.dry_run)

    print(f"\nFiles modified: {stats['files_modified']}")
    print(f"  Canonical [[FirstName Lastname]] wraps: {stats['canonical_wraps']}")
    print(f"  Alias [[Target|First]] wraps:")
    for target, count in sorted(stats["alias_wraps"].items(), key=lambda x: -x[1]):
        print(f"    {target}: {count}")
    print(f"  Files with partial linking: {stats['files_with_partial']}")
    print()
    print(f"Skipped 'FirstName <KnownPublic>': "
          f"{sum(stats['skipped_public_figure'].values())}")
    for lname, count in sorted(stats["skipped_public_figure"].items(), key=lambda x: -x[1])[:10]:
        print(f"    {args.first_name} {lname}: {count}")
    print(f"Skipped bare-FirstName ambiguous (no marker): "
          f"{stats['skipped_ambiguous_no_marker']}")
    print(f"Skipped bare-FirstName ambiguous (multi marker): "
          f"{stats['skipped_ambiguous_multi_marker']}")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
