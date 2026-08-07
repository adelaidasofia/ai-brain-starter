#!/usr/bin/env python3
"""Generate one stub note per Floor + tier-index notes for the public substrate.

The `daily-journal` skill tags every entry with wikilinks like `[[Fear]]` and
`[[Low Floors]]`. Without per-floor notes, those wikilinks resolve to empty
bare-string nodes in the graph. This script emits 34 floor stubs + 3 tier-index
notes at `floors/<Name>.md`, plus the writing-series pointer note.

The canonical floor data (the 34-floor table, elevator emotions, shadow twins,
EN + ES) is NOT hardcoded here. It is PARSED from the vendored, pinned copy of
the open-source High-Rise framework at `vendor/high-rise/floors.md`
(see vendor/high-rise/README.md). ai-brain-starter consumes that upstream; it
does not keep its own divergent floor list. To change the floors, change them in
`Fundacion-Lontananza/high-rise`, run `scripts/sync-high-rise.py`, then re-run
this script. `scripts/test_generate_floor_stubs.py` fails if `floors/` drifts
from this generator's output.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "floors"
CANONICAL = ROOT / "vendor" / "high-rise" / "floors.md"

SUBSTACK_EN = "https://adelaidadiazroa.substack.com"
SUBSTACK_ES = "https://perspectivasblog.substack.com"

# Tier presentation (note titles + blurbs) is this substrate's own framing; the
# tier BOUNDARIES and each floor's tier come from the canonical table below.
TIER_INDEX = {
    "Low":    ("Low Floors",    "Pisos Bajos",    "Reactive: 1-18. The reactive tier. Where the charge lives.",      1, 18),
    "Middle": ("Middle Floors", "Pisos Medios",   "Transitional: 19-24. The transitional tier. The climb up.",       19, 24),
    "High":   ("High Floors",   "Pisos Altos",    "Generative: 25-34. The generative tier. Where the energy gives.", 25, 34),
}

# A member reference inside an elevator/shadow line: "Grief (10)", "Love (29)".
_MEMBER_RE = re.compile(r"([A-Z][A-Za-z]+)\s*\((\d+)\)")
# A floors-table data row: | 13 | Fear | Miedo | Low | anxiety, ... |
_FLOOR_ROW_RE = re.compile(
    r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(Low|Middle|High)\s*\|\s*(.+?)\s*\|\s*$"
)


def _canon_die(msg: str) -> None:
    print(
        f"generate_floor_stubs: ERROR parsing {CANONICAL.relative_to(ROOT)}: {msg}\n"
        f"  Is the vendored High-Rise framework present + current? "
        f"Run: python3 scripts/sync-high-rise.py",
        file=sys.stderr,
    )
    sys.exit(2)


def _english_section(text: str, header: str) -> str:
    """Return the body of the first `## <header>` section (English half only).

    The canonical file has an English half then a `---` then a Spanish half whose
    sub-sections are level-3 (`### ...`). Matching the level-2 `## <header>`
    exactly keeps us in the English half.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == f"## {header}":
            start = i + 1
            break
    if start is None:
        _canon_die(f"missing '## {header}' section")
    body = []
    for line in lines[start:]:
        if line.startswith("## ") or line.strip() == "---":
            break
        body.append(line)
    return "\n".join(body)


