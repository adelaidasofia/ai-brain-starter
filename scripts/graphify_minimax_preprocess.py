#!/usr/bin/env python3
"""
graphify_minimax_preprocess.py — Phase 1.5: cheap-model pre-extraction for graphify

Sits between Phase 1 (regex prep) and Phase 3 (main-model subagent dispatch).
Sends each file to a cheap extractor model (MiniMax M2.7 by default) for
entity/theme extraction, so downstream subagents can skip entity discovery
and focus on cross-file inference.

Usage:
    python3 graphify_minimax_preprocess.py <input_dir> [--apply] [--max-concurrent 5]

    Without --apply: dry run, shows what would be processed and estimated cost.
    With --apply: calls the API and writes pre-extracts.

Output:
    <input_dir>/../graphify-out/.minimax_preextract.json — per-file entity/theme data
    <input_dir>/../graphify-out/.minimax_preextract_report.md — summary report

The pre-extract JSON gets included in the main-model subagent prompt (Phase 3),
replacing raw file reads for entity discovery. Main model then focuses ONLY on:
  - Cross-file inference (what the cheap model can't see, since it processes file-by-file)
  - Hyperedge synthesis (requires understanding relationships across files)
  - Nuanced semantic similarity between concepts

Configuration:
    MINIMAX_API_KEY env var must be set. Get a key at https://minimax.io
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# -- Config --

MINIMAX_MODEL = "MiniMax-M2.7"
MINIMAX_ENDPOINT = "https://api.minimax.io/v1/text/chatcompletion_v2"
MAX_TOKENS_PER_FILE = 1500
MAX_FILE_CHARS = 15000
COST_PER_M_TOKENS = 0.30

EXTRACTION_PROMPT = """You are a fast entity/theme extractor. Given a markdown file from a personal knowledge vault, extract structured data. Be precise and brief.

FILE CONTENT:
---
{content}
---

Extract and return ONLY valid JSON (no markdown fencing, no commentary):
{{
  "people": ["list of named people mentioned (first name or full name, not pronouns)"],
  "places": ["list of named locations/cities/countries/venues"],
  "companies": ["list of company/organization/project names"],
  "concepts": ["list of key themes or concepts NOT inside [[wikilinks]] — implicit ideas only"],
  "frameworks": ["list of original metaphors or frameworks the author coined"],
  "emotions": ["list of emotional states or floor references (Shame, Guilt, Fear, Courage, Love, etc.)"],
  "decisions": ["one-line summary of any decisions made in this file"],
  "key_relationships": ["brief description of relationships between entities, e.g. 'Person A disagrees with Person B about pricing'"]
}}

