#!/usr/bin/env python3
"""
Auto-wikilink misfire audit.

Finds path-form wikilinks ([[folder/Name]]) that should be bare ([[Name]])
because a canonical root-level note exists with that basename.

This fixes misfires left by the v1 auto-wikilink script, which wrote full
folder paths as wikilink targets instead of bare filenames.

Usage:
  python3 "⚙️ Meta/scripts/wikilink_misfire_audit.py"              # report only
  python3 "⚙️ Meta/scripts/wikilink_misfire_audit.py" --fix        # apply rewrites
  python3 "⚙️ Meta/scripts/wikilink_misfire_audit.py" --fix --dry-run  # preview

Run order (full wikilink cleanup pass):
  1. python3 "⚙️ Meta/scripts/wikilink_misfire_audit.py" --fix
     Cleans path-form misfires first. Run BEFORE auto-wikilink --all.
  2. python3 "⚙️ Meta/scripts/auto-wikilink.py" --all --dry-run
     Preview new bare links. Check for aggressive multi-word title matches.
  3. python3 "⚙️ Meta/scripts/auto-wikilink.py" --all
     Apply.
"""

import os
import re
import sys
import datetime
from collections import defaultdict

VAULT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXCLUDED_SCAN_DIRS = {
    "graphify-out", "graphify-input", ".obsidian", ".git", ".claude",
    "_archive", "Archive", "Templates",
}

# Pattern: [[some/path/Name]] or [[some/path/Name|Display]]
PATH_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+/[^\[\]|]+?)(?:\|([^\[\]]*?))?\]\]")

# Files we skip when writing fixes
SKIP_WRITE_PATTERNS = ["GRAPH_REPORT", "Knowledge Graph Report", "graphify-out"]

# Notes whose path-form alias links should be STRIPPED rather than fixed.
# When a path-form link like [[folder/X|display]] exists and "display" is
# semantically wrong as an alias for X (e.g. a generic word that the v1 script
# incorrectly linked to a concept note), add X's basename (lowercase) here.
# The fix will revert these to plain display text instead of [[X|display]].
#
# Example: if your vault has [[🌱 Curiosities/Hosting|venue]] everywhere and
# "venue" should NOT link to the Hosting concept note, add "hosting" here.
#
# Leave empty if you want all path-form alias links to be fixed to bare form.
WRONG_ALIAS_BASENAMES: set = set()


def should_skip_dir(dirpath: str) -> bool:
    parts = dirpath.replace("\\", "/").split("/")
    for part in parts:
        if part in EXCLUDED_SCAN_DIRS:
            return True
    return False


def collect_all_files() -> list:
    result = []
    for root, dirs, files in os.walk(VAULT):
        rel = os.path.relpath(root, VAULT)
        if should_skip_dir(rel):
            dirs[:] = []
            continue
        dirs[:] = [d for d in sorted(dirs) if not d.startswith(".") and d not in EXCLUDED_SCAN_DIRS]
        for fname in files:
            if fname.endswith(".md"):
                result.append(os.path.join(root, fname))
    return result


def build_canonical_index() -> dict:
    """Walk the full vault and build {lowercase_basename -> [full_paths]}."""
    index: dict[str, list] = defaultdict(list)
    for fpath in collect_all_files():
        name = os.path.basename(fpath)[:-3]  # strip .md
        index[name.lower()].append(fpath)
    return index


def get_file_folder(fpath: str) -> str:
    return os.path.basename(os.path.dirname(fpath))


def is_internal_link(src_fpath: str, link_target: str) -> bool:
    """True if the link target folder matches the source file's own folder.
    e.g. a file inside '🤖 AI Chats/' linking to '[[🤖 AI Chats/Note]]' is internal.
    """
    src_folder = get_file_folder(src_fpath)
    first_segment = link_target.split("/")[0].strip()
    return src_folder == first_segment


