#!/usr/bin/env python3
"""
vault-classify-untyped.py — suggest `type:` for frontmatter-missing files via MiniMax.

Finds every markdown file whose frontmatter lacks a `type:` field and asks
MiniMax M2.7 to classify each against the canonical type list. Output: a
markdown report listing
  - file path
  - suggested type
  - confidence
  - reason

Does NOT write to files. Human reviews the report, accepts/edits suggestions,
then re-runs /second-brain-mapping to extract.

Why MiniMax: this is bulk text classification — cheap and structural, not
voice-dependent. ~$0.06/M tokens, so 100 files × ~300 tokens = $0.002 total.
Needs `minimax.sh` on PATH or in scripts/. Omit this step if MiniMax isn't
configured; hand-tag types instead.

Usage:
  python3 vault-classify-untyped.py           # run classifier, write report
  python3 vault-classify-untyped.py --dry-run # list files that would be classified
"""
import argparse
import glob
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "extractors"))

from _base import VAULT, SKIP_PARTS, parse_frontmatter  # noqa: E402

OUTPUT = os.path.join(VAULT, "⚙️ Meta/Untyped Classification Report.md")
MINIMAX_SCRIPT = os.path.join(HERE, "minimax.sh")

CANONICAL_TYPES = [
    "journal", "book", "article", "concept", "person", "business",
    "meeting", "ai_chat", "writing_draft", "strategy", "negotiation_prep",
    "company", "daily_log", "talk", "travel", "goal", "playbook",
    "asset", "reference", "dashboard", "meta", "template", "runbook",
    "report", "imported", "rule", "skill", "hook",
]

CLASSIFIER_PROMPT = """You are a vault document classifier. Given a file's path and first 400 characters, pick ONE type from this list:

{types}

Respond in strict JSON with this exact shape, no prose:
{{"type": "<chosen_type>", "confidence": "high|medium|low", "reason": "<under 15 words>"}}

If the file is truly an infrastructure doc (meta/template/runbook/report), use that. Never invent a new type. Never paraphrase. Pick the closest match."""


def find_untyped_files():
    """All .md files in vault whose frontmatter lacks `type:`. Skip SKIP_PARTS."""
    out = []
    for fp in glob.glob(os.path.join(VAULT, "**", "*.md"), recursive=True):
        parts = set(fp.split(os.sep))
        if parts & SKIP_PARTS:
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                content = f.read(3000)
        except Exception:
            continue
        if not content.startswith("---"):
            continue
        fm, _, _ = parse_frontmatter(content)
        if fm is None:
            continue
        if (fm.get("type") or "").strip():
            continue
        out.append(fp)
    return out


def classify_one(filepath):
    """Call MiniMax with the file context; parse JSON response."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read(3000)
    _, _, body = parse_frontmatter(content)

    rel = filepath.replace(VAULT, "").lstrip(os.sep)
    context = f"PATH: {rel}\nBODY (first 400 chars):\n{body[:400]}"
    prompt = CLASSIFIER_PROMPT.format(types=", ".join(CANONICAL_TYPES))
    full = f"{prompt}\n\n{context}"

    if not os.path.exists(MINIMAX_SCRIPT):
        return {"type": "UNKNOWN", "confidence": "low", "reason": "minimax.sh not found"}

    try:
        result = subprocess.run(
            ["bash", MINIMAX_SCRIPT, full, "800"],
            capture_output=True, text=True, timeout=45,
        )
        raw = result.stdout.strip()
        # Extract first JSON object
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return {"type": "UNKNOWN", "confidence": "low", "reason": f"no json: {raw[:80]}"}
        return json.loads(raw[start:end + 1])
    except subprocess.TimeoutExpired:
        return {"type": "UNKNOWN", "confidence": "low", "reason": "timeout"}
    except Exception as e:
        return {"type": "UNKNOWN", "confidence": "low", "reason": f"err: {e}"}


def write_report(classifications):
    from datetime import date
    lines = ["---", "type: report", f"last_updated: {date.today().isoformat()}", "---", ""]
    lines.append("# Untyped files — classification suggestions")
    lines.append("")
    lines.append(f"*MiniMax-generated suggestions for {len(classifications)} files lacking a `type:` field.*")
    lines.append(f"*Review each, edit the frontmatter directly, then re-run `/second-brain-mapping` to extract.*")
    lines.append("")
    lines.append("| File | Suggested type | Confidence | Reason |")
    lines.append("|---|---|---|---|")
    for c in classifications:
        rel = c["path"].replace(VAULT, "").lstrip(os.sep)
        lines.append(f"| `{rel}` | `{c['suggestion']['type']}` | {c['suggestion']['confidence']} | {c['suggestion']['reason']} |")
    lines.append("")
    lines.append("## How to accept a suggestion")
    lines.append("")
    lines.append("Open the file, add `type: <suggested>` to the frontmatter, save. Run `/second-brain-mapping` to extract.")
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="List files that would be classified, no MiniMax calls.")
    args = ap.parse_args()

    print("vault-classify-untyped  scanning for files without type…")
    files = find_untyped_files()
    print(f"  Found {len(files)} untyped files.")

    if args.dry_run:
        for fp in files[:20]:
            print(f"  - {fp.replace(VAULT, '').lstrip(os.sep)}")
        if len(files) > 20:
            print(f"  … and {len(files) - 20} more")
        return

    print(f"  Classifying via MiniMax (≈ $0.001 for ~{len(files)} files)…")
    results = []
    for i, fp in enumerate(files, 1):
        suggestion = classify_one(fp)
        results.append({"path": fp, "suggestion": suggestion})
        print(f"  [{i}/{len(files)}] {os.path.basename(fp)[:50]:50}  →  {suggestion.get('type')}")

    write_report(results)
    print(f"\n  Report: {OUTPUT}")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
