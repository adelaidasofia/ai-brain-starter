#!/usr/bin/env python3
"""Auto-wikilink v2 — vault-scope-aware, regex-safe.

Scans markdown files for unlinkified mentions of vault concepts and adds
wikilinks. Replaces the v1 script which had three critical bugs:
  1. Wrote path-form wikilinks ([[🌱 Curiosities/Colombia]]) instead of bare
     filenames. The display in Obsidian then showed the path string as the
     visible text, leaking personal vault folder names into team-vault files.
  2. Reached across the team-vault symlink, linking team-vault files to
     personal-vault concept notes. Violated the team-vault firewall rule.
  3. The substitution regex over-matched and ate adjacent characters when
     emoji or special characters were near a match (e.g. "A VP at BigCo"
     became "Curiosities/CountryBigCo]]").

v2 fixes all three:
  - Vault-scope-aware: a file inside `🚀 Onde Team/` only links to terms
    whose source files are also inside `🚀 Onde Team/`. Personal context is
    invisible to team files. ONE-WAY FIREWALL.
  - Bare filenames or alias syntax only. The script REFUSES to write any
    wikilink that contains '/' in the target. Hard guard.
  - Region-tracking substitution: builds a list of [[...]] regions in the
    body first, then only modifies text strictly outside those regions.
    Recalculates after every change. Kills the character-eating bug.
  - Hard-blocks team-vault files unless --allow-team is passed explicitly.
    Default behavior is "personal vault only." You have to opt in to touch
    team-vault content.

Usage:
  # Default: process journals only
  python3 "⚙️ Meta/scripts/auto-wikilink.py"

  # Entire vault (journals + AI Chats + writing + notes + everywhere)
  python3 "⚙️ Meta/scripts/auto-wikilink.py" --all

  # Dry run first to preview changes
  python3 "⚙️ Meta/scripts/auto-wikilink.py" --all --dry-run

  # Specific files
  python3 "⚙️ Meta/scripts/auto-wikilink.py" file1.md file2.md

  # Include team-vault files (uses team-vault terms only, strict firewall)
  python3 "⚙️ Meta/scripts/auto-wikilink.py" --all --allow-team

Maintenance runbook:
  CORRECT ORDER for a full wikilink cleanup pass:
    1. python3 "⚙️ Meta/scripts/wikilink_misfire_audit.py" --fix
       Cleans path-form misfires first ([[folder/Name]] → [[Name]]).
       Run this BEFORE auto-wikilink so you're not adding bare links
       on top of path-form ones that will get cleaned anyway.
    2. python3 "⚙️ Meta/scripts/auto-wikilink.py" --all --dry-run
       Preview: check for aggressive multi-word title matches (a note
       titled "Seven Companies Seven Lessons" will match "seven companies"
       anywhere via alias scanning). Review before applying.
    3. python3 "⚙️ Meta/scripts/auto-wikilink.py" --all
       Apply.

  Known quirks:
  - Notes whose titles are full phrases (not single concepts) create
    aggressive alias matches. Before a --all run, scan PERSONAL_CONCEPT_DIRS
    for phrase-title notes and consider excluding or renaming them.
  - Files can vanish between the walk and the read (e.g. git-deleted stubs).
    The script handles this with a try/except — missing files are skipped.
  - WRONG_ALIAS_BASENAMES in wikilink_misfire_audit.py controls which notes'
    alias-form path links get STRIPPED (display text kept, link removed) vs
    fixed to bare form. Set it to the basenames of notes whose v1 alias links
    were semantically wrong for your vault.
"""
import os
import re
import sys

VAULT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Folders never scanned for TERMS (concept notes live elsewhere; AI chats are not canonical)
EXCLUDED_TERM_DIRS = {
    "🤖 AI Chats", "AI Chats", "graphify-input", "graphify-out",
    "_archive", "Archive", ".obsidian", ".git", ".claude", "Templates",
}

# Folders never written to during --all walk (system/read-only; AI Chats IS writable)
EXCLUDED_PROCESSING_DIRS = {
    "graphify-input", "graphify-out",
    "_archive", "Archive", ".obsidian", ".git", ".claude", "Templates",
}