def detect_misfires(files: list, canonical_index: dict):
    """Scan files for path-form wikilinks. Returns misfires grouped by basename."""
    misfire_map: dict[str, dict] = {}

    for fpath in files:
        rel = os.path.relpath(fpath, VAULT)
        if any(pat in rel for pat in SKIP_WRITE_PATTERNS):
            continue

        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            continue

        for m in PATH_WIKILINK_RE.finditer(content):
            target = m.group(1).strip()   # e.g. "🤖 AI Chats/Note"
            display = m.group(2)          # e.g. display text or None
            full_link = m.group(0)        # e.g. "[[🤖 AI Chats/Note]]"

            if is_internal_link(fpath, target):
                continue

            basename = target.split("/")[-1].strip()
            basename_lower = basename.lower()

            # Only flag as misfire if a canonical note exists for this basename
            if basename_lower not in canonical_index:
                continue

            if basename_lower not in misfire_map:
                candidates = canonical_index[basename_lower]
                candidates_sorted = sorted(candidates, key=lambda p: (len(os.path.relpath(p, VAULT).split("/")), p))
                misfire_map[basename_lower] = {
                    "canonical_paths": candidates_sorted,
                    "occurrences": []
                }
            misfire_map[basename_lower]["occurrences"].append((fpath, full_link, target, display, basename))

    return misfire_map


def apply_fixes(misfire_map: dict, dry_run: bool = False) -> dict:
    """Rewrite path-form wikilinks to bare form. Returns {fpath: count_replaced}.

    For basenames in WRONG_ALIAS_BASENAMES: if the display text differs from
    the basename, strip the link entirely and keep only the display text.
    e.g. [[folder/Hosting|venue]] -> venue  (if "hosting" is in WRONG_ALIAS_BASENAMES)

    All other path-form links: fix the path, preserve the alias.
    e.g. [[folder/Name|Display]] -> [[Name|Display]]
    e.g. [[folder/Name]] -> [[Name]]
    """
    file_fixes: dict[str, list] = defaultdict(list)

    for basename_lower, info in misfire_map.items():
        for fpath, full_link, target, display, basename in info["occurrences"]:
            is_wrong_alias = (
                display
                and display.lower() != basename.lower()
                and basename_lower in WRONG_ALIAS_BASENAMES
            )
            if is_wrong_alias:
                replacement = display  # strip link, keep display text as plain text
            elif display:
                replacement = f"[[{basename}|{display}]]"
            else:
                replacement = f"[[{basename}]]"
            file_fixes[fpath].append((full_link, replacement))

    files_changed = {}
    for fpath, fixes in file_fixes.items():
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            print(f"  ERROR reading {fpath}: {e}")
            continue

        new_content = content
        count = 0
        for old, new in fixes:
            if old in new_content:
                count += new_content.count(old)
                new_content = new_content.replace(old, new)

        if new_content != content and count > 0:
            files_changed[fpath] = count
            if not dry_run:
                try:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(new_content)
                except Exception as e:
                    print(f"  ERROR writing {fpath}: {e}")

    return files_changed


