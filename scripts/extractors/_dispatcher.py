#!/usr/bin/env python3
"""
extractors/_dispatcher.py — type-aware vault metadata extraction.

Reads every markdown file's `type:` field, routes to the matching extractor
module, writes structured YAML back. Zero LLM per run (unless the user opts
into an `ai_*` field — those route to MiniMax, never Claude).

Each extractor module must expose:
  extract(filepath, body, fm_dict, context) -> ExtractionResult | None

Registry convention: module filename == type name. `book.py` handles `type: book`.
Modules starting with _ are infrastructure, not extractors.
"""
from __future__ import annotations

import argparse
import glob
import importlib
import os
import random
import sys
import time
import yaml

# Make sibling modules importable (no package installs, emoji-path-safe)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _base import (  # noqa: E402
    VAULT, SKIP_PARTS,
    parse_frontmatter, strip_auto_fields, reassemble_file,
    render_fields, get_crm_names,
)

# Types that exist in frontmatter but should NEVER be auto-extracted.
# These are infrastructure: templates, meta, runbooks, reports.
INFRASTRUCTURE_TYPES = {
    "meta", "template", "runbook", "report", "imported", "dashboard",
    "rule", "skill", "hook", "changelog",
}

# Alias map: users write whatever type feels natural; we route to a real extractor.
TYPE_ALIASES = {
    "place": "travel",
    "trip": "travel",
    "chat": "ai_chat",
    "conversation": "ai_chat",
    "speaking": "talk",
    "workshop": "talk",
    "okr": "goal",
    "vision": "goal",
    "draft": "writing_draft",
    "log": "daily_log",
    "framework": "concept",
    "analysis": "strategy",
    "tracker": "dashboard",  # treat as infra
}


def discover_extractors():
    """Auto-load every extractors/<name>.py that has an `extract` function."""
    registry = {}
    for path in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(name)
        except Exception as e:
            print(f"  ⚠ Failed to load extractor '{name}': {e}", file=sys.stderr)
            continue
        if not hasattr(mod, "extract"):
            continue
        registry[name] = mod
    return registry


def should_skip_path(path):
    """Respect global SKIP_PARTS (Meta, Archive, etc.)."""
    parts = set(path.split(os.sep))
    return bool(parts & SKIP_PARTS)


def list_vault_files(type_filter=None):
    """All .md files in vault, minus SKIP_PARTS folders."""
    pattern = os.path.join(VAULT, "**", "*.md")
    for fp in glob.glob(pattern, recursive=True):  # Rule 36
        if should_skip_path(fp):
            continue
        yield fp


def process_file(filepath, registry, context, dry_run=False, force=False):
    """Extract metadata for one file. Returns status string for logging."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return f"READ_ERR: {e}"

    fm_dict, fm_raw, body = parse_frontmatter(content)
    if fm_dict is None:
        return "NO_FRONTMATTER"

    doc_type_raw = (fm_dict.get("type") or "").strip().lower()
    if not doc_type_raw:
        return "NO_TYPE"
    if doc_type_raw in INFRASTRUCTURE_TYPES:
        return "INFRASTRUCTURE"

    # Normalize: Python modules can't have hyphens. `negotiation-prep` → `negotiation_prep`.
    doc_type = doc_type_raw.replace("-", "_")
    # Aliases: `place` → `travel`, `chat` → `ai_chat`, etc.
    doc_type = TYPE_ALIASES.get(doc_type, doc_type)
    # Aliases can route to infrastructure (e.g. tracker → dashboard): re-check.
    if doc_type in INFRASTRUCTURE_TYPES:
        return "INFRASTRUCTURE"
    extractor = registry.get(doc_type)
    if not extractor:
        return f"NO_EXTRACTOR_FOR:{doc_type}"

    # Idempotency check: is ANY of the extractor's auto fields already present?
    # Using "any" (not "first") because some first fields are optional (e.g. book_author
    # when the note has no explicit author line) — that would falsely trigger re-processing.
    auto_fields = getattr(extractor, "AUTO_FIELDS", ())
    already_processed = any(f in fm_dict for f in auto_fields)

    if already_processed and not force:
        return "SKIP_ALREADY_TAGGED"

    result = extractor.extract(filepath, body, fm_dict or {}, context)
    if result is None:
        return "EXTRACTOR_SKIPPED"

    # On --force, strip previously-written auto fields before re-appending
    fm_work = strip_auto_fields(fm_raw, result.auto_fields) if force else fm_raw

    new_block = render_fields(result.fields, result.field_order)
    if not new_block:
        return "NO_FIELDS_EMITTED"

    new_content = reassemble_file(fm_work, new_block, body, had_fm=True)

    if dry_run:
        short = os.path.relpath(filepath, VAULT)
        print(f"  [DRY] {doc_type:<18} {short}")
        return "DRY_OK"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(new_content)
    return "WROTE"


def _peek_type(filepath):
    """Read only the frontmatter header and return the type string (lowercase) or None."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            head = f.read(2048)
    except Exception:
        return None
    fm_dict, _, _ = parse_frontmatter(head)
    if not fm_dict:
        return None
    return (fm_dict.get("type") or "").strip().lower() or None


def select_sample(all_files, registry, n_per_type):
    """Pick up to n_per_type files for each registered extractor type.

    Returns a list of filepaths spanning as many registered types as possible.
    Pure function of the input set: deterministic for a given seed.
    """
    buckets = {t: [] for t in registry}
    for fp in all_files:
        t = _peek_type(fp)
        if t and t in buckets:
            buckets[t].append(fp)
    rng = random.Random(42)  # deterministic sample
    sample = []
    for t, files in buckets.items():
        if not files:
            continue
        rng.shuffle(files)
        sample.extend(files[:n_per_type])
    return sample