def load_canonical() -> tuple[list, dict, list]:
    """Parse FLOORS, ELEVATORS, SHADOWS from the vendored canonical floors.md."""
    if not CANONICAL.exists():
        _canon_die("file not found")
    text = CANONICAL.read_text(encoding="utf-8")

    # --- floors table (the whole file has exactly one; rows are 1..34) --------
    floors = []
    for line in text.splitlines():
        m = _FLOOR_ROW_RE.match(line)
        if m:
            num, en, es, tier, energy = m.groups()
            floors.append((int(num), en.strip(), es.strip(), tier, energy.strip()))
    if len(floors) != 34:
        _canon_die(f"expected 34 floor rows, parsed {len(floors)}")
    nums = [f[0] for f in floors]
    if nums != list(range(1, 35)):
        _canon_die(f"floor numbers are not 1..34 in order: {nums}")

    # --- elevator emotions ----------------------------------------------------
    elevators = {}
    for line in _english_section(text, "Elevator emotions").splitlines():
        line = line.strip()
        if not line.startswith("- **"):
            continue
        name_m = re.match(r"-\s*\*\*(.+?)\*\*", line)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        members = [(int(n), fn) for fn, n in _MEMBER_RE.findall(line)]
        elevators[name] = members  # members may be [] (e.g. Overwhelm = any floor)

    # --- shadow twins ---------------------------------------------------------
    shadows = []  # (low_num, low_name, high_num|None, high_name, tell)
    for line in _english_section(text, "Shadow twins").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 3:
            continue
        low_cell, high_cell, tell = cells
        if low_cell.lower().startswith("shadow") or set(low_cell) <= {"-", ":"}:
            continue  # header / separator row
        low_m = _MEMBER_RE.search(low_cell)
        if not low_m:
            continue
        low_name, low_num = low_m.group(1), int(low_m.group(2))
        high_m = _MEMBER_RE.search(high_cell)
        if high_m:
            high_name, high_num = high_m.group(1), int(high_m.group(2))
        else:
            # True twin can be a non-floor concept (e.g. Confidence) — no number.
            high_name, high_num = high_cell.strip(), None
        shadows.append((low_num, low_name, high_num, high_name, tell))
    if not shadows:
        _canon_die("parsed zero shadow-twin rows")

    return floors, elevators, shadows


FLOORS, ELEVATORS, SHADOWS = load_canonical()
FLOOR_NAME = {num: en for num, en, *_ in FLOORS}
SHADOW_BY_LOW = {low: (hi, hi_name, tell) for low, _ln, hi, hi_name, tell in SHADOWS}
SHADOW_BY_HIGH = {hi: (low, low_name, tell)
                  for low, low_name, hi, _hn, tell in SHADOWS if hi is not None}


def elevators_for(num: int) -> list[tuple[str, list[tuple[int, str]]]]:
    """Elevators this floor is a member of (elevators with no members are skipped)."""
    return [(name, members) for name, members in ELEVATORS.items()
            if any(m_num == num for m_num, _ in members)]


def floor_body(num: int, en: str, es: str, tier: str, energy: str) -> str:
    elevators = elevators_for(num)

    shadow_block = ""
    if num in SHADOW_BY_LOW:
        high_num, high_name, tell = SHADOW_BY_LOW[num]
        # The true twin may be a floor (link it) or a non-floor concept (plain).
        twin = f"[[{high_name}]] ({high_num})" if high_num is not None else f"**{high_name}**"
        check = f"[[{high_name}]]" if high_num is not None else high_name
        shadow_block = dedent(f"""
            ## Shadow twin

            **{en} ({num})** is the shadow of {twin}.

            > {tell}

            When {en} shows up easily, check whether {check} is what you actually mean. The difference is whether you are still bracing.
        """).strip() + "\n\n"
    elif num in SHADOW_BY_HIGH:
        low_num, low_name, tell = SHADOW_BY_HIGH[num]
        shadow_block = dedent(f"""
            ## Shadow twin

            **[[{low_name}]] ({low_num})** is the shadow of **{en} ({num})**.

            > {tell}

            Most "{en}" people claim is actually [[{low_name}]]. The difference is whether you are still bracing.
        """).strip() + "\n\n"

    elevator_block = ""
    if elevators:
        lines = []
        for elev_name, members in elevators:
            parts = []
            for m_num, m_name in members:
                if m_num == num:
                    parts.append(f"**{m_name}**")
                else:
                    parts.append(f"[[{m_name}]]")
            lines.append(f"- **{elev_name}** = " + " + ".join(parts))
        elevator_block = (
            "## Elevators involving this floor\n\n" + "\n".join(lines)
            + "\n\nThese are movements between floors, not floors themselves. "
            "The journal tags the floor you land on; the elevator name describes the trip.\n\n"
        )

    tier_name = TIER_INDEX[tier][0]

    body = dedent(f"""\
        ---
        type: floor
        floor_number: {num}
        floor_name: {en}
        floor_level: {tier}
        aliases: [{es}, "Floor {num}", "Piso {num}"]
        ---

        # {en} · {es}

        **Floor {num} · {tier} tier**

        {energy[0].upper() + energy[1:]}.

        Part of the {tier.lower()} tier: see [[{tier_name}]].

        """)

    if elevator_block:
        body += elevator_block

    if shadow_block:
        body += shadow_block

    body += dedent(f"""\
        ## How to use this floor

        - **Name it.** "I'm on {en}," not "I'm in {en}." The naming reduces the charge.
        - **Look for the elevator.** What carried you here? A conversation, a meal, a meeting, a thought. The elevator is the actual lesson.
        - **Check the shadow twin** (above) if there is one.

        ## Narrative form

        The 34-floor framework started as a writing project. Each floor gets a lived-experience chapter in [[The High-Rise Series]]:

        - English: [{SUBSTACK_EN}]({SUBSTACK_EN})
        - Español: [{SUBSTACK_ES}]({SUBSTACK_ES})

        ## Reference

        See [[floors|All 34 floors]] for the canonical list. The framework is MIT-licensed and lives in the public substrate.
        """)

    return body