# Keep a single alias for the term-scanning path (used in _discover_subdirs + load_terms)
EXCLUDED_DIR_NAMES = EXCLUDED_TERM_DIRS


def _detect_team_vault(vault_path: str) -> str:
    """Find the team vault (if any).

    Priority:
      1. AI_BRAIN_TEAM_VAULT env var (relative subfolder name within the vault)
      2. Any symlinked directory at the vault root (the convention multi-vault
         setups use to mount a shared team vault into the personal vault)
      3. Legacy hardcoded '🚀 Onde Team' fallback (kept so existing Onde team
         vault setups keep working without reconfiguring)

    Returns an absolute path to the team vault, or "" if none exists.
    """
    override = os.environ.get("AI_BRAIN_TEAM_VAULT", "").strip()
    if override:
        cand = os.path.join(vault_path, override)
        return cand if os.path.isdir(cand) else ""
    if os.path.isdir(vault_path):
        for name in sorted(os.listdir(vault_path)):
            p = os.path.join(vault_path, name)
            if os.path.islink(p) and os.path.isdir(p):
                return p
    legacy = os.path.join(vault_path, "🚀 Onde Team")
    return legacy if os.path.isdir(legacy) else ""


def _find_journal_dir(vault_path: str) -> str:
    """Locate the journals folder. Tries common names; falls back to emoji form."""
    for name in ("📓 Journals", "Journals", "📔 Journal", "Journal"):
        p = os.path.join(vault_path, name)
        if os.path.isdir(p):
            return p
    return os.path.join(vault_path, "📓 Journals")


def _discover_subdirs(parent: str) -> list:
    """List immediate subdirectories of `parent`, skipping hidden and excluded."""
    if not parent or not os.path.isdir(parent):
        return []
    return [
        os.path.join(parent, name)
        for name in sorted(os.listdir(parent))
        if not name.startswith('.')
        and name not in EXCLUDED_DIR_NAMES
        and os.path.isdir(os.path.join(parent, name))
    ]


TEAM_VAULT_PREFIX = _detect_team_vault(VAULT)
JOURNAL_DIR = _find_journal_dir(VAULT)

# Concept directories scanned for the personal-scope term list.
# Hardcoded defaults cover the ai-brain-starter standard folder layout;
# missing directories are silently skipped by `load_terms_for_scope`.
# Users with custom folder names can set AI_BRAIN_PERSONAL_CONCEPT_DIRS
# to a colon-separated list of subfolder names relative to the vault root.
_personal_override = os.environ.get("AI_BRAIN_PERSONAL_CONCEPT_DIRS", "").strip()
if _personal_override:
    PERSONAL_CONCEPT_DIRS = [
        os.path.join(VAULT, p.strip()) for p in _personal_override.split(":") if p.strip()
    ]
else:
    PERSONAL_CONCEPT_DIRS = [
        os.path.join(VAULT, "✍️ Writing", "The High-Rise", "Floors"),
        os.path.join(VAULT, "✍️ Writing"),
        os.path.join(VAULT, "🧠 Psychology"),
        os.path.join(VAULT, "Psychology"),
        os.path.join(VAULT, "📝 Notes"),
        os.path.join(VAULT, "Notes"),
        os.path.join(VAULT, "🌱 Curiosities"),
        os.path.join(VAULT, "👤 CRM"),
        os.path.join(VAULT, "CRM"),
        os.path.join(VAULT, "💼 Business"),
        os.path.join(VAULT, "Business"),
    ]

# Concept directories scanned for the team-vault scope term list.
# Auto-discovers all non-hidden, non-excluded immediate subdirs of the team
# vault. If user-specific subfolder names exist from the starter's
# canonical layout, they'll be picked up automatically; if the team vault
# uses different names, those are picked up too. Empty list if no team
# vault exists.
TEAM_CONCEPT_DIRS = _discover_subdirs(TEAM_VAULT_PREFIX)


def in_team_vault(filepath: str) -> bool:
    """True if `filepath` lies inside the team vault. False if no team vault exists."""
    if not TEAM_VAULT_PREFIX:
        return False
    return os.path.abspath(filepath).startswith(os.path.abspath(TEAM_VAULT_PREFIX))


