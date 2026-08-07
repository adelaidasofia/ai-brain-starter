#!/usr/bin/env python3
"""
promote-episodic-to-procedural.py - Background consolidation for closed-loop
learning.

Walks <vault-root>/Meta/Learnings/ (the episodic capture sink populated by the
post-tool-use-learnings.py PostToolUse hook) and looks for recurring patterns.
When the same source_tool fails the same way 3 or more times, the script
drafts a procedural-memory candidate at:

    <vault-root>/Meta/Promotion-Candidates/<sha8>.md

The candidate's frontmatter is shaped to match either the workflow schema
(when the pattern is a repeatable success path) or the exception schema (when
the pattern is a repeatable failure). The default classification is
exception, because the hook only writes Learnings on failures or explicit
annotations, so the recurring case is overwhelmingly a failure pattern.

The candidate is `status: candidate`. A human reviews the file, edits it
into shape, moves it to the appropriate folder (`Meta/Workflows/` or
`Meta/Exceptions/`), and only then does the procedural memory go live.
The script never promotes directly: human review is the gate.

Confidence-weighted promotion (Deliverable C):
  - When all entries in a cluster carry confidence >= AUTO_CONFIDENCE_THRESHOLD
    (default 0.85) AND the cluster spans >= AUTO_SPAN_DAYS (default 7) days,
    the candidate's status becomes `ready-for-auto-promote`. A human still
    has to flip the status to live (the gate stays human-in-the-loop), but
    these high-confidence candidates surface separately so reviewers can
    triage them faster.
  - Lower-confidence clusters (any entry below threshold, or span too short)
    keep `status: candidate`.

Cron-friendly behavior (Deliverable A):
  - State file at <vault-root>/.promote-state.json tracks the last-run
    timestamp and the count of Learning files seen. Skip work entirely when
    no new Learnings have appeared since the last run.
  - --quiet flag suppresses output unless candidates were drafted, so cron
    invocations do not generate inbox noise on no-op runs.

Heuristic for grouping (deliberately simple; we want false-positive bias
toward surfacing patterns):
  1. Group Learning files by source_tool.
  2. Within each group, compute a 5-gram set over the error_excerpt.
  3. Two Learnings cluster together if their 5-gram sets share at least
     30 percent of their union. Single-link agglomerative.
  4. A cluster of size >= --min-occurrences becomes a candidate.

The 5-gram approach is on whitespace-tokenized words after lowercasing and
stripping non-word punctuation. It catches "permission denied: /tmp/x" and
"permission denied: /tmp/y" as the same pattern while keeping unrelated
errors apart.

CLI:
    python3 promote-episodic-to-procedural.py \\
        --vault-root /path/to/vault \\
        --min-occurrences 3 \\
        --dry-run \\
        --quiet \\
        --auto-confidence 0.85 \\
        --auto-span-days 7

Cron pattern (every 6 hours, quiet on no-ops):
    0 */6 * * * cd /path/to/vault && python3 \
        /path/to/ai-brain-starter/scripts/promote-episodic-to-procedural.py \
        --vault-root /path/to/vault --quiet

Stdlib + PyYAML only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


WORD_RE = re.compile(r"[a-z0-9]+")
NGRAM_N = 5
JACCARD_THRESHOLD = 0.30
STATE_FILENAME = ".promote-state.json"
DEFAULT_AUTO_CONFIDENCE = 0.85
DEFAULT_AUTO_SPAN_DAYS = 7


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a markdown file into (frontmatter_dict, body). Returns ({}, text) on failure."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw_fm = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    if yaml is None:
        return parse_simple_yaml(raw_fm), body
    try:
        fm = yaml.safe_load(raw_fm) or {}
        if not isinstance(fm, dict):
            return {}, text
        return fm, body
    except yaml.YAMLError:
        return parse_simple_yaml(raw_fm), body


def parse_simple_yaml(raw: str) -> dict:
    """Tiny stdlib fallback that handles the flat key:value lines this script writes."""
    fm: dict = {}
    for line in raw.splitlines():
        if ":" not in line or line.startswith(" ") or line.startswith("-"):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        fm[key] = value
    return fm


def render_frontmatter(frontmatter: dict) -> str:
    if yaml is not None:
        return yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    lines = []
    for key, value in frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                if isinstance(item, dict):
                    first = True
                    for k, v in item.items():
                        prefix = "  - " if first else "    "
                        lines.append(f"{prefix}{k}: {json.dumps(v, ensure_ascii=False)}")
                        first = False
                else:
                    lines.append(f"  - {json.dumps(item, ensure_ascii=False)}")
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            for k, v in value.items():
                lines.append(f"  {k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def tokenize(text: str) -> list[str]:
    return WORD_RE.findall(text.lower())


def ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    if len(tokens) < n:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def cluster_learnings(items: list[dict], threshold: float) -> list[list[dict]]:
    """Single-link agglomerative clustering on the 5-gram Jaccard similarity."""
    clusters: list[list[dict]] = []
    for item in items:
        placed = False
        for cluster in clusters:
            for existing in cluster:
                if jaccard(item["ngrams"], existing["ngrams"]) >= threshold:
                    cluster.append(item)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            clusters.append([item])
    return clusters


def stable_sha8(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def common_excerpt(cluster: list[dict]) -> str:
    """Pick the shortest non-empty excerpt as the representative."""
    excerpts = [c["error_excerpt"] for c in cluster if c["error_excerpt"]]
    if not excerpts:
        return ""
    excerpts.sort(key=len)
    return excerpts[0][:300]


def parse_iso_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Fall back to date-only.
        if len(s) >= 10:
            try:
                return datetime.fromisoformat(s[:10])
            except ValueError:
                return None
        return None


def cluster_meets_auto_promote(
    cluster: list[dict],
    auto_confidence: float,
    auto_span_days: int,
) -> bool:
    """High-confidence + sufficient span check for auto-promote eligibility."""
    confidences = [c.get("confidence") for c in cluster]
    if not all(isinstance(c, (int, float)) and c >= auto_confidence for c in confidences):
        return False

    timestamps = [c.get("captured_dt") for c in cluster if c.get("captured_dt")]
    if len(timestamps) < 2:
        return False

    span_days = (max(timestamps) - min(timestamps)).days
    return span_days >= auto_span_days


def build_candidate(
    cluster: list[dict],
    vault_root: Path,
    auto_confidence: float,
    auto_span_days: int,
) -> tuple[str, dict, str]:
    """Return (filename_stem, frontmatter_dict, body_string)."""
    source_tool = cluster[0]["source_tool"]
    excerpt = common_excerpt(cluster)
    seed = f"{source_tool}|{excerpt}|{len(cluster)}"
    sha8 = stable_sha8(seed)

    auto_eligible = cluster_meets_auto_promote(cluster, auto_confidence, auto_span_days)
    status = "ready-for-auto-promote" if auto_eligible else "candidate"

    summary = (
        f"{source_tool} repeatedly produces the same failure "
        f"(observed {len(cluster)} times)."
    )

    sources = []
    for c in cluster:
        rel = (
            str(c["path"].relative_to(vault_root))
            if vault_root in c["path"].parents
            else str(c["path"])
        )
        sources.append(rel)

    confidences = [c.get("confidence") for c in cluster if isinstance(c.get("confidence"), (int, float))]
    timestamps = [c.get("captured_dt") for c in cluster if c.get("captured_dt")]
    span_days = (max(timestamps) - min(timestamps)).days if len(timestamps) >= 2 else 0
    min_conf = min(confidences) if confidences else None

    frontmatter = {
        "type": "exception",
        "memory_class": "procedural",
        "status": status,
        "exception_summary": summary,
        "frequency_observed": len(cluster),
        "cluster_span_days": span_days,
        "min_confidence": min_conf,
        "source_episodic_files": sources,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provenance": [
            {
                "source_type": "claude-session",
                "source_id": "promote-episodic-to-procedural",
                "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ],
    }

    body_parts = [
        "## Pattern",
        "",
        f"`{source_tool}` failed {len(cluster)} times with overlapping error signatures.",
        "",
        "## Representative excerpt",
        "",
        "```",
        excerpt or "(no excerpt available)",
        "```",
        "",
        "## Source episodic captures",
        "",
    ]
    for src in sources:
        body_parts.append(f"- `{src}`")
    body_parts.append("")
    body_parts.append("## Reviewer notes")
    body_parts.append("")
    body_parts.append(f"Status: {status}. Awaiting human review.")
    body_parts.append("")
    if status == "ready-for-auto-promote":
        body_parts.append(
            "This cluster cleared the auto-promote bar (every captured entry "
            f"has confidence >= {auto_confidence} and the cluster spans >= "
            f"{auto_span_days} days). The reviewer can fast-track this candidate."
        )
        body_parts.append("")
    body_parts.append(
        "If this pattern reflects a real reusable failure mode, edit this file into the exception schema "
        "(see `templates/schemas/exception.json`) and move it to `Meta/Exceptions/`. "
        "If it reflects a positive repeatable recipe, reshape it to the workflow schema "
        "(see `templates/schemas/workflow.json`) and move it to `Meta/Workflows/` instead. "
        "Either way, the source episodic captures stay in `Meta/Learnings/` as evidence."
    )
    body_parts.append("")

    return sha8, frontmatter, "\n".join(body_parts)


SINK_GITIGNORE = (
    "# Closed-loop machinery sink — local-only, never synced.\n"
    "# Episodic captures and derived candidates can carry failure excerpts,\n"
    "# internal paths, and untrusted third-party content. They must never enter\n"
    "# a vault's git history. This .gitignore self-scopes the sink so protection\n"
    "# does not depend on the vault's root .gitignore or the operator's habits.\n"
    "*\n"
    "!.gitignore\n"
)


def ensure_sink_gitignore(directory: Path) -> None:
    """Drop a self-scoping .gitignore into a machinery sink dir (idempotent +
    self-healing). Safe-by-construction: the sink never syncs."""
    gi = directory / ".gitignore"
    if not gi.exists():
        try:
            gi.write_text(SINK_GITIGNORE, encoding="utf-8")
        except OSError:
            pass


def load_learnings(learnings_dir: Path) -> list[dict]:
    items: list[dict] = []
    if not learnings_dir.is_dir():
        return items
    for path in sorted(learnings_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = parse_frontmatter(text)
        if (fm.get("type") or "").strip() != "learning":
            continue
        excerpt = (fm.get("error_excerpt") or "").strip()
        if not excerpt:
            m = re.search(r"## Error excerpt\s*```(.*?)```", body, re.DOTALL)
            if m:
                excerpt = m.group(1).strip()
        source_tool = (fm.get("source_tool") or "").strip() or "unknown"
        tokens = tokenize(excerpt)
        confidence = fm.get("confidence")
        if isinstance(confidence, str):
            try:
                confidence = float(confidence)
            except ValueError:
                confidence = None
        captured_dt = parse_iso_dt(fm.get("captured_at"))
        items.append(
            {
                "path": path,
                "source_tool": source_tool,
                "error_excerpt": excerpt,
                "ngrams": ngrams(tokens, NGRAM_N),
                "confidence": confidence,
                "captured_dt": captured_dt,
            }
        )
    return items


def load_state(state_path: Path) -> dict:
    if not state_path.is_file():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state_path: Path, state: dict) -> None:
    try:
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"WARN: could not write state file {state_path}: {e}", file=sys.stderr)


def count_learnings(learnings_dir: Path) -> int:
    if not learnings_dir.is_dir():
        return 0
    return sum(1 for _ in learnings_dir.glob("*.md"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--vault-root",
        type=Path,
        required=True,
        help="Path to the vault root (contains a Meta/ folder).",
    )
    parser.add_argument(
        "--min-occurrences",
        type=int,
        default=3,
        help="Minimum cluster size to draft a promotion candidate (default 3).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be drafted without writing any files.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress output unless candidates were drafted (cron-friendly).",
    )
    parser.add_argument(
        "--auto-confidence",
        type=float,
        default=DEFAULT_AUTO_CONFIDENCE,
        help=(
            f"Minimum confidence on every clustered entry to mark a candidate "
            f"ready-for-auto-promote (default {DEFAULT_AUTO_CONFIDENCE})."
        ),
    )
    parser.add_argument(
        "--auto-span-days",
        type=int,
        default=DEFAULT_AUTO_SPAN_DAYS,
        help=(
            f"Minimum span (days between earliest and latest entry) for "
            f"auto-promote eligibility (default {DEFAULT_AUTO_SPAN_DAYS})."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the state file and re-scan all Learnings.",
    )
    args = parser.parse_args(argv)

    vault_root = args.vault_root.expanduser().resolve()
    learnings_dir = vault_root / "Meta" / "Learnings"
    candidates_dir = vault_root / "Meta" / "Promotion-Candidates"
    state_path = vault_root / STATE_FILENAME

    # Re-assert sink self-protection every run (self-heals older installs whose
    # hook predates the safe-by-construction .gitignore).
    if learnings_dir.is_dir():
        ensure_sink_gitignore(learnings_dir)

    state = load_state(state_path)
    learning_count = count_learnings(learnings_dir)
    last_count = state.get("last_learning_count", -1)

    if not args.force and learning_count == last_count and learning_count > 0:
        if not args.quiet:
            print(
                f"No new Learnings since last run ({learning_count} files). "
                "Skipping. Use --force to rescan."
            )
        return 0

    items = load_learnings(learnings_dir)
    if not items:
        if not args.quiet:
            print(f"No learning files found at {learnings_dir}")
        # Persist state so empty runs do not retrigger.
        new_state = dict(state)
        new_state["last_run_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_state["last_learning_count"] = learning_count
        if not args.dry_run:
            save_state(state_path, new_state)
        return 0

    by_tool: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_tool[item["source_tool"]].append(item)

    drafted = 0
    skipped = 0
    auto_ready = 0
    drafted_messages: list[str] = []

    for tool, group in by_tool.items():
        clusters = cluster_learnings(group, JACCARD_THRESHOLD)
        for cluster in clusters:
            if len(cluster) < args.min_occurrences:
                continue
            sha8, fm, body = build_candidate(
                cluster,
                vault_root,
                args.auto_confidence,
                args.auto_span_days,
            )
            target = candidates_dir / f"{sha8}.md"
            content = "---\n" + render_frontmatter(fm).rstrip() + "\n---\n\n" + body
            status = fm.get("status", "candidate")
            if status == "ready-for-auto-promote":
                auto_ready += 1
            if args.dry_run:
                drafted_messages.append(
                    f"[dry-run] would write {target} ({len(cluster)} captures, tool={tool}, status={status})"
                )
                drafted += 1
                continue
            if target.exists():
                skipped += 1
                continue
            try:
                candidates_dir.mkdir(parents=True, exist_ok=True)
                ensure_sink_gitignore(candidates_dir)
                target.write_text(content, encoding="utf-8")
                drafted_messages.append(
                    f"Wrote {target} ({len(cluster)} captures, tool={tool}, status={status})"
                )
                drafted += 1
            except OSError as e:
                print(f"Failed to write {target}: {e}", file=sys.stderr)

    # Update state file (skip when --dry-run so dry runs do not gate next live run).
    if not args.dry_run:
        new_state = dict(state)
        new_state["last_run_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_state["last_learning_count"] = learning_count
        new_state["last_drafted"] = drafted
        new_state["last_auto_ready"] = auto_ready
        save_state(state_path, new_state)

    # Output gating: --quiet only speaks when we drafted something new.
    if drafted == 0 and skipped == 0:
        if not args.quiet:
            print(
                f"Scanned {len(items)} learning file(s). No clusters reached "
                f"min-occurrences={args.min_occurrences}."
            )
        return 0

    if args.quiet and drafted == 0:
        # Pure no-op skip-by-existing case under --quiet: stay silent.
        return 0

    for msg in drafted_messages:
        print(msg)
    print(
        f"Drafted {drafted} candidate(s), {auto_ready} ready-for-auto-promote. "
        f"Skipped {skipped} pre-existing."
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
