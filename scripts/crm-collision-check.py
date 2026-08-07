#!/usr/bin/env python3
"""
crm-collision-check.py — warns when a candidate CRM filename would collide
with an existing file's name or alias.

Usage:
    python3 crm-collision-check.py "<Candidate Name>"

Environment:
    VAULT_ROOT — vault path (defaults to current working directory)
    CRM_DIR    — CRM folder name relative to vault (defaults to "👤 CRM")

Exit codes:
    0 — safe to create (no collision found, OR candidate file already exists = update)
    1 — collision detected (filename matches an existing file's alias, or is a prefix of an existing fuller name)
    2 — script error

Collision types detected:
    1. Alias collision:   candidate name is listed in `aliases: [...]` of an existing CRM file
    2. Prefix collision:  candidate is a single-word name that is a prefix of an existing fuller name
                          (e.g., "Alex" would collide with "Alex Rivera")
    3. Exact name exists: candidate filename already exists (NOT a collision, treated as update)

Intended consumer: an LLM workflow or a hookify rule that wants to dedupe
CRM cards before creating a new one.
"""
import sys, os, re, pathlib

VAULT = os.environ.get("VAULT_ROOT", os.getcwd())
CRM_DIRNAME = os.environ.get("CRM_DIR", "👤 CRM")
CRM_DIR = pathlib.Path(VAULT) / CRM_DIRNAME


def parse_frontmatter_aliases(text: str) -> list[str]:
    """Extract aliases list from frontmatter. Supports both inline [a, b, c] and block form."""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return []
    fm = m.group(1)
    inline = re.search(r"^aliases:\s*\[(.*?)\]", fm, re.MULTILINE)
    if inline:
        return [x.strip().strip('"').strip("'") for x in inline.group(1).split(",") if x.strip()]
    block = re.search(r"^aliases:\s*\n((?:\s+-\s*[^\n]+\n?)+)", fm, re.MULTILINE)
    if block:
        return [re.sub(r"^\s*-\s*", "", line).strip().strip('"').strip("'")
                for line in block.group(1).splitlines() if line.strip()]
    return []


def check(candidate: str) -> tuple[int, str]:
    """Return (exit_code, message)."""
    candidate = candidate.strip().replace(".md", "")
    if not candidate:
        return 2, "empty candidate name"

    if not CRM_DIR.is_dir():
        return 2, f"CRM directory not found at {CRM_DIR} (override with CRM_DIR env var)"

    # Fast path: file with this exact name already exists, this is an update.
    target = CRM_DIR / f"{candidate}.md"
    if target.exists():
        return 0, f"`{candidate}.md` already exists. This is an update, not a create. No collision check needed."

    alias_hits = []
    prefix_hits = []
    candidate_is_single_word = len(candidate.split()) == 1

    for f in sorted(CRM_DIR.glob("*.md")):
        stem = f.stem
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        aliases = parse_frontmatter_aliases(text)
        for a in aliases:
            if a.lower() == candidate.lower():
                alias_hits.append((a, stem))
        if candidate_is_single_word and stem.startswith(candidate + " "):
            prefix_hits.append(stem)

    if not alias_hits and not prefix_hits:
        return 0, f"No collision found for `{candidate}.md`. Safe to create."

    lines = [f"COLLISION for `{candidate}.md`:"]
    if alias_hits:
        for alias, owner in alias_hits:
            lines.append(f"  - `{alias}` is an alias of existing file `{owner}.md`")
    if prefix_hits:
        for owner in prefix_hits:
            lines.append(f"  - `{candidate}` is a single-word prefix of existing file `{owner}.md` (likely same person)")
    lines.append("")
    lines.append("Before writing, ASK the user:")
    lines.append(f"  - Is `{candidate}` the same person as one of the above?")
    lines.append(f"  - If yes: update the existing file (or add `{candidate}` as an alias in its frontmatter).")
    lines.append(f"  - If no: disambiguate the filename (e.g., `{candidate} <LastName>.md`) before creating.")
    return 1, "\n".join(lines)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: crm-collision-check.py \"<Candidate Name>\"", file=sys.stderr)
        return 2
    code, msg = check(sys.argv[1])
    print(msg)
    return code


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
