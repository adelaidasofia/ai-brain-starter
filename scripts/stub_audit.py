#!/usr/bin/env python3
"""
stub_audit.py — bucketed signal-density audit for vault files.

Does NOT delete. Shows distribution so the user can pick a threshold.

Buckets:
  A. Empty or URL-only
  B. Pure bullet/CSV lists (no prose sentences, no headers, <40 words)
  C. Very short with few wikilinks (<40 words, <=1 wikilink, no floor tag, no headers, no prose)
  D. Short scratch (40-100 words, no floor, no headers, <=1 wikilink, no prose)
  E. Short with one strong signal (sentence + wikilink + <100 words) — KEEP candidates
  F. Everything else — substantive

Writes:
  STUB_AUDIT.md   — counts by bucket × top folder + 10 sample paths per bucket
  STUB_AUDIT.json — full paths for each bucket

Usage:
    python3 stub_audit.py --vault-root "$(pwd)"
    python3 stub_audit.py --vault-root "$(pwd)" --skip "Drafts,Personal"
    python3 stub_audit.py --vault-root "$(pwd)" --out-dir "/tmp/audit"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict, Counter
from pathlib import Path

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
HEADER_RE = re.compile(r"^#{2,3}\s+\S", re.MULTILINE)
SENTENCE_RE = re.compile(
    r"\b(is|are|was|were|has|have|had|will|would|could|should|does|did|made|makes|"
    r"feels|felt|think|thought|know|knew|want|wanted|need|needed|said|saw|see|seen|"
    r"love|loved|hate|hated|built|build|shipped|wrote|writing|realize|realized|noticed)\b",
    re.IGNORECASE,
)
URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Universal-skip directory names. Override via --skip or VAULT_SKIP_PARTS env var.
DEFAULT_SKIP_PARTS = {
    "⚙️ Meta", ".trash", ".obsidian", ".claude", ".git",
    "graphify-out", "graphify-input",
    "_review_alternate_drafts", "Archive", "🗄 Archive",
}


def load_skip_parts(extra: str | None) -> set[str]:
    skip = set(DEFAULT_SKIP_PARTS)
    env_extra = os.environ.get("VAULT_SKIP_PARTS", "")
    for src in (env_extra, extra or ""):
        if not src:
            continue
        for token in src.split(","):
            token = token.strip()
            if token:
                skip.add(token)
    return skip


def parse_frontmatter(content):
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    block = m.group(1)
    body = content[m.end():]
    fm = {}
    for line in block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, body


def classify(path):
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return "F", {}

    fm, body = parse_frontmatter(content)
    clean = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()
    wc = len(clean.split())
    wl = len(WIKILINK_RE.findall(clean))
    headers = len(HEADER_RE.findall(clean))
    sentences = len(SENTENCE_RE.findall(clean))
    floor = fm.get("floor") or fm.get("floors")
    has_floor = floor and floor.lower() not in ("", "null", "none", "[]")

    non_empty_lines = [l for l in clean.splitlines() if l.strip()]
    bullet_lines = [l for l in non_empty_lines if l.strip().startswith(("-", "*", "+"))]
    bullet_ratio = len(bullet_lines) / max(len(non_empty_lines), 1)

    stats = {"wc": wc, "wl": wl, "headers": headers, "sentences": sentences,
             "has_floor": bool(has_floor), "bullet_ratio": round(bullet_ratio, 2)}

    if not clean:
        return "A", stats
    if len(clean.splitlines()) <= 3 and URL_ONLY_RE.match(clean.strip().splitlines()[0] if clean.strip() else ""):
        return "A", stats

    if bullet_ratio > 0.7 and headers == 0 and sentences < 2 and wc < 40 and not has_floor:
        return "B", stats

    if wc < 40 and wl <= 1 and headers == 0 and sentences < 2 and not has_floor:
        return "C", stats

    if 40 <= wc < 100 and wl <= 1 and headers == 0 and sentences < 2 and not has_floor:
        return "D", stats

    if wc < 100 and (sentences >= 1 or has_floor or headers >= 1 or wl >= 2):
        return "E", stats

    return "F", stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault-root", required=True)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--skip", default="", help="Extra comma-separated SKIP_PARTS names")
    args = ap.parse_args()

    vault = Path(args.vault_root).resolve()
    skip_parts = load_skip_parts(args.skip)
    out_dir = Path(args.out_dir) if args.out_dir else vault / "⚙️ Meta" / "graphify-out"
    out_dir.mkdir(parents=True, exist_ok=True)

    buckets = defaultdict(list)
    total = 0
    for md in vault.rglob("*.md"):
        rel = md.relative_to(vault)
        if any(p in skip_parts for p in rel.parts):
            continue
        total += 1
        bucket, stats = classify(md)
        buckets[bucket].append({"path": str(rel), **stats})

    for b in buckets:
        buckets[b].sort(key=lambda x: x["wc"])

    labels = {
        "A": "Empty / URL-only",
        "B": "Pure bullet list or CSV (no prose, <40w)",
        "C": "Very short, light wikilinks, no structure (<40w)",
        "D": "Short scratch, no structure (40-100w, <=1 wikilink)",
        "E": "Short with signal (has sentence, floor, header, or >=2 wikilinks)",
        "F": "Substantive (default keep)",
    }

    json_out = {
        "vault": str(vault),
        "total_scanned": total,
        "buckets": {b: {"label": labels[b], "count": len(buckets[b]), "files": buckets[b]} for b in "ABCDEF"},
    }
    (out_dir / "STUB_AUDIT.json").write_text(json.dumps(json_out, indent=2, ensure_ascii=False))

    lines = ["# Stub Audit — bucketed by signal density", ""]
    lines.append(f"Total files scanned: **{total}**")
    lines.append("")
    lines.append("## Bucket summary")
    lines.append("")
    lines.append("| Bucket | Label | Count |")
    lines.append("|---|---|---|")
    for b in "ABCDEF":
        lines.append(f"| **{b}** | {labels[b]} | {len(buckets[b])} |")
    lines.append("")

    for b in "ABCDE":
        entries = buckets[b]
        if not entries:
            continue
        lines.append(f"## Bucket {b} — {labels[b]} ({len(entries)} files)")
        lines.append("")
        folder_counts = Counter()
        for e in entries:
            folder_counts[e["path"].split("/")[0]] += 1
        lines.append("**By folder:**")
        lines.append("")
        for folder, count in folder_counts.most_common():
            lines.append(f"- `{folder}`: {count}")
        lines.append("")
        lines.append("**10 smallest samples:**")
        lines.append("")
        for e in entries[:10]:
            lines.append(f"- [{e['wc']}w, wl={e['wl']}, prose={e['sentences']}] `{e['path']}`")
        lines.append("")

    (out_dir / "STUB_AUDIT.md").write_text("\n".join(lines))

    print(f"Scanned: {total}")
    for b in "ABCDEF":
        print(f"  Bucket {b}  {labels[b]:55s}  {len(buckets[b])}")
    print(f"\nReports:\n  {out_dir / 'STUB_AUDIT.md'}\n  {out_dir / 'STUB_AUDIT.json'}")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