def tier_body(tier: str) -> str:
    en, es, blurb, lo, hi = TIER_INDEX[tier]
    rows = [f"- {n}. [[{f_en}]] ({f_es}) — {energy}" for n, f_en, f_es, t, energy in FLOORS if t == tier]
    return dedent(f"""\
        ---
        type: floor-tier
        tier: {tier}
        aliases: [{es}]
        ---

        # {en} · {es}

        {blurb}

        Floors **{lo}-{hi}**:

        """) + "\n".join(rows) + dedent(f"""

        ## Reference

        See [[floors|All 34 floors]] for the full canonical list with energies, Spanish names, elevator emotions, and shadow twins.
        """)


SERIES_BODY = dedent(f"""\
    ---
    type: writing-project
    aliases: ["High-Rise", "The High-Rise", "La Torre"]
    ---

    # The High-Rise Series

    A lived-experience writing project: each of the 34 floors gets its own chapter. Not theory. Not a self-help ladder. One person walking the building she lives in.

    Read the series:

    - English: [{SUBSTACK_EN}]({SUBSTACK_EN})
    - Español: [{SUBSTACK_ES}]({SUBSTACK_ES})

    The framework underneath the chapters (canonical 34-floor list, energies, Spanish names, tiers, elevator emotions, shadow twins) lives at [[floors|floors]]. Every floor note links back here.

    The framework is MIT-licensed and lives in the public substrate. The chapters are the author's writing and live behind their own license on Substack.
    """)


def main():
    OUT.mkdir(exist_ok=True)
    written = []
    for num, en, es, tier, energy in FLOORS:
        path = OUT / f"{en}.md"
        path.write_text(floor_body(num, en, es, tier, energy), encoding="utf-8")
        written.append(str(path.relative_to(ROOT)))
    for tier in ("Low", "Middle", "High"):
        en, _, _, _, _ = TIER_INDEX[tier]
        path = OUT / f"{en}.md"
        path.write_text(tier_body(tier), encoding="utf-8")
        written.append(str(path.relative_to(ROOT)))
    series_path = OUT / "The High-Rise Series.md"
    series_path.write_text(SERIES_BODY, encoding="utf-8")
    written.append(str(series_path.relative_to(ROOT)))
    print(f"Wrote {len(written)} files from {CANONICAL.relative_to(ROOT)}:")
    for p in written:
        print(f"  {p}")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
