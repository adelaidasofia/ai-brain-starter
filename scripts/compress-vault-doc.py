#!/usr/bin/env python3
"""
compress-vault-doc.py — caveman-form compression pass for vault docs.

Pure-Python regex pass that performs the SAFE cuts (drop "Codified YYYY-MM-DD
after X" sentences, drop bare PR/commit URLs, collapse multi-blank lines,
drop "Why: <prose>" appendages on rules already self-evident from the rule
body, etc.) and surfaces the rest for human review.

What this script DOES auto-cut:
  1. Trailing "Codified YYYY-MM-DD ..." sentences inside rule bodies
  2. Bare PR/issue links of shape `PR [text](https://github.com/.../pull/N)`
     when they appear inside a paragraph that already names the rule
  3. Multi-paragraph "[Name] YYYY-MM-DD verbatim:" quote blocks
  4. Repeated multi-paragraph rationale where a one-liner survives
  5. Multiple consecutive blank lines collapsed to one
  6. Trailing whitespace on every line

What it WILL NOT touch (preserved byte-for-byte):
  - Code fences (```...```)
  - Inline code (`...`)
  - Wikilinks ([[...]])
  - File paths (anything starting with /, ~/, ./, ⚙️, 📓, etc., or matching
    *.{md,py,sh,ts,tsx,json})
  - URLs that aren't a github.com/.../pull/N or .../issues/N or .../commit/HASH
  - YAML frontmatter (--- ... ---)
  - Tables (| ... | rows)
  - Headers (# ## ### lines)

Output:
  Backup at <file>.original.md (only if backup doesn't already exist).
  Overwrites the original with the compressed version.
  Prints byte-count delta + line-count delta.

Dry-run mode (--dry-run) writes the compressed output to <file>.compressed.md
and the original is untouched.

Usage:
  VAULT_ROOT=/path/to/vault python3 scripts/compress-vault-doc.py CLAUDE.md
  VAULT_ROOT=/path/to/vault python3 scripts/compress-vault-doc.py --dry-run "rules/efficiency.md"
  VAULT_ROOT=/path/to/vault python3 scripts/compress-vault-doc.py --auto-from-drift

Inspired by JuliusBrussee/caveman's /caveman:compress. The Python regex pass
is conservative: it preserves frontmatter, code fences, inline code, wikilinks,
tables, and emoji-prefixed paths byte-for-byte. Only safe text-prose cuts are
applied (codified-stamp lines, verbatim-quote attributions, PR/issue URL
parens, blank-line collapse, trailing whitespace).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", str(_SCRIPT_DIR.parent)))


# ─── Regex patterns (run in order) ──────────────────────────────────────

# 1. "Codified YYYY-MM-DD after ..." trailing sentence inside paragraphs
#    (preserves the rule body, drops the historical attribution).
#    Matches a sentence-end period, then "Codified" through the next end-of-paragraph.
PATTERN_CODIFIED = re.compile(
    r"(?<=[.!?])\s+(?:Originally\s+)?[Cc]odified\s+(?:on\s+)?\[?\d{4}-\d{2}-\d{2}\]?[^.\n]*"
    r"(?:\.[^.\n]*)*?\.(?=\s|$|\n)",
    re.MULTILINE,
)

# 2. Bare commit hash + author lines from auto-merge comments (rare in docs but bloating)
PATTERN_COAUTHOR = re.compile(r"^Co-Authored-By: Claude.*$\n?", re.MULTILINE)

# 3. "[NAME] YYYY-MM-DD verbatim: \"...\"" quote attributions inside rule bodies.
# Configure VERBATIM_NAMES for the names you author rules under (e.g. "She", "Her",
# "User", or your name). Default catches third-person pronouns; add personal
# tokens to the alternation to scrub more aggressively.
VERBATIM_NAMES = ["She", "Her", "User", "The user"]
PATTERN_VERBATIM_QUOTE = re.compile(
    r'(?:' + '|'.join(VERBATIM_NAMES) + r')\s+\d{4}-\d{2}-\d{2}\s+verbatim:\s*"[^"]*"\s*\.?',
    re.MULTILINE,
)

# 4. PR/issue/commit URLs inline with the parenthesized markdown form
#    matches: " (PR [text](https://github.com/foo/bar/pull/N))"
#         or " (PR [diazroa-concierge#74](https://github.com/.../pull/74))"
PATTERN_PR_URL_PAREN = re.compile(
    r"\s*\(?\s*PR\s+\[[^\]]+\]\(https://github\.com/[^)]+/(?:pull|issues|commit)/[^)]+\)\s*\)?\.?",
    re.MULTILINE,
)

# 5. Multiple blank lines → one
PATTERN_BLANK = re.compile(r"\n{3,}")

# 6. Trailing whitespace
PATTERN_TRAILING = re.compile(r"[ \t]+$", re.MULTILINE)

# Protect zones (replace with sentinel, restore at end)
SENTINEL_FMT = "\x00CMPSAFE_{}\x00"

# Code fences (multiline, lazy)
RE_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
# Inline code
RE_INLINE_CODE = re.compile(r"`[^`\n]+`")
# Wikilinks
RE_WIKILINK = re.compile(r"\[\[[^\]]+\]\]")
# YAML frontmatter (only at start of file)
RE_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
# Tables (consecutive lines starting with |)
RE_TABLE = re.compile(r"(?:^\|.*$\n?)+", re.MULTILINE)


def protect(text: str) -> tuple[str, list[str]]:
    """Replace protected zones with sentinels. Return modified text + list of originals."""
    bag: list[str] = []

    def stash(m):
        bag.append(m.group(0))
        return SENTINEL_FMT.format(len(bag) - 1)

    # Order matters: frontmatter first, then code fences, then tables, then wikilinks, then inline code
    text = RE_FRONTMATTER.sub(stash, text, count=1)
    text = RE_CODE_FENCE.sub(stash, text)
    text = RE_TABLE.sub(stash, text)
    text = RE_WIKILINK.sub(stash, text)
    text = RE_INLINE_CODE.sub(stash, text)
    return text, bag


def restore(text: str, bag: list[str]) -> str:
    # Iterate until no sentinels remain. Nested protections (wikilink inside
    # inline-code block) need multiple passes because restoring the outer
    # sentinel re-exposes the inner one.
    for _ in range(len(bag) + 2):  # bounded iteration: at most len(bag)+2 passes
        any_replaced = False
        for i, original in enumerate(bag):
            sentinel = SENTINEL_FMT.format(i)
            if sentinel in text:
                text = text.replace(sentinel, original)
                any_replaced = True
        if not any_replaced:
            break
    return text


def compress(text: str) -> tuple[str, dict]:
    """Run the safe compression passes. Return compressed text + stats dict."""
    stats = {
        "codified_dropped": 0,
        "verbatim_dropped": 0,
        "pr_urls_dropped": 0,
        "coauthor_dropped": 0,
        "blank_collapsed": 0,
        "trailing_stripped": 0,
    }

    # Protect zones first
    protected, bag = protect(text)

    # Run regex passes
    new, n = PATTERN_CODIFIED.subn("", protected)
    stats["codified_dropped"] = n
    protected = new

    new, n = PATTERN_VERBATIM_QUOTE.subn("", protected)
    stats["verbatim_dropped"] = n
    protected = new

    new, n = PATTERN_PR_URL_PAREN.subn("", protected)
    stats["pr_urls_dropped"] = n
    protected = new

    new, n = PATTERN_COAUTHOR.subn("", protected)
    stats["coauthor_dropped"] = n
    protected = new

    new, n = PATTERN_BLANK.subn("\n\n", protected)
    stats["blank_collapsed"] = n
    protected = new

    new, n = PATTERN_TRAILING.subn("", protected)
    stats["trailing_stripped"] = n
    protected = new

    # Restore zones
    return restore(protected, bag), stats


def process_file(path: Path, dry_run: bool = False) -> dict:
    """Compress one file. Return stats dict including before/after sizes."""
    if not path.exists():
        return {"error": f"not found: {path}"}
    if not path.is_file():
        return {"error": f"not a file: {path}"}
    if path.suffix not in (".md", ".markdown", ".txt"):
        return {"error": f"only .md/.markdown/.txt supported, got {path.suffix}"}

    original = path.read_text(encoding="utf-8")
    compressed, stats = compress(original)

    before_bytes = len(original.encode("utf-8"))
    after_bytes = len(compressed.encode("utf-8"))
    before_lines = original.count("\n") + 1
    after_lines = compressed.count("\n") + 1

    stats.update(
        {
            "path": str(path),
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "saved_bytes": before_bytes - after_bytes,
            "saved_pct": (before_bytes - after_bytes) / before_bytes * 100 if before_bytes else 0,
            "before_lines": before_lines,
            "after_lines": after_lines,
        }
    )

    if dry_run:
        out_path = path.with_suffix(path.suffix + ".compressed")
        out_path.write_text(compressed, encoding="utf-8")
        stats["wrote"] = str(out_path)
    else:
        # Backup original ONCE (never overwrite an existing backup)
        backup = path.with_name(path.stem + ".original" + path.suffix)
        if not backup.exists():
            backup.write_text(original, encoding="utf-8")
            stats["backup"] = str(backup)
        else:
            stats["backup"] = f"(existing) {backup}"
        path.write_text(compressed, encoding="utf-8")
        stats["wrote"] = str(path)

    return stats


def auto_from_drift() -> list[Path]:
    """Read Drift Audit.md and extract files flagged for review (>30KB and high-edit)."""
    drift_path = VAULT_ROOT / "⚙️ Meta" / "Drift Audit.md"
    if not drift_path.exists():
        print(f"Drift Audit not found at {drift_path}. Run drift-detection.py first.", file=sys.stderr)
        return []

    text = drift_path.read_text(encoding="utf-8")
    # Find lines like "- `path/to/file.md` — N edits, M.M KB ..."
    candidates: list[Path] = []
    for m in re.finditer(r"-\s+`([^`]+\.md)`\s+—\s+\d+\s+edits,\s+([\d.]+)\s+KB", text):
        rel_path = m.group(1)
        size_kb = float(m.group(2))
        if size_kb >= 30:  # only files >= 30KB
            full = VAULT_ROOT / rel_path
            if full.exists() and full.is_file():
                candidates.append(full)
    return candidates


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1] if __doc__ else "")
    ap.add_argument("paths", nargs="*", help="files to compress (relative to vault root or absolute)")
    ap.add_argument("--dry-run", action="store_true", help="write to <file>.compressed.md, don't touch original")
    ap.add_argument(
        "--auto-from-drift",
        action="store_true",
        help="compress every file flagged by Drift Audit (>=30KB)",
    )
    args = ap.parse_args()

    paths: list[Path] = []
    if args.auto_from_drift:
        paths.extend(auto_from_drift())
        if not paths:
            print("(no files >=30KB in Drift Audit)")
            return
    if args.paths:
        for p in args.paths:
            full = Path(p) if Path(p).is_absolute() else VAULT_ROOT / p
            paths.append(full)

    if not paths:
        ap.print_help()
        return

    total_saved = 0
    total_before = 0
    for path in paths:
        stats = process_file(path, dry_run=args.dry_run)
        if "error" in stats:
            print(f"✗ {path}: {stats['error']}", file=sys.stderr)
            continue
        total_saved += stats["saved_bytes"]
        total_before += stats["before_bytes"]
        print(
            f"✓ {path.name}: {stats['before_bytes']:,} → {stats['after_bytes']:,} bytes "
            f"({stats['saved_bytes']:,} saved, {stats['saved_pct']:.1f}%)"
        )
        ops = []
        if stats["codified_dropped"]:
            ops.append(f"{stats['codified_dropped']} codified-stamps")
        if stats["verbatim_dropped"]:
            ops.append(f"{stats['verbatim_dropped']} verbatim-quotes")
        if stats["pr_urls_dropped"]:
            ops.append(f"{stats['pr_urls_dropped']} PR-URLs")
        if stats["coauthor_dropped"]:
            ops.append(f"{stats['coauthor_dropped']} co-author lines")
        if stats["blank_collapsed"]:
            ops.append(f"{stats['blank_collapsed']} blank-runs")
        if ops:
            print(f"   dropped: {', '.join(ops)}")
        if not args.dry_run and "backup" in stats and not stats["backup"].startswith("(existing)"):
            print(f"   backup: {Path(stats['backup']).name}")

    if len(paths) > 1:
        pct = total_saved / total_before * 100 if total_before else 0
        print(f"\nTotal: {total_before:,} → {total_before - total_saved:,} bytes ({total_saved:,} saved, {pct:.1f}%)")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