Rules:
- People: use the name as written. "Mom" and "Dad" count. Skip "I" or "me"
- Concepts: only include non-obvious themes the text implies but doesn't name with [[wikilinks]]
- Frameworks: only include if the author clearly coined or owns the framework (not general knowledge)
- Emotions: map to High-Rise floor names when possible (Shame=1, Guilt=2, Apathy=3, Grief=4, Fear=5, Desire=6, Anger=7, Pride=8, Courage=9, Neutrality=10, Willingness=11, Acceptance=12, Reason=13, Love=14, Joy=15, Peace=16)
- key_relationships: only include relationships STATED in the text, not inferred
- Empty arrays are fine. Don't invent data
- Return ONLY the JSON object, nothing else"""


def get_api_key():
    """Resolve MINIMAX_API_KEY from the environment, with a fallback grep
    across common shell config files.

    Why the full fallback chain: ~/.zshrc is interactive-only. Scripts launched
    by IDE agents or CI runners run under a non-interactive shell that never
    sources .zshrc, so a key defined only there is invisible. Users commonly
    keep secrets in ~/.zsh_secrets or ~/.zshenv (which every shell sources).
    Walking all of them makes the script Just Work without requiring users to
    reshape their dotfiles.
    """
    key = os.environ.get("MINIMAX_API_KEY")
    if not key:
        candidates = [
            "~/.zshenv", "~/.zsh_secrets", "~/.zshrc", "~/.zprofile",
            "~/.bashrc", "~/.bash_profile", "~/.profile", "~/.env",
        ]
        for path in candidates:
            full = os.path.expanduser(path)
            if not os.path.exists(full):
                continue
            try:
                result = subprocess.run(
                    ["grep", "^export MINIMAX_API_KEY=", full],
                    capture_output=True, text=True
                )
                for line in result.stdout.strip().splitlines():
                    if '"' in line:
                        key = line.split('"')[1]
                        break
                    if "=" in line:
                        key = line.split("=", 1)[1].strip().strip("'").strip('"')
                        break
                if key:
                    break
            except Exception:
                pass
    if not key:
        print(
            "Error: MINIMAX_API_KEY not found in environment or any of "
            "~/.zshenv, ~/.zsh_secrets, ~/.zshrc, ~/.zprofile, ~/.bashrc, "
            "~/.bash_profile, ~/.profile, ~/.env",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def call_minimax(api_key, content, filename):
    import urllib.request
    import urllib.error

    if len(content) > MAX_FILE_CHARS:
        content = content[:MAX_FILE_CHARS] + "\n\n[... truncated for extraction ...]"

    prompt = EXTRACTION_PROMPT.format(content=content)

    payload = json.dumps({
        "model": MINIMAX_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS_PER_FILE,
    }).encode("utf-8")

    req = urllib.request.Request(
        MINIMAX_ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as e:
        return {"error": str(e), "file": filename}

    status_code = data.get("base_resp", {}).get("status_code", 0)
    if status_code != 0:
        return {
            "error": data.get("base_resp", {}).get("status_msg", "Unknown"),
            "file": filename,
        }

    raw_content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = data.get("usage", {})

    try:
        cleaned = raw_content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        extracted = json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "error": f"JSON parse failed: {raw_content[:200]}",
            "file": filename,
            "raw": raw_content,
        }

    return {
        "file": filename,
        "extracted": extracted,
        "tokens": {
            "prompt": usage.get("prompt_tokens", 0),
            "completion": usage.get("completion_tokens", 0),
            "total": usage.get("total_tokens", 0),
        },
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Cheap-model pre-extraction for graphify")
    parser.add_argument("input_dir", help="Path to graphify-input directory")
    parser.add_argument("--apply", action="store_true", help="Actually call the API (default: dry run)")
    parser.add_argument("--max-concurrent", type=int, default=5, help="Max concurrent API calls")
    parser.add_argument("--limit", type=int, default=0, help="Process only first N files (for testing)")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        print(f"Error: {input_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    md_files = sorted([
        f for f in input_dir.rglob("*.md")
        if "_review_alternate_drafts" not in str(f)
        and not any(part.startswith(".") for part in f.parts)
    ])

    if args.limit > 0:
        md_files = md_files[:args.limit]

    total_chars = 0
    file_data = []
    for f in md_files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            chars = min(len(content), MAX_FILE_CHARS)
            total_chars += chars
            file_data.append((f, content))
        except Exception as e:
            print(f"  Skip (read error): {f.name} — {e}", file=sys.stderr)

    est_input_tokens = total_chars // 4
    est_output_tokens = len(file_data) * 300
    est_total_tokens = est_input_tokens + est_output_tokens
    est_cost = est_total_tokens * COST_PER_M_TOKENS / 1_000_000

    print(f"\n{'='*60}")
    print(f"Pre-Extraction — {'DRY RUN' if not args.apply else 'LIVE RUN'}")
    print(f"{'='*60}")
    print(f"Files to process:    {len(file_data)}")
    print(f"Total input chars:   {total_chars:,} ({total_chars // 4:,} est. tokens)")
    print(f"Est. output tokens:  {est_output_tokens:,}")
    print(f"Est. total tokens:   {est_total_tokens:,}")
    print(f"Est. cost:           ${est_cost:.4f}")
    print(f"Max concurrent:      {args.max_concurrent}")
    print(f"{'='*60}\n")

    if not args.apply:
        print("Dry run complete. Add --apply to execute.")
        return

    api_key = get_api_key()

    results = []
    errors = []
    total_tokens_used = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=args.max_concurrent) as executor:
        futures = {
            executor.submit(call_minimax, api_key, content, str(f.relative_to(input_dir))): f
            for f, content in file_data
        }

        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if "error" in result:
                errors.append(result)
                print(f"  [{i}/{len(file_data)}] ERROR: {result['file']} — {result['error']}")
            else:
                results.append(result)
                tokens = result.get("tokens", {}).get("total", 0)
                total_tokens_used += tokens
                print(f"  [{i}/{len(file_data)}] OK: {result['file']} ({tokens} tokens)")

    elapsed = time.time() - start_time

    output_dir = input_dir.parent / "graphify-out"
    output_dir.mkdir(exist_ok=True)

    preextract_path = output_dir / ".minimax_preextract.json"
    preextract_data = {
        "files": {r["file"]: r["extracted"] for r in results},
        "total_tokens": total_tokens_used,
        "total_files": len(results),
        "errors": len(errors),
        "elapsed_seconds": round(elapsed, 1),
    }
    preextract_path.write_text(json.dumps(preextract_data, indent=2, ensure_ascii=False))

    report_path = output_dir / ".minimax_preextract_report.md"
    actual_cost = total_tokens_used * COST_PER_M_TOKENS / 1_000_000

    all_people = set()
    all_places = set()
    all_companies = set()
    all_concepts = set()
    for r in results:
        ext = r.get("extracted", {})
        all_people.update(ext.get("people", []))
        all_places.update(ext.get("places", []))
        all_companies.update(ext.get("companies", []))
        all_concepts.update(ext.get("concepts", []))

    report = f"""# Pre-Extraction Report

**Date:** {time.strftime('%Y-%m-%d %H:%M')}
**Files processed:** {len(results)} / {len(file_data)} ({len(errors)} errors)
**Total tokens:** {total_tokens_used:,}
**Cost:** ${actual_cost:.4f}
**Elapsed:** {elapsed:.1f}s

## Entity Summary

| Type | Count | Examples |
|------|-------|---------|
| People | {len(all_people)} | {', '.join(sorted(all_people)[:10])} |
| Places | {len(all_places)} | {', '.join(sorted(all_places)[:10])} |
| Companies | {len(all_companies)} | {', '.join(sorted(all_companies)[:10])} |
| Concepts | {len(all_concepts)} | {', '.join(sorted(all_concepts)[:10])} |

## Errors

{chr(10).join(f"- {e['file']}: {e['error']}" for e in errors) if errors else "None"}

## Next Step

This pre-extract is saved to `.minimax_preextract.json`. Include it in the
main-model subagent prompt (Phase 3) so agents skip entity discovery and
focus on cross-file inference + hyperedge synthesis.
"""
    report_path.write_text(report)

    print(f"\n{'='*60}")
    print(f"DONE")
    print(f"{'='*60}")
    print(f"Files:    {len(results)} processed, {len(errors)} errors")
    print(f"Tokens:   {total_tokens_used:,}")
    print(f"Cost:     ${actual_cost:.4f}")
    print(f"Time:     {elapsed:.1f}s")
    print(f"Output:   {preextract_path}")
    print(f"Report:   {report_path}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
