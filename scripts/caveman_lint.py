#!/usr/bin/env python3
"""
caveman_lint.py — flag prose drift in runbooks + lessons + rules.

Checks numbered lessons (**N.** ... or N. ...) against caveman rules:
  - word count per lesson bullet (default max 60)
  - sentence count per bullet (default max 3)
  - prose markers that usually signal drift

Exit 0 = clean. Exit 1 = drift found. Prints a report to stdout.

Usage:
  caveman_lint.py [files...]            # check specific files
  caveman_lint.py --default             # check the three graphify runbooks
  caveman_lint.py --max-words 50 ...    # stricter word limit
  caveman_lint.py --fix-suggest ...     # print compressed stub suggestions
"""

import argparse
import re
import sys
from pathlib import Path

PROSE_MARKERS = [
    r"\bthe reason (is|was)\b",
    r"\bunfortunately\b",
    r"\bfortunately\b",
    r"\bit turns out (that )?\b",
    r"\bmoreover\b",
    r"\badditionally\b",
    r"\bin other words\b",
    r"\bwhat this means (is )?\b",
    r"\bthis is because\b",
    r"\bit's worth noting\b",
    r"\bthat being said\b",
    r"\bto be clear\b",
    r"\bin practice\b",
    r"\bmore specifically\b",
    r"\bgoing forward\b",
    r"\bthat said\b",
]

LESSON_RE = re.compile(r"^\s*\*\*(\d+[a-z]?)\.\*\*\s+", re.MULTILINE)
BOLD_RE = re.compile(r"\*\*(.*?)\*\*", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^|\]]+\|)?([^\]]+)\]\]")
CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
MARKDOWN_BOLD = re.compile(r"\*\*([^*]+)\*\*")
URL_RE = re.compile(r"https?://\S+")

DEFAULT_TARGETS = [
    "skills/graphify/SKILL.md",
    "skills/graphify/RUNBOOK.md",
    "skills/graphify/LESSONS.md",
    "skills/graphify/OPTIMIZATIONS.md",
    "CLAUDE.md",
]


def strip_for_counting(text: str) -> str:
    t = CODE_BLOCK_RE.sub(" ", text)
    t = INLINE_CODE_RE.sub(" ", t)
    t = URL_RE.sub(" ", t)
    t = WIKILINK_RE.sub(lambda m: m.group(2), t)
    t = MARKDOWN_BOLD.sub(r"\1", t)
    return t


SECTION_BOUNDARY = re.compile(r"\n\n(?:#{1,6} |---)")


def split_lessons(md: str):
    """Yield (num, start, end, body) for each **N.** ... block.

    Body ends at the next **N.** marker OR the next section heading/hr
    (whichever is first), so headings don't bleed in."""
    matches = list(LESSON_RE.finditer(md))
    for i, m in enumerate(matches):
        num = m.group(1)
        start = m.end()
        next_lesson = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        boundary = SECTION_BOUNDARY.search(md, pos=start)
        end = min(next_lesson, boundary.start() if boundary else next_lesson)
        body = md[start:end].rstrip()
        yield num, start, end, body


def count_words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def count_sentences(text: str) -> int:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return 0
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", text)
    return len([s for s in sentences if s.strip()])


def find_prose_markers(text: str):
    hits = []
    for pat in PROSE_MARKERS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            hits.append((pat, m.group(0), m.start()))
    return hits


def lint_file(path: Path, max_words: int, max_sentences: int) -> list:
    try:
        md = path.read_text()
    except Exception as e:
        return [("FILE_READ_ERROR", 0, str(e))]

    findings = []
    for num, start, end, body in split_lessons(md):
        stripped = strip_for_counting(body)
        words = count_words(stripped)
        sentences = count_sentences(stripped)
        line_no = md[:start].count("\n") + 1

        if words > max_words:
            findings.append(("WORDS", line_no, f"#{num}: {words}w (cap {max_words})"))
        if sentences > max_sentences:
            findings.append(("SENTENCES", line_no, f"#{num}: {sentences} sentences (cap {max_sentences})"))
        markers = find_prose_markers(body)
        if markers:
            summary = ", ".join(sorted({hit[1].lower() for hit in markers})[:3])
            findings.append(("PROSE_MARKER", line_no, f"#{num}: {summary}"))
    return findings


def main():
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="*")
    p.add_argument("--default", action="store_true", help="Check the 3 default graphify runbooks")
    p.add_argument("--vault-root", default=".", help="Vault root (for --default)")
    p.add_argument("--max-words", type=int, default=60)
    p.add_argument("--max-sentences", type=int, default=6)
    p.add_argument("--quiet-if-clean", action="store_true")
    args = p.parse_args()

    vault = Path(args.vault_root).resolve()
    files = [Path(f) for f in args.files]
    if args.default or not files:
        files.extend((vault / t) for t in DEFAULT_TARGETS)

    files = [f for f in files if f.exists()]
    if not files:
        print("caveman_lint: no files to check", file=sys.stderr)
        return 2

    total = 0
    for f in files:
        findings = lint_file(f, args.max_words, args.max_sentences)
        if not findings:
            if not args.quiet_if_clean:
                print(f"OK  {f}")
            continue
        rel = f.relative_to(vault) if f.is_absolute() and str(f).startswith(str(vault)) else f
        print(f"DRIFT {rel}  ({len(findings)} findings)")
        for kind, line, msg in findings:
            print(f"  {kind:14} {str(rel)}:{line}  {msg}")
        total += len(findings)

    if total:
        print(f"\ncaveman_lint: {total} drift findings across {len(files)} file(s)")
        return 1
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
