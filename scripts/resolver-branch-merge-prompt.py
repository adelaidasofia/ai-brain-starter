#!/usr/bin/env python3
"""
resolver-branch-merge-prompt.py drafts an operator merge prompt for each
parallel-branch decision pair surfaced by the resolver.

Branch-merge candidates are pairs of rules that share the same `pattern`,
contradict in `outcome`, and have `last_verified` within 30 days of each
other. Neither is clearly newer, so this is not a supersession; the
operator must choose one branch, the other, or merge into a new rule.

For each candidate the script writes a markdown prompt at:
  Meta/Branch-Merge-Candidates/<sha8>.md

The prompt carries:
  - YAML frontmatter with `status: awaiting-merge-decision` and pointers
    at both source files.
  - Both decisions side by side (frontmatter + body excerpts).
  - The contradiction surfaced (the outcome strings + token signal).
  - Three operator options:
      1. keep_branch_A: discard branch B, mark its file `superseded_by`.
      2. keep_branch_B: discard branch A, mark its file `superseded_by`.
      3. merge_into_new: write a new decision combining the two; both
         originals are then `superseded_by` the new one.
  - A merged-decision template the operator can fill in.

Idempotent. Filename is sha8(pattern + sorted(rule_a, rule_b)), so re-running
on the same pair overwrites the same prompt. Existing prompts with
`status: resolved` are skipped.

CLI:
  python3 scripts/resolver-branch-merge-prompt.py --vault-root PATH [--dry-run]

Stdlib + PyYAML only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any


def _load_aggregator():
    """Load resolver-build.py (hyphenated module name forces importlib)."""
    here = Path(__file__).resolve().parent
    src = here / "resolver-build.py"
    if not src.is_file():
        print(f"ERROR: resolver-build.py not found at {src}", file=sys.stderr)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("resolver_build", src)
    if spec is None or spec.loader is None:
        print("ERROR: could not load resolver-build.py", file=sys.stderr)
        sys.exit(1)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha8(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def render_yaml_block(meta: dict[str, Any]) -> str:
    """Render the prompt's frontmatter using PyYAML when available."""
    try:
        import yaml
        return yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    except ImportError:
        lines = []
        for k, v in meta.items():
            if isinstance(v, list):
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f"  - {item}")
            elif isinstance(v, dict):
                lines.append(f"{k}:")
                for sk, sv in v.items():
                    lines.append(f"  {sk}: {sv}")
            else:
                lines.append(f"{k}: {v}")
        return "\n".join(lines)


def read_source_excerpt(abs_path: str, max_chars: int = 800) -> str:
    """Read the source file's body (post-frontmatter) for inclusion in the prompt."""
    if not abs_path:
        return "_(source path missing)_"
    p = Path(abs_path)
    if not p.is_file():
        return f"_(source not readable at {abs_path})_"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return f"_(source unreadable at {abs_path})_"
    body = text
    if text.startswith("---"):
        m = re.match(r"^---\n.*?\n---\s*", text, re.DOTALL)
        if m:
            body = text[m.end():]
    body = body.strip()
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n\n_(truncated)_"
    return body


def build_prompt_filename(pattern: str, rule_a: str, rule_b: str) -> str:
    """Stable per-pair filename; ordering is rule_a/rule_b alphabetic."""
    pair = sorted([rule_a, rule_b])
    return sha8(f"{pattern}|{pair[0]}|{pair[1]}") + ".md"


