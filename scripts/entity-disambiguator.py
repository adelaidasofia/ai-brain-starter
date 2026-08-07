#!/usr/bin/env python3
"""
entity-disambiguator.py builds the alias index at Meta/.entity-aliases.json.

Scans Meta/Workflows/, Meta/Decisions/, Meta/Exceptions/, Meta/Facts/ for
capitalized noun phrases and slug-like tokens. Folds variant spellings into
a single canonical form so downstream synthesizers can write a stable
canonical_entity field next to the raw_mention.

Algorithm:
  1. Walk the four typed-memory folders.
  2. Pull candidate mentions from frontmatter `name`, `topic`, `subject`,
     `entity_ids` values, and from the body text via:
       - Capitalized noun phrases (one or two adjacent capitalized tokens).
       - Slug-like tokens (lower or mixed-case alphanumeric runs of length
         >= 4 with internal capitalization or hyphens).
  3. Cluster mentions by similarity:
       - Normalize each candidate to a comparable key (lowercased,
         non-alphanumeric stripped).
       - Two mentions belong to the same cluster when their normalized
         keys are equal OR Jaccard similarity on character bigrams >= 0.80
         OR Levenshtein distance / max-length <= 0.20.
  4. Pick a canonical form per cluster:
       - The most frequent original spelling wins.
       - Ties go to the longest spelling.
       - Further ties go to alphabetic sort.
  5. Emit Meta/.entity-aliases.json with shape:
       {
         "built_at": "<iso>",
         "vault_root": "<path>",
         "canonical_count": <int>,
         "alias_count": <int>,
         "aliases": {"<variant>": "<canonical>", ...}
       }
  6. Operator overrides at Meta/entity-aliases-overrides.json (committed)
     always win. Override schema:
       {"aliases": {"<variant>": "<canonical>", ...}}
     Overrides are applied AFTER the auto-build, so any variant listed in
     the override file is forced to the override canonical.

CLI:
  python3 scripts/entity-disambiguator.py --vault-root PATH [--rebuild] [--dry-run]

Idempotent. Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


META_BASENAMES = ("Meta",)
TYPED_FOLDERS = ("Decisions", "Workflows", "Exceptions", "Facts")

JACCARD_THRESHOLD = 0.80
LEVENSHTEIN_RATIO_THRESHOLD = 0.20
MIN_TOKEN_LEN = 4

CAP_PHRASE = re.compile(
    r"\b([A-Z][a-z0-9]+(?:[ \-]?[A-Z][a-z0-9]+){0,3})\b"
)
SLUG_TOKEN = re.compile(
    r"\b([A-Za-z][A-Za-z0-9]*(?:[A-Z][a-z0-9]+|[\-_][A-Za-z0-9]+)+)\b"
)
LOWER_RUN = re.compile(r"\b([a-z][a-z0-9]{3,})\b")

STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "into", "over",
    "step", "steps", "note", "notes", "rule", "rules", "name", "type",
    "decision", "workflow", "exception", "fact", "topic", "subject",
    "true", "false", "null", "none", "yes", "no", "today", "yesterday",
    "tomorrow", "now", "version", "build", "build_at", "built_at",
    "approved", "deny", "allow", "review", "summary", "body", "text",
    "title", "owner", "owners", "team", "teams", "input", "output",
}


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir as _find_meta_dir_helper  # noqa: E402


def find_meta_dir(vault_root: Path) -> Path | None:
    return _find_meta_dir_helper(
        vault_root,
        prefer_subfolders=("RESOLVER.md", "Decisions"),
    )


def normalize_key(value: str) -> str:
    """Strip non-alphanumerics, lowercase. The comparison key for clustering."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def char_bigrams(value: str) -> set[str]:
    """Set of 2-char shingles. Empty for strings shorter than 2."""
    s = (value or "").lower()
    if len(s) < 2:
        return set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def jaccard(a: str, b: str) -> float:
    """Jaccard similarity over character bigrams. 1.0 == identical."""
    ba = char_bigrams(a)
    bb = char_bigrams(b)
    if not ba and not bb:
        return 1.0
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    union = len(ba | bb)
    return inter / union if union else 0.0