def main():
    ap = argparse.ArgumentParser(description="Vault-wide metadata extractor (type-aware dispatcher).")
    ap.add_argument("--dry-run", action="store_true", help="Preview, no writes.")
    ap.add_argument("--force", action="store_true", help="Re-process files already tagged.")
    ap.add_argument("--type", help="Only process files with this type.")
    ap.add_argument("--year", help="Only process files whose path contains this year string.")
    ap.add_argument("--limit", type=int, help="Cap processing at N files (for testing).")
    ap.add_argument("--sample", type=int, nargs="?", const=1, metavar="N",
                    help="Sample N files per registered type (default 1). Random per-type "
                         "pick with a fixed seed. Good for cold-start preview before a full run.")
    ap.add_argument("--progress-every", type=int, default=250, metavar="N",
                    help="Print a heartbeat every N files scanned (default 250). 0 = silent.")
    args = ap.parse_args()

    # --sample implies dry-run semantics visually, but we still write unless --dry-run.
    # We DO flip dry_run on when sampling to avoid surprising the user — sample is a preview.
    effective_dry_run = args.dry_run or args.sample is not None

    mode_tag = "(DRY RUN)" if effective_dry_run else ""
    if args.sample is not None:
        mode_tag = f"(SAMPLE: {args.sample} per type, preview only)"
    print(f"vault-metadata-extract {mode_tag}")
    print(f"Vault: {VAULT}")

    registry = discover_extractors()
    print(f"Registered extractors ({len(registry)}): {', '.join(sorted(registry.keys()))}")

    if not registry:
        print("\nNo extractors registered. Run /setup-vault-types first to choose which")
        print("document types to extract. Nothing to do.")
        return 2

    context = {
        "crm_names": get_crm_names(),
        "vault": VAULT,
    }
    print(f"CRM entries: {len(context['crm_names'])}\n")

    counters = {
        "WROTE": 0, "DRY_OK": 0, "SKIP_ALREADY_TAGGED": 0,
        "NO_TYPE": 0, "NO_FRONTMATTER": 0, "INFRASTRUCTURE": 0,
        "EXTRACTOR_SKIPPED": 0, "NO_FIELDS_EMITTED": 0,
    }
    no_extractor_types = {}
    errors = []

    # Materialize file list (needed for sample + progress total). For very large vaults,
    # this is one glob over .md files — cheap compared to per-file frontmatter parsing.
    all_files = list(list_vault_files())

    if args.sample is not None:
        scan_files = select_sample(all_files, registry, args.sample)
        print(f"Sample selected: {len(scan_files)} files across {len(registry)} types")
    else:
        scan_files = all_files

    total = len(scan_files)
    files_done = 0
    scanned = 0
    t_start = time.monotonic()
    heartbeat = args.progress_every if args.progress_every and args.progress_every > 0 else 0

    for fp in scan_files:
        scanned += 1
        if args.year and args.year not in fp:
            continue
        if args.limit and files_done >= args.limit:
            break

        # If --type filter, peek at frontmatter first
        if args.type:
            t = _peek_type(fp)
            if t != args.type:
                continue

        status = process_file(fp, registry, context,
                              dry_run=effective_dry_run, force=args.force)
        files_done += 1

        if status in counters:
            counters[status] += 1
        elif status.startswith("NO_EXTRACTOR_FOR:"):
            t = status.split(":", 1)[1]
            no_extractor_types[t] = no_extractor_types.get(t, 0) + 1
        elif status == "NO_EXTRACTOR":
            no_extractor_types["(unknown)"] = no_extractor_types.get("(unknown)", 0) + 1
        elif status.startswith("READ_ERR"):
            errors.append((fp, status))
        else:
            errors.append((fp, status))

        if heartbeat and scanned % heartbeat == 0:
            elapsed = time.monotonic() - t_start
            rate = scanned / elapsed if elapsed > 0 else 0
            eta = (total - scanned) / rate if rate > 0 else 0
            pct = (scanned / total * 100) if total else 0
            print(f"  … {scanned}/{total} ({pct:.0f}%)  "
                  f"{rate:.0f} files/s  ETA {eta:.0f}s  "
                  f"wrote {counters['WROTE']+counters['DRY_OK']}",
                  flush=True)

    wrote = counters["WROTE"] + counters["DRY_OK"]
    print(f"\nDone. Processed: {files_done}")
    print(f"  Wrote / would-write: {wrote}")
    print(f"  Already tagged:      {counters['SKIP_ALREADY_TAGGED']}")
    print(f"  No type field:       {counters['NO_TYPE']}")
    print(f"  Infrastructure:      {counters['INFRASTRUCTURE']}")
    print(f"  No frontmatter:      {counters['NO_FRONTMATTER']}")
    print(f"  Extractor skipped:   {counters['EXTRACTOR_SKIPPED']}")

    if no_extractor_types:
        print("\n  Types present but no extractor registered:")
        for t, n in sorted(no_extractor_types.items(), key=lambda x: -x[1]):
            print(f"    {t:<25} {n} files")

    if errors:
        print(f"\n  Errors: {len(errors)}")
        for fp, err in errors[:10]:
            print(f"    {os.path.basename(fp)}: {err}")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