def generate_report(misfire_map: dict, files_changed: dict, dry_run: bool, fix_mode: bool) -> str:
    today = datetime.date.today().isoformat()
    lines = []
    lines.append("---")
    lines.append("type: report")
    lines.append(f"creationDate: {today}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Auto-Wikilink Misfire Audit — {today}")
    lines.append("")

    total_occurrences = sum(len(v["occurrences"]) for v in misfire_map.values())
    total_files = len(set(fpath for v in misfire_map.values() for fpath, *_ in v["occurrences"]))

    lines.append(f"**Total misfires detected:** {total_occurrences}")
    lines.append(f"**Unique basenames affected:** {len(misfire_map)}")
    lines.append(f"**Files containing misfires:** {total_files}")

    if fix_mode:
        total_fixed = sum(files_changed.values())
        lines.append(f"**Total replacements {'(would be) ' if dry_run else ''}applied:** {total_fixed} across {len(files_changed)} files")
    lines.append("")

    lines.append("## Root-cause analysis")
    lines.append("")
    lines.append("The v1 `auto-wikilink.py` script wrote full folder paths as wikilink targets "
                 "(e.g. `[[🤖 AI Chats/Note]]`) instead of bare filenames. The v2 script has a "
                 "hard guard (`if '/' in canonical: continue`) that prevents NEW misfires but "
                 "does not retroactively fix files written by v1.")
    lines.append("")

    lines.append("## Top misfired canonicals")
    lines.append("")

    sorted_misfires = sorted(misfire_map.items(), key=lambda x: len(x[1]["occurrences"]), reverse=True)

    for i, (basename_lower, info) in enumerate(sorted_misfires[:10]):
        occs = info["occurrences"]
        canonical_paths = info["canonical_paths"]
        best = os.path.relpath(canonical_paths[0], VAULT) if canonical_paths else "?"
        display_name = occs[0][4] if occs else basename_lower

        lines.append(f"### {i+1}. [[{display_name}]] ({len(occs)} misfires)")
        lines.append(f"- **Canonical note:** `{best}`")

        by_path: dict[str, int] = defaultdict(int)
        for _, full_link, target, _, _ in occs:
            by_path[full_link] += 1
        for link, count in sorted(by_path.items(), key=lambda x: -x[1]):
            lines.append(f"- `{link}` — {count} occurrence(s)")
        lines.append("")

    if len(sorted_misfires) > 10:
        lines.append(f"### ... and {len(sorted_misfires) - 10} more basenames (see full list below)")
        lines.append("")

    lines.append("## All misfires by basename")
    lines.append("")
    for basename_lower, info in sorted_misfires:
        occs = info["occurrences"]
        display_name = occs[0][4] if occs else basename_lower
        canonical_paths = info["canonical_paths"]
        best = os.path.relpath(canonical_paths[0], VAULT) if canonical_paths else "?"
        status = "FIXED" if fix_mode and not dry_run else ("WOULD FIX" if dry_run else "PENDING")
        lines.append(f"- **[[{display_name}]]** — {len(occs)} occurrence(s) — canonical: `{best}` — {status}")
    lines.append("")

    lines.append("## Intentional path-form wikilinks (not fixed)")
    lines.append("")
    lines.append("Path-form wikilinks where no canonical note exists for the basename are left "
                 "untouched — they are likely intentional path-disambiguators (e.g. two notes "
                 "with the same name in different folders).")
    lines.append("")
    lines.append("*(Run with `--show-skipped` to list them)*")

    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    fix_mode = "--fix" in args
    dry_run = "--dry-run" in args

    print(f"=== Wikilink Misfire Audit {'(DRY-RUN) ' if dry_run else ''}===")
    print(f"Vault: {VAULT}")
    print()

    print("Collecting files...")
    files = collect_all_files()
    print(f"  {len(files)} .md files found")

    print("Building canonical note index...")
    canonical_index = build_canonical_index()
    print(f"  {len(canonical_index)} unique basenames indexed")

    print("Scanning for path-form wikilinks...")
    misfire_map = detect_misfires(files, canonical_index)

    total_occurrences = sum(len(v["occurrences"]) for v in misfire_map.values())
    total_files_affected = len(set(fpath for v in misfire_map.values() for fpath, *_ in v["occurrences"]))
    print(f"  Found {total_occurrences} path-form misfires across {total_files_affected} files")
    print(f"  {len(misfire_map)} unique basenames affected")

    if total_occurrences == 0:
        print("\nNo misfires found. Vault is clean.")
        return

    sorted_misfires = sorted(misfire_map.items(), key=lambda x: len(x[1]["occurrences"]), reverse=True)
    print("\n--- Top misfires by count ---")
    for basename_lower, info in sorted_misfires[:10]:
        occs = info["occurrences"]
        display_name = occs[0][4] if occs else basename_lower
        by_path: dict[str, int] = defaultdict(int)
        for _, full_link, *_ in occs:
            by_path[full_link] += 1
        top_link = max(by_path, key=by_path.get)
        print(f"  [{len(occs):3d}] {display_name:30s} → {top_link}")

    files_changed = {}
    if fix_mode or total_occurrences < 10:
        if total_occurrences < 10 and not fix_mode:
            print(f"\nFewer than 10 misfires — applying inline fix automatically.")
            fix_mode = True
        print(f"\n{'Dry-run — previewing' if dry_run else 'Applying'} fixes...")
        files_changed = apply_fixes(misfire_map, dry_run=dry_run)
        total_replaced = sum(files_changed.values())
        prefix = "Would replace" if dry_run else "Replaced"
        print(f"  {prefix} {total_replaced} wikilinks across {len(files_changed)} files")

    if total_occurrences >= 10:
        today = datetime.date.today().isoformat()
        report_path = os.path.join(VAULT, "⚙️ Meta", "Reports", f"auto-wikilink-misfire-audit-{today}.md")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        report_content = generate_report(misfire_map, files_changed, dry_run, fix_mode)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_content)
        print(f"\nReport written to: {os.path.relpath(report_path, VAULT)}")

    print("\nDone.")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