def load_terms_for_scope(scope_dirs):
    """Walk the given concept directories and return {lowercase_term -> bare filename}.

    Canonical names are ALWAYS bare filenames (e.g. "Colombia"), never paths.
    Filenames containing '/' are skipped defensively. Aliases are pulled from
    YAML frontmatter on the first 20 lines of each file.
    """
    terms: dict[str, str] = {}
    for cdir in scope_dirs:
        if not os.path.exists(cdir):
            continue
        for root, dirs, files in os.walk(cdir):
            # Strip excluded subfolders in-place so os.walk skips them
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
            for fname in files:
                if not fname.endswith(".md"):
                    continue
                name = fname[:-3]
                # Defensive: never accept a name that looks like a path
                if "/" in name or len(name) < 3:
                    continue
                terms[name.lower()] = name

                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as af:
                        in_fm = False
                        for i, line in enumerate(af):
                            if i > 20:
                                break
                            if i == 0 and line.strip() == "---":
                                in_fm = True
                                continue
                            if not in_fm:
                                continue
                            if line.strip() == "---":
                                break
                            if line.startswith("aliases:"):
                                bracket_match = re.search(r"\[(.+)\]", line)
                                if bracket_match:
                                    for a in bracket_match.group(1).split(","):
                                        a = a.strip().strip("\"").strip("'")
                                        if a and len(a) > 2 and "/" not in a:
                                            terms[a.lower()] = name
                except Exception:
                    pass
    return terms


def find_wikilink_regions(text: str):
    """Return a list of (start, end) tuples for every [[...]] region in `text`."""
    regions = []
    for m in re.finditer(r"\[\[[^\[\]]*?\]\]", text):
        regions.append((m.start(), m.end()))
    return regions


def is_in_region(pos: int, regions) -> bool:
    """True if `pos` falls inside any (start, end) tuple in `regions`."""
    for start, end in regions:
        if start <= pos < end:
            return True
    return False


def already_linked_terms(text: str):
    """Return a set of lowercase canonical filenames already wikilinked in `text`.

    Defensively strips any path prefix from the link target so we don't
    re-link a name that's already present in path-form (legacy corruption).
    """
    linked = set()
    for m in re.finditer(r"\[\[([^\[\]|]+?)(?:\|[^\[\]]*?)?\]\]", text):
        target = m.group(1).strip()
        if "/" in target:
            target = target.split("/")[-1]
        linked.add(target.lower())
    return linked


