"""Render the routing-umbrella map for the install this session is running on.

WHY THIS DERIVES INSTEAD OF HARDCODING. The map this replaces was a string
literal naming 13 umbrellas, living in a machine-local repo with no remote, so
it never reached a client install at all. The obvious fix -- copy the literal
into the substrate -- is wrong three ways, and each is its own bug class:

  1. SCOPE. Most umbrellas are private/paid skills a free install does not
     have. A hardcoded map routes the user to a dozen skills that are not on
     their machine. The map's NAME claims to describe THIS session's routing
     surface; a literal describes the AUTHOR's surface instead.
  2. BOUNDARY. Hardcoding the paid catalogue into a public MIT repo publishes
     that inventory.
  3. DRIFT. The literal already disagreed with two other hardcoded copies of
     the same fact -- one said 14, one still routed a skill deprecated seven
     weeks earlier. A third copy would have made that worse.

So the substrate ships the MECHANISM and each skill carries its own
DECLARATION. A skill is an umbrella when its SKILL.md frontmatter says
`umbrella: true`. Nothing here enumerates skill names, which is what keeps it
correct on an install whose skill set we have never seen.

FRONTMATTER CONTRACT (all optional except `umbrella`):
    umbrella: true            marks this skill as a routing umbrella
    umbrella_group: <text>    heading it renders under (default "Skills")
    umbrella_domain: <text>   the one-line "what it covers" (default: first
                              sentence of `description`)
    umbrella_order: <int>     sort key, lower first (default 50). A GROUP is
                              ordered by its earliest member, so group order is
                              declared by the skills too -- this file knows no
                              group name in advance.

WHY A _lib MODULE AND NOT A WIRED HOOK. footprint-budgets.json caps
SessionStart fan-out at 19 and the event measured exactly 19. That budget's own
`_budget_rationale` directs the next addition to optimize the fan-out rather
than raise it again. session-start-context.py is already wired and already pays
for the interpreter, and the umbrella map IS session-start context, so it folds
in there for zero added fan-out (measured: footprint-sla-check --gate, 19/19,
exit 0). Living in _lib/ also keeps it directly testable.

HONEST SCOPE. This runs once per session start and scales with the user's skill
count, which makes it a concurrency multiplier: N simultaneous sessions means N
simultaneous scans. So it is bounded on purpose -- two `iterdir` levels (never
os.walk/rglob), one bounded read per candidate, AND a wall-clock budget across
the whole scan. It also COUNTS what it could not read and says so in the
header, because a map that silently shrinks is a map that lies about its scope.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

try:
    from _lib.safe_read import safe_read_text
except ImportError:  # direct import when hooks/_lib is already on sys.path
    from safe_read import safe_read_text  # type: ignore[no-redef]

DEFAULT_GROUP = "Skills"
DEFAULT_ORDER = 50
SCAN_BUDGET_S = 2.0   # whole-scan wall clock, not per file
PER_READ_S = 0.5      # safe_read's own default is 5s; N files x 5s is not a bound
# A YAML block-scalar indicator means the value is on the FOLLOWING lines, so
# the key's own line carries only the indicator. Treat it as "no value" rather
# than rendering a literal ">" as a skill's domain.
BLOCK_SCALARS = {">", "|", ">-", "|-", ">+", "|+", ">>"}


def default_skills_dir() -> Path:
    return Path.home() / ".claude" / "skills"


def _strip_comment(value: str) -> str:
    """Drop an unquoted trailing `# comment`, the way real YAML does.

    Without this, `umbrella: true  # routing umbrella` parses to
    `"true  # routing umbrella"`, which is not truthy -- a syntactically valid
    declaration the map ignores, failing identically to "not an umbrella".
    """
    if not value or value[:1] in ("'", '"'):
        return value
    for sep in (" #", "\t#"):
        head, found, _ = value.partition(sep)
        if found:
            value = head
    return value.strip()


def parse_frontmatter(text: str) -> dict:
    """Minimal `key: value` YAML-frontmatter reader.

    Deliberately NOT a YAML parser. scripts/ci.sh installs PyYAML only under
    GITHUB_ACTIONS, so a hook cannot REQUIRE it; the sibling hooks that import
    yaml all guard it and degrade to a skip, and for this map a skip renders
    nothing -- indistinguishable from an install with no umbrellas.

    Requires a CLOSING delimiter. Without that check an unclosed frontmatter
    scans to EOF, and a SKILL.md that DOCUMENTS this very contract inside a
    fenced code block would promote itself and inherit the example's group.
    """
    text = text.lstrip("﻿").lstrip()  # BOM, or a blank line before ---
    if not text.startswith("---"):
        return {}
    out: dict = {}
    closed = False
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if stripped in ("---", "..."):
            closed = True
            break
        # Top-level keys only. An indented line belongs to a nested structure
        # this does not model, and treating it as top-level would let a nested
        # `umbrella:` promote an unrelated skill into the map.
        if line[:1] in (" ", "\t") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not key:
            continue
        value = _strip_comment(value.strip())
        if value in BLOCK_SCALARS:
            value = ""
        out[key] = value.strip("\"'")
    return out if closed else {}


def first_sentence(text: str, limit: int = 96) -> str:
    """First sentence of a description, clipped. Used when a skill declares
    `umbrella: true` but gives no `umbrella_domain`."""
    text = " ".join(text.split())
    for stop in (". ", " - ", "; "):
        head, sep, _ = text.partition(stop)
        if sep:
            text = head
            break
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("true", "yes", "1", "on")


def _skill_md_candidates(skills_dir: Path) -> list:
    """Every place a SKILL.md legitimately lives, FLAT ENTRIES FIRST.

    TWO LAYOUTS, both real, and missing the second is the whole bug this module
    exists to fix. A skills repo linked into ~/.claude/skills puts each skill
    FLAT (`<root>/<name>/SKILL.md`). A cloned skill BUNDLE keeps its own tree,
    so its skills sit NESTED (`<root>/<bundle>/skills/<name>/SKILL.md`).
    Measured: a maintainer box has BOTH, so a flat-only scan passes there and
    renders NOTHING on a substrate-only install -- exactly the client case.

    TWO PASSES, not one interleaved walk. The dedup below is first-wins, and
    the flat copy is the one the harness actually loads, so every flat entry
    must be enumerated before any nested one. Interleaving silently inverts
    that whenever a bundle directory sorts before a flat skill of the same
    name -- measured on a real tree: 49 colliding names, 46 resolving to the
    nested copy, including this module's own vault-system.

    Still bounded: two `iterdir` levels, no os.walk / rglob.
    """
    try:
        entries = sorted(skills_dir.iterdir())
    except OSError:
        return []
    def _dirs(paths) -> list:
        # DIRECTORIES only. A skills tree legitimately contains loose files --
        # measured: five `.zip` archives beside the skill dirs of one bundle --
        # and `<archive>.zip/SKILL.md` reads as an ERROR, which the unreadable
        # counter would then report as five broken skills on a healthy machine.
        # A guard that cries on correct state is one people learn to ignore.
        out = []
        for x in paths:
            try:
                if x.is_dir():
                    out.append(x)
            except OSError:
                continue
        return out

    flat = [(e.name, e / "SKILL.md") for e in _dirs(entries)]
    nested: list = []
    for e in _dirs(entries):
        sub = e / "skills"
        try:
            if sub.is_dir():
                nested.extend((s.name, s / "SKILL.md") for s in _dirs(sorted(sub.iterdir())))
        except OSError:
            continue
    return flat + nested


def _read_skill(path: Path, timeout: float):
    """Bounded read that also tolerates a SKILL.md which is ITSELF a symlink.

    safe_read opens with O_NOFOLLOW and rejects a non-regular final component,
    so a symlinked SKILL.md comes back `not-regular` and would be skipped
    identically to "not declared" -- silent. Symlinked skill DIRECTORIES are
    fine either way; only the final component is constrained.
    """
    result = safe_read_text(path, errors="replace", timeout=timeout)
    if not result.ok and getattr(result, "status", "") == "not-regular":
        try:
            if path.is_symlink():
                target = path.resolve(strict=True)
                if target.is_file():
                    return safe_read_text(target, errors="replace", timeout=timeout)
        except OSError:
            pass
    return result


def collect_umbrellas(skills_dir: Path) -> tuple:
    """Return (umbrellas, unreadable_count).

    The count is not decoration. safe_read's per-file deadline is 5s and there
    is no budget across a scan, so a stalled cloud-sync folder could block
    session start for minutes AND -- if every read fails -- return an empty
    list that is indistinguishable from a genuine "nothing declared". A scan
    that ran out of budget or hit unreadable files must be able to say so.
    """
    found: list = []
    seen: set = set()
    unreadable = 0
    deadline = time.monotonic() + SCAN_BUDGET_S
    for entry_name, skill_md in _skill_md_candidates(skills_dir):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            unreadable += 1
            continue
        result = _read_skill(skill_md, timeout=min(PER_READ_S, remaining))
        if not result.ok:
            # "missing" is the overwhelmingly common case (a dir with no
            # SKILL.md) and is NOT a failure; anything else is.
            if getattr(result, "status", "") != "missing":
                unreadable += 1
            continue
        fm = parse_frontmatter(result.text or "")
        if not _truthy(fm.get("umbrella", "")):
            continue
        name = fm.get("name") or entry_name
        if name in seen:
            continue  # first wins, and flat is enumerated first by construction
        seen.add(name)
        try:
            order = int(fm.get("umbrella_order", DEFAULT_ORDER))
        except ValueError:
            order = DEFAULT_ORDER
        found.append({
            "name": name,
            "group": fm.get("umbrella_group") or DEFAULT_GROUP,
            "domain": fm.get("umbrella_domain") or first_sentence(fm.get("description", "")),
            "order": order,
        })
    return found, unreadable


def render(umbrellas: list, unreadable: int = 0) -> str:
    groups: dict = {}
    for u in umbrellas:
        groups.setdefault(str(u["group"]), []).append(u)
    ordered = sorted(
        groups.items(),
        key=lambda kv: (min(int(u["order"]) for u in kv[1]), kv[0]),
    )
    count = len(umbrellas)
    noun = "umbrella" if count == 1 else "umbrellas"
    header = f"[skill-umbrellas] {count} routing {noun} available this session."
    if unreadable:
        # State the scope rather than let the count imply completeness.
        header += f" ({unreadable} skill file(s) unreadable, so this list may be short.)"
    lines = [header, ""]
    for group, members in ordered:
        lines.append(f"{group}:")
        for u in sorted(members, key=lambda x: (int(x["order"]), str(x["name"]))):
            domain = str(u["domain"])
            lines.append(f"  /{u['name']}" + (f" — {domain}" if domain else ""))
        lines.append("")
    lines.append(
        "Pick the umbrella whose domain matches the work. Multiple matches → chain."
    )
    return "\n".join(lines)


def render_umbrella_map(skills_dir: Path | None = None) -> str | None:
    """The map, or None when there is nothing honest to say.

    Three states kept distinct even though all render nothing, because
    collapsing them is how a failed read starts reading as an empty answer:
      - bypassed           -> None
      - no skills dir      -> None  (fresh install; nothing to route yet)
      - dir with 0 marked  -> None  (a real zero, not a failure)
    A scan that found nothing but COULD NOT READ anything is the fourth state,
    and it renders a header saying so rather than masquerading as the third.
    """
    if _truthy(os.environ.get("UMBRELLA_MAP_BYPASS", "")):
        return None
    try:
        root = skills_dir or default_skills_dir()
        if not root.is_dir():
            return None
        umbrellas, unreadable = collect_umbrellas(root)
        if not umbrellas:
            return render([], unreadable) if unreadable else None
        return render(umbrellas, unreadable)
    except Exception:
        # A SessionStart payload is not worth breaking a session over.
        return None
