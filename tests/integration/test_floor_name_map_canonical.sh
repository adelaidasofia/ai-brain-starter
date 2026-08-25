#!/usr/bin/env bash
# Test: scripts/extractors/_floors.py mirrors the pinned canonical floor table.
#
# floors.md says it plainly: "Nothing in this repo hand-maintains a second
# floor list." Yet the journal extractor shipped its own — the pre-expansion
# 17-level Hawkins map (Shame=1 … Enlightenment=17), English only — while the
# vendored canonical table has 34 floors with English AND Spanish names. So
# `floor: Hope` scored 9 in one place and 20 in the framework, `floor: Trust`
# scored nothing at all, and every Spanish name scored nothing. _floors.py is
# now the ONE translation, and it is a copy of the vendored table (the
# extractors are symlinked into a vault's own scripts/ folder, where vendor/ is
# absent, so it cannot parse the table at runtime). A copy drifts unless
# something checks it: this does, on every change.
#
# Asserts:
#   1. Every English and Spanish name in vendor/high-rise/floors.md maps to its
#      floor number in _floors.FLOOR_NAME_TO_NUM (case-insensitively).
#   2. Every accented Spanish name also resolves without its accents
#      (`floor: Alegria` typed on a US keyboard still counts).
#   3. The map names no floor number outside the canonical set — nothing
#      invented, nothing left over from the 17-level scale.
#   4. floor_num_from_name: case-insensitive, list → lowest, unknown → None.
#   5. floor_num_from_fm: the NAME beats a stored (possibly old-scale) number;
#      the number is used only when there is no translatable name.
#   Negative control: a mutated copy of the map (one wrong number, one missing
#   name) is caught by the same comparison — the guard is load-bearing.
#
# Self-contained, read-only. Exit 0 = pass, 1 = fail.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TABLE="$REPO_ROOT/vendor/high-rise/floors.md"
MOD="$REPO_ROOT/scripts/extractors/_floors.py"
for f in "$TABLE" "$MOD"; do
  if [ ! -f "$f" ]; then
    echo "ERROR: $f not found" >&2
    exit 1
  fi
done

python3 - "$REPO_ROOT" <<'PY'
import os, re, sys, unicodedata
repo = sys.argv[1]
sys.path.insert(0, os.path.join(repo, "scripts", "extractors"))
from _floors import FLOOR_NAME_TO_NUM, floor_num_from_name, floor_num_from_fm

def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

# Parse the canonical table: rows shaped `| 13 | Fear | Miedo | Low | ... |`
canon = {}   # num -> (en, es)
for line in open(os.path.join(repo, "vendor/high-rise/floors.md"), encoding="utf-8"):
    m = re.match(r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", line)
    if m:
        canon[int(m.group(1))] = (m.group(2), m.group(3))
if len(canon) != 34 or set(canon) != set(range(1, 35)):
    print(f"ERROR: expected 34 canonical rows numbered 1..34, parsed {len(canon)}: {sorted(canon)}", file=sys.stderr)
    sys.exit(1)

def compare(mapping):
    """Return the list of drift problems between `mapping` and the canonical table."""
    problems = []
    for num, (en, es) in canon.items():
        for name in (en, es):
            got = mapping.get(name.strip().lower())
            if got != num:
                problems.append(f"{name!r} -> {got!r}, canonical {num}")
        plain = strip_accents(es).lower()
        if plain != es.lower() and mapping.get(plain) != num:
            problems.append(f"accent-stripped {plain!r} -> {mapping.get(plain)!r}, canonical {num}")
    extra = sorted(set(mapping.values()) - set(canon))
    if extra:
        problems.append(f"map names floor numbers not in the canonical table: {extra}")
    return problems

failed = 0
problems = compare(FLOOR_NAME_TO_NUM)
for p in problems:
    print(f"FAIL [drift] {p}", file=sys.stderr)
failed += len(problems)

# 4. floor_num_from_name semantics
checks = [
    (floor_num_from_name("Hope"), 20, "English name, title case"),
    (floor_num_from_name("esperanza"), 20, "Spanish name, lower case"),
    (floor_num_from_name("ALEGRÍA"), 33, "upper case with accent"),
    (floor_num_from_name("Alegria"), 33, "accent dropped"),
    (floor_num_from_name(["Miedo", "Esperanza"]), 13, "list -> lowest floor"),
    (floor_num_from_name("Enlightenment"), None, "17-level-only name -> None, never guessed"),
    (floor_num_from_name("no such floor"), None, "unknown -> None"),
    (floor_num_from_name(None), None, "missing -> None"),
    (floor_num_from_name(31), None, "non-string -> None"),
]
for got, want, label in checks:
    if got != want:
        print(f"FAIL [floor_num_from_name] {label}: got {got!r}, want {want!r}", file=sys.stderr)
        failed += 1

# 5. floor_num_from_fm precedence
fm_checks = [
    ({"floor": "Fear", "floor_num": 5}, 13, "name beats a stored old-scale number"),
    ({"floor": "Entusiasmo"}, 31, "name only"),
    ({"floor_num": 12}, 12, "stored number when there is no name"),
    ({"floor": "custom", "floor_num": 12}, 12, "stored number when the name is unknown"),
    ({"floor": "custom"}, None, "unknown name, no number -> None"),
    ({"floor_num": True}, None, "bool is not a floor number"),
    ({}, None, "nothing -> None"),
]
for fm, want, label in fm_checks:
    got = floor_num_from_fm(fm)
    if got != want:
        print(f"FAIL [floor_num_from_fm] {label}: got {got!r}, want {want!r}", file=sys.stderr)
        failed += 1

# Negative control: the comparison must catch drift.
mutated = dict(FLOOR_NAME_TO_NUM)
mutated["hope"] = 9            # the old Hawkins number
del mutated["entusiasmo"]      # a dropped Spanish name
caught = compare(mutated)
if len(caught) < 2:
    print(f"FAIL [negative control] mutated map raised {len(caught)} problem(s), expected >= 2: {caught}", file=sys.stderr)
    failed += 1

if failed:
    print(f"FAIL: {failed} assertion(s) failed", file=sys.stderr)
    sys.exit(1)
print(f"PASS: _floors.py mirrors all 34 canonical floors (en + es, accents optional); name beats stored number; negative control caught {len(caught)} planted drifts")
PY