def add_wikilinks(filepath: str, terms, dry_run: bool = False):
    """Add missing wikilinks to a single file. Returns (count_added, details_list)."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (FileNotFoundError, OSError):
        return 0, []

    original = content

    # Split frontmatter and body so we never modify YAML
    parts = content.split("---", 2)
    if len(parts) >= 3 and parts[0].strip() == "":
        frontmatter = "---" + parts[1] + "---"
        body = parts[2]
    else:
        frontmatter = ""
        body = content

    linked = already_linked_terms(body)
    sorted_terms = sorted(terms.items(), key=lambda x: len(x[0]), reverse=True)
    changes = []

    for term_lower, canonical in sorted_terms:
        if canonical.lower() in linked:
            continue
        if len(term_lower) < 3:
            continue

        # SAFETY: refuse to write any path-form wikilink. Hard guard.
        if "/" in canonical:
            continue

        # Recalculate wikilink regions on every iteration — body grows after edits
        regions = find_wikilink_regions(body)

        # Find first whole-word match outside any wikilink region
        pattern = r"\b" + re.escape(term_lower) + r"\b"
        match = None
        for m in re.finditer(pattern, body, re.IGNORECASE):
            if not is_in_region(m.start(), regions):
                match = m
                break
        if match is None:
            continue

        original_text = match.group(0)
        # Use alias syntax when matched text differs from canonical filename
        if original_text == canonical:
            replacement = f"[[{canonical}]]"
        else:
            replacement = f"[[{canonical}|{original_text}]]"

        body = body[: match.start()] + replacement + body[match.end():]
        linked.add(canonical.lower())
        changes.append(f"  {original_text} -> {replacement}")

    new_content = frontmatter + body if frontmatter else body
    if new_content != original:
        if not dry_run:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(new_content)
        return len(changes), changes
    return 0, []


def collect_all_files(vault_path: str, include_team: bool = False) -> list:
    """Walk the entire vault and return all .md file paths, skipping system dirs.

    Used by --all mode. AI Chats is intentionally included — files there can
    receive wikilinks even though they're excluded from term scanning.
    Team vault files are excluded unless include_team is True.
    """
    result = []
    team_prefix = os.path.abspath(TEAM_VAULT_PREFIX) if TEAM_VAULT_PREFIX else None
    for root, dirs, files in os.walk(vault_path):
        dirs[:] = [
            d for d in sorted(dirs)
            if not d.startswith(".")
            and d not in EXCLUDED_PROCESSING_DIRS
        ]
        for fname in files:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(root, fname)
            abs_fpath = os.path.abspath(fpath)
            if team_prefix and abs_fpath.startswith(team_prefix) and not include_team:
                continue
            result.append(fpath)
    return result


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    allow_team = "--allow-team" in args
    all_mode = "--all" in args
    specific_files = [a for a in args if not a.startswith("--")]

    print(f"=== auto-wikilink v2 ({'DRY-RUN' if dry_run else 'APPLY'}) ===")

    # Determine files to process
    if specific_files:
        files = [os.path.abspath(f) for f in specific_files if f.endswith(".md")]
    elif all_mode:
        files = collect_all_files(VAULT, include_team=allow_team)
        print(f"--all mode: {len(files)} .md files found in vault")
    else:
        files = []
        if os.path.exists(JOURNAL_DIR):
            for fname in os.listdir(JOURNAL_DIR):
                fpath = os.path.join(JOURNAL_DIR, fname)
                if fname.endswith(".md") and not os.path.isdir(fpath):
                    files.append(fpath)

    # FIREWALL: split files into safe + blocked based on vault scope
    personal_files, team_files, blocked = [], [], []
    for fpath in files:
        if in_team_vault(fpath):
            if allow_team:
                team_files.append(fpath)
            else:
                blocked.append(fpath)
        else:
            personal_files.append(fpath)

    if blocked:
        print(f"\n⚠️  BLOCKED {len(blocked)} team-vault files (pass --allow-team to override):")
        for b in blocked[:5]:
            print(f"  {os.path.relpath(b, VAULT)}")
        if len(blocked) > 5:
            print(f"  ... and {len(blocked) - 5} more")

    total_changes = 0
    files_modified = 0

    # Process personal-vault files with personal-vault terms only
    if personal_files:
        print("\nLoading PERSONAL-vault concept terms...")
        personal_terms = load_terms_for_scope(PERSONAL_CONCEPT_DIRS)
        print(f"  {len(personal_terms)} terms loaded")
        print(f"\nProcessing {len(personal_files)} personal-vault files...")
        for fpath in personal_files:
            count, details = add_wikilinks(fpath, personal_terms, dry_run)
            if count > 0:
                files_modified += 1
                total_changes += count
                fname = os.path.basename(fpath)
                print(f"\n{fname} ({count} links added):")
                for d in details[:5]:
                    print(d)
                if len(details) > 5:
                    print(f"  ... and {len(details) - 5} more")

    # Process team-vault files with team-vault terms only — STRICT FIREWALL
    if team_files:
        print("\nLoading TEAM-vault concept terms (firewall: NO personal vault terms)...")
        team_terms = load_terms_for_scope(TEAM_CONCEPT_DIRS)
        print(f"  {len(team_terms)} terms loaded")
        print(f"\nProcessing {len(team_files)} team-vault files...")
        for fpath in team_files:
            count, details = add_wikilinks(fpath, team_terms, dry_run)
            if count > 0:
                files_modified += 1
                total_changes += count
                fname = os.path.basename(fpath)
                print(f"\n{fname} ({count} links added):")
                for d in details[:5]:
                    print(d)
                if len(details) > 5:
                    print(f"  ... and {len(details) - 5} more")

    suffix = "DRY-RUN — " if dry_run else ""
    will = "would be " if dry_run else ""
    print(f"\n{suffix}Summary: {total_changes} wikilinks {will}added across {files_modified} files")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