def levenshtein(a: str, b: str) -> int:
    """Classic Levenshtein. Stdlib only, iterative two-row implementation."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                curr[j - 1] + 1,
                prev[j] + 1,
                prev[j - 1] + cost,
            )
        prev = curr
    return prev[-1]


def lev_ratio(a: str, b: str) -> float:
    """Levenshtein distance / max(len). 0.0 == identical, 1.0 == fully different."""
    la = len(a)
    lb = len(b)
    if la == 0 and lb == 0:
        return 0.0
    if la == 0 or lb == 0:
        return 1.0
    return levenshtein(a, b) / max(la, lb)


def parse_frontmatter_yaml(text: str) -> dict[str, Any]:
    """Lightweight frontmatter extractor. PyYAML if available, else regex."""
    if not text.startswith("---"):
        return {}
    m = re.match(r"^---\n(.*?)\n---\s*", text, re.DOTALL)
    if not m:
        return {}
    block = m.group(1)
    try:
        import yaml
    except ImportError:
        return _scrape_simple_kv(block)
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return _scrape_simple_kv(block)
    return data if isinstance(data, dict) else {}


def _scrape_simple_kv(block: str) -> dict[str, Any]:
    """Fallback when PyYAML cannot parse: pull key: value lines verbatim."""
    out: dict[str, Any] = {}
    for line in block.splitlines():
        m = re.match(r"^(\w[\w\-]*):\s*(.+?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def split_body(text: str) -> str:
    """Return the body after the YAML frontmatter (or the full text)."""
    if not text.startswith("---"):
        return text
    m = re.match(r"^---\n.*?\n---\s*", text, re.DOTALL)
    return text[m.end():] if m else text


def extract_candidates_from_text(body: str) -> list[str]:
    """Extract capitalized phrases and slug-like tokens from body text."""
    out: list[str] = []
    out.extend(m.group(1) for m in CAP_PHRASE.finditer(body))
    out.extend(m.group(1) for m in SLUG_TOKEN.finditer(body))
    out.extend(m.group(1) for m in LOWER_RUN.finditer(body))
    return out


def extract_candidates_from_frontmatter(fm: dict[str, Any]) -> list[str]:
    """Walk frontmatter for entity-bearing fields. Recurse into dicts/lists."""
    candidates: list[str] = []
    for key in ("name", "topic", "subject", "rule_id", "id"):
        v = fm.get(key)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item.strip():
                    candidates.append(item.strip())
    eids = fm.get("entity_ids")
    if isinstance(eids, dict):
        for v in eids.values():
            if isinstance(v, str) and v.strip():
                candidates.append(v.strip())
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item.strip():
                        candidates.append(item.strip())
    return candidates


def collect_mentions(meta_dir: Path) -> Counter[str]:
    """Walk typed-memory folders, count every mention occurrence."""
    counts: Counter[str] = Counter()
    for folder_name in TYPED_FOLDERS:
        folder = meta_dir / folder_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = parse_frontmatter_yaml(text)
            body = split_body(text)
            for c in extract_candidates_from_frontmatter(fm):
                norm = normalize_key(c)
                if not norm or len(norm) < MIN_TOKEN_LEN:
                    continue
                if norm in STOPWORDS:
                    continue
                counts[c.strip()] += 1
            for c in extract_candidates_from_text(body):
                norm = normalize_key(c)
                if not norm or len(norm) < MIN_TOKEN_LEN:
                    continue
                if norm in STOPWORDS:
                    continue
                counts[c.strip()] += 1
    return counts


def cluster_mentions(counts: Counter[str]) -> dict[str, str]:
    """Group similar mentions, pick a canonical per cluster.

    Returns a dict mapping every observed variant to its canonical form.
    """
    by_norm: dict[str, list[str]] = defaultdict(list)
    for mention in counts:
        by_norm[normalize_key(mention)].append(mention)

    keys = sorted(by_norm.keys())
    parent: dict[str, str] = {k: k for k in keys}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    n = len(keys)
    for i in range(n):
        for j in range(i + 1, n):
            ki, kj = keys[i], keys[j]
            if not ki or not kj:
                continue
            if ki == kj:
                union(ki, kj)
                continue
            jc = jaccard(ki, kj)
            lr = lev_ratio(ki, kj)
            if jc >= JACCARD_THRESHOLD or lr <= LEVENSHTEIN_RATIO_THRESHOLD:
                union(ki, kj)

    clusters: dict[str, list[str]] = defaultdict(list)
    for k in keys:
        root = find(k)
        clusters[root].extend(by_norm[k])

    aliases: dict[str, str] = {}
    for cluster_members in clusters.values():
        scored: list[tuple[int, int, str]] = []
        for mention in cluster_members:
            scored.append((counts[mention], len(mention), mention))
        scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
        canonical = scored[0][2]
        for mention in cluster_members:
            aliases[mention] = canonical
    return aliases


def load_overrides(meta_dir: Path) -> dict[str, str]:
    """Load operator overrides if present. Empty dict otherwise."""
    override_path = meta_dir / "entity-aliases-overrides.json"
    if not override_path.is_file():
        return {}
    try:
        data = json.loads(override_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    aliases = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(aliases, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in aliases.items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def apply_overrides(aliases: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    """Operator overrides win. If override remaps a canonical, every alias of
    that canonical follows the override."""
    if not overrides:
        return aliases
    out = dict(aliases)
    for variant, override_canonical in overrides.items():
        out[variant] = override_canonical
    canonical_remap: dict[str, str] = {}
    for variant, override_canonical in overrides.items():
        existing = aliases.get(variant)
        if existing and existing != override_canonical:
            canonical_remap[existing] = override_canonical
    if canonical_remap:
        for k, v in list(out.items()):
            if v in canonical_remap:
                out[k] = canonical_remap[v]
    return out


def render_index(
    aliases: dict[str, str],
    vault_root: Path,
) -> dict[str, Any]:
    """Build the JSON document the script writes."""
    canonicals = sorted({c for c in aliases.values()})
    return {
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "vault_root": str(vault_root),
        "canonical_count": len(canonicals),
        "alias_count": len(aliases),
        "aliases": dict(sorted(aliases.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=Path(os.environ.get("VAULT_ROOT", Path.cwd())),
        help="Vault root containing the Meta folder.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force a full rebuild even if Meta/.entity-aliases.json already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rendered JSON without writing to disk.",
    )
    args = parser.parse_args()

    vault_root = args.vault_root.resolve()
    meta_dir = find_meta_dir(vault_root)
    if meta_dir is None:
        print(f"ERROR: no Meta folder under {vault_root}", file=sys.stderr)
        return 1

    out_path = meta_dir / ".entity-aliases.json"

    if out_path.exists() and not args.rebuild and not args.dry_run:
        print(
            f"[skip] {out_path} exists; pass --rebuild to refresh."
        )
        return 0

    counts = collect_mentions(meta_dir)
    aliases = cluster_mentions(counts)
    overrides = load_overrides(meta_dir)
    aliases = apply_overrides(aliases, overrides)

    index = render_index(aliases, vault_root)
    rendered = json.dumps(index, indent=2, ensure_ascii=False)

    if args.dry_run:
        print(rendered)
        return 0

    out_path.write_text(rendered + "\n", encoding="utf-8")
    print(
        f"Wrote {out_path} "
        f"({index['alias_count']} alias(es), "
        f"{index['canonical_count']} canonical(s), "
        f"{len(rendered):,} bytes)"
    )
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
