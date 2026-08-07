#!/usr/bin/env python3
"""
repo-bundle — render the repo into a single Markdown file for LLM ingestion.

A reader (human or agent) can paste one file into a Claude session and have the
whole substrate as context. Same pattern as karpathy/rendergit's LLM View,
reimplemented in our register: pure Python stdlib, no HTML, no browser-opening,
no clone-to-tmp. Walks the working tree, applies sane skip filters, emits a
fenced-code Markdown bundle with file paths as headings.

Usage:
  scripts/repo-bundle.py              # writes dist/llm-bundle.md
  scripts/repo-bundle.py -o out.md
  scripts/repo-bundle.py --include "skills/**/*.md,scripts/**/*.py"

Defaults: includes .md, .py, .sh, .ts, .tsx, .js, .json, .yml, .yaml.
Skips: .git, node_modules, dist, build, .next, .venv, __pycache__,
       binaries, files >200KB.
"""

import argparse
import fnmatch
import os
import sys
from pathlib import Path

DEFAULT_EXTS = {".md", ".py", ".sh", ".ts", ".tsx", ".js", ".jsx", ".json", ".yml", ".yaml", ".toml"}
SKIP_DIRS = {".git", "node_modules", "dist", "build", ".next", ".venv", "__pycache__", ".pytest_cache", ".turbo"}
MAX_BYTES = 200_000

LANG_MAP = {
    ".py": "python", ".sh": "bash", ".ts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "jsx", ".json": "json",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".md": "markdown",
}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="dist/llm-bundle.md", help="output path")
    ap.add_argument("--include", help="comma-separated glob filters (e.g. 'skills/**/*.md,scripts/*.py')")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    return ap.parse_args()


def should_skip(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    for part in rel.parts:
        if part in SKIP_DIRS:
            return True
    if path.suffix not in DEFAULT_EXTS:
        return True
    try:
        if path.stat().st_size > MAX_BYTES:
            return True
    except OSError:
        return True
    return False


def matches_includes(rel_path: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    return any(fnmatch.fnmatch(rel_path, p) for p in patterns)


def collect(root: Path, includes: list[str]) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if should_skip(p, root):
                continue
            rel = str(p.relative_to(root))
            if not matches_includes(rel, includes):
                continue
            found.append(p)
    found.sort()
    return found


def render(files: list[Path], root: Path) -> str:
    out_lines = [
        "# Repo bundle",
        "",
        f"Files: {len(files)}. Generated for LLM ingestion.",
        "",
    ]
    for f in files:
        rel = f.relative_to(root)
        lang = LANG_MAP.get(f.suffix, "")
        out_lines.append(f"## `{rel}`")
        out_lines.append("")
        out_lines.append(f"```{lang}")
        try:
            out_lines.append(f.read_text(encoding="utf-8", errors="replace").rstrip())
        except OSError as exc:
            out_lines.append(f"(read error: {exc})")
        out_lines.append("```")
        out_lines.append("")
    return "\n".join(out_lines)


def main() -> None:
    args = parse_args()
    root = Path(args.root).resolve()
    includes = [s.strip() for s in args.include.split(",")] if args.include else []
    files = collect(root, includes)
    print(f"[repo-bundle] {len(files)} files from {root}", file=sys.stderr)
    if not files:
        sys.exit("no files matched")
    bundle = render(files, root)
    out = Path(args.output)
    if not out.is_absolute():
        out = root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(bundle, encoding="utf-8")
    print(f"[repo-bundle] wrote {out} ({len(bundle)} bytes)", file=sys.stderr)
    print(out)


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