def render_prompt(bm: dict[str, Any], existing_status: str | None = None) -> str:
    """Render one merge-prompt markdown body."""
    pattern = bm["pattern"]
    a = bm["branch_a"]
    b = bm["branch_b"]
    delta = bm["delta_days"]

    today = dt.date.today().isoformat()
    fm = {
        "type": "branch-merge-candidate",
        "status": existing_status or "awaiting-merge-decision",
        "pattern": pattern,
        "delta_days": delta,
        "drafted_at": today,
        "branch_a_rule_id": a["rule_id"],
        "branch_a_source": a["source_path"],
        "branch_a_last_verified": a["last_verified"],
        "branch_b_rule_id": b["rule_id"],
        "branch_b_source": b["source_path"],
        "branch_b_last_verified": b["last_verified"],
    }

    body_a = read_source_excerpt(a.get("abs_path", ""))
    body_b = read_source_excerpt(b.get("abs_path", ""))

    lines: list[str] = []
    lines.append("---")
    lines.append(render_yaml_block(fm))
    lines.append("---")
    lines.append("")
    lines.append("# Branch-Merge Candidate")
    lines.append("")
    lines.append(
        f"Two rules share `pattern: {pattern}` and have last_verified "
        f"dates within {delta} days of each other, but their `outcome` "
        f"fields contradict. Neither is clearly newer, so the resolver "
        "cannot pick a winner via validity-time precedence. An operator "
        "merge decision is required."
    )
    lines.append("")
    lines.append("## Branch A")
    lines.append("")
    lines.append(f"- rule_id: `{a['rule_id']}`")
    lines.append(f"- type: {a.get('type', 'unknown')}")
    lines.append(f"- last_verified: {a['last_verified'] or 'unknown'}")
    lines.append(f"- outcome: \"{a['outcome']}\"")
    lines.append(f"- source: [[{a['source_path']}]]")
    lines.append("")
    lines.append("Body excerpt:")
    lines.append("")
    lines.append("```")
    lines.append(body_a)
    lines.append("```")
    lines.append("")
    lines.append("## Branch B")
    lines.append("")
    lines.append(f"- rule_id: `{b['rule_id']}`")
    lines.append(f"- type: {b.get('type', 'unknown')}")
    lines.append(f"- last_verified: {b['last_verified'] or 'unknown'}")
    lines.append(f"- outcome: \"{b['outcome']}\"")
    lines.append(f"- source: [[{b['source_path']}]]")
    lines.append("")
    lines.append("Body excerpt:")
    lines.append("")
    lines.append("```")
    lines.append(body_b)
    lines.append("```")
    lines.append("")
    lines.append("## Contradiction")
    lines.append("")
    lines.append(
        f"Branch A outcome `{a['outcome']}` reads as opposite to branch B "
        f"outcome `{b['outcome']}`. Both share `pattern: {pattern}`, both "
        f"have a verifiable `last_verified` date, and the gap between "
        f"those dates is {delta} days (within the {30}-day branch-merge "
        "window)."
    )
    lines.append("")
    lines.append("## Operator options")
    lines.append("")
    lines.append("Pick exactly ONE of the three options below. Update the")
    lines.append("frontmatter `status:` field to record the decision.")
    lines.append("")
    lines.append("### Option 1: keep_branch_A")
    lines.append("")
    lines.append(
        f"Branch A wins. Mark branch B's file with `superseded_by: "
        f"{a['rule_id']}` and rerun `scripts/resolver-build.py`. Set this "
        "prompt's frontmatter to `status: resolved-keep-A`."
    )
    lines.append("")
    lines.append("### Option 2: keep_branch_B")
    lines.append("")
    lines.append(
        f"Branch B wins. Mark branch A's file with `superseded_by: "
        f"{b['rule_id']}` and rerun `scripts/resolver-build.py`. Set this "
        "prompt's frontmatter to `status: resolved-keep-B`."
    )
    lines.append("")
    lines.append("### Option 3: merge_into_new")
    lines.append("")
    lines.append(
        "Neither branch wins outright. Write a new typed-memory entry "
        "that captures the merged policy, then mark BOTH originals "
        "`superseded_by: <new-rule-id>`. Set this prompt's frontmatter to "
        "`status: resolved-merged`."
    )
    lines.append("")
    lines.append("Merged-decision template:")
    lines.append("")
    lines.append("```yaml")
    lines.append("type: decision")
    lines.append(f"pattern: {pattern}")
    lines.append("decision_date: <YYYY-MM-DD>")
    lines.append("outcome: <merged outcome that resolves both branches>")
    lines.append("stakes: <low | medium | high>")
    lines.append("speed: deliberate")
    lines.append("memory_class: episodic")
    lines.append("source_count: 2")
    lines.append("provenance:")
    lines.append(f"  - source_type: branch-merge")
    lines.append(f"    source_id: {a['rule_id']}")
    lines.append(f"  - source_type: branch-merge")
    lines.append(f"    source_id: {b['rule_id']}")
    lines.append(f"freshness_days: 180")
    lines.append(f"last_verified: <YYYY-MM-DD>")
    lines.append("```")
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=Path(os.environ.get("VAULT_ROOT", Path.cwd())),
        help="Vault root containing the Meta folder.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rendered prompts to stdout without writing.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite resolved prompts (skipped by default).",
    )
    args = parser.parse_args()

    vault_root = args.vault_root.resolve()
    aggregator = _load_aggregator()
    meta_dir = aggregator.find_meta_dir(vault_root)
    if meta_dir is None:
        print(f"ERROR: no Meta folder under {vault_root}", file=sys.stderr)
        return 1

    rules = aggregator.collect_rules(meta_dir)
    branch_merges = aggregator.detect_branch_merges(rules)

    out_dir = meta_dir / "Branch-Merge-Candidates"
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0
    for bm in branch_merges:
        filename = build_prompt_filename(
            bm["pattern"],
            bm["branch_a"]["rule_id"],
            bm["branch_b"]["rule_id"],
        )
        out_path = out_dir / filename

        existing_status = None
        if out_path.exists():
            try:
                existing_text = out_path.read_text(encoding="utf-8")
            except OSError:
                existing_text = ""
            m = re.search(r"^status:\s*([A-Za-z0-9_\-]+)", existing_text, re.M)
            if m:
                existing_status = m.group(1)
            if existing_status and existing_status.startswith("resolved") and not args.force:
                print(f"[skip] {out_path.name} status={existing_status} (pass --force to overwrite)")
                skipped += 1
                continue

        rendered = render_prompt(bm, existing_status=None)
        if args.dry_run:
            print(f"--- DRY RUN: would write {out_path} ---")
            print(rendered)
            continue
        out_path.write_text(rendered, encoding="utf-8")
        print(f"[wrote] {out_path}")
        written += 1

    print(
        f"\nProcessed {len(branch_merges)} candidate(s), "
        f"wrote {written}, skipped {skipped}."
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
