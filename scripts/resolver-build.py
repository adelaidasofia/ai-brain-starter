#!/usr/bin/env python3
"""
resolver-build.py builds Meta/RESOLVER.md, the Bi-Temporal Resolver index.

RESOLVER.md is the single file that routes natural-language queries to
executable skills with current policy constraints. It is the catalect
aggregator across the four typed-memory primitives that carry routable
rules: decisions, workflows, exceptions, facts.

Each row in the rendered table represents one rule with its bi-temporal
status:

  validity-time     = decision_date / last_verified (when the rule
                      describes the world)
  transaction-time  = git commit time of the source file (when the rule
                      was written into the vault)

Status logic:
  active        = last_verified is set and (today - last_verified) <= freshness_days
  stale         = last_verified is set and (today - last_verified) > freshness_days
  superseded    = same pattern + overlapping subject as another rule with a
                  more recent last_verified (validity-time precedence)
  under-review  = outcome field is non-blank but pattern field is empty
                  (decision resolved but lesson not yet extracted)
  unknown       = neither last_verified nor outcome/pattern signal applies

Conflict detection:
  Two rules conflict when they share the same `pattern` field AND their
  subjects overlap. Subject is derived from the source file's first H1
  plus the frontmatter `topic` or `subject` field. Overlap is a simple
  case-folded keyword match. Among a conflicting set, the rule with the
  most recent `last_verified` wins; older rules are marked
  status: superseded with a `superseded_by` field naming the winner.

The aggregator walks Meta/Decisions/, Meta/Workflows/, Meta/Exceptions/,
Meta/Facts/ inside --vault-root, parses YAML frontmatter on every .md file,
and emits a single Meta/RESOLVER.md as a stable, single-Read-readable index.

Usage:
  python3 scripts/resolver-build.py --vault-root PATH [--out PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir  # noqa: E402


# Folder names the aggregator walks. The auto-detect supports both
# emoji-prefixed ("⚙️ Meta") and plain ("Meta") layouts.
META_BASENAMES = ("Meta",)
TYPED_FOLDERS = ("Decisions", "Workflows", "Exceptions", "Facts")
TYPE_BY_FOLDER = {
    "Decisions": "decision",
    "Workflows": "workflow",
    "Exceptions": "exception",
    "Facts": "fact",
}

# Branch-merge window. Two decisions are treated as parallel branches
# (rather than a clean supersession) when they share `pattern`, contradict
# in outcome, and their last_verified dates are within this many days.
BRANCH_MERGE_WINDOW_DAYS = 30


def parse_frontmatter(text: str) -> dict[str, Any] | None:
    """Return the YAML frontmatter dict, or None if missing/unparseable."""
    if not text.startswith("---"):
        return None
    m = re.match(r"^---\n(.*?)\n---\s*", text, re.DOTALL)
    if not m:
        return None
    try:
        import yaml  # PyYAML
    except ImportError:
        print("ERROR: PyYAML required (pip install pyyaml)", file=sys.stderr)
        sys.exit(1)
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return _stringify_dates(data)


def _stringify_dates(obj: Any) -> Any:
    """PyYAML auto-converts dates; we want strings for date arithmetic."""
    if isinstance(obj, dict):
        return {k: _stringify_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify_dates(v) for v in obj]
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, dt.date):
        return obj.isoformat()
    return obj


def parse_iso_date(value: Any) -> dt.date | None:
    """Best-effort parse of an ISO-style date or datetime string."""
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if len(s) >= 10:
        head = s[:10]
        try:
            return dt.date.fromisoformat(head)
        except ValueError:
            return None
    return None


def derive_status(fm: dict[str, Any], today: dt.date) -> str:
    """Map a frontmatter dict to one of: active, stale, under-review, unknown."""
    last_verified = parse_iso_date(fm.get("last_verified"))
    freshness = fm.get("freshness_days")
    if last_verified is not None and isinstance(freshness, int) and freshness >= 0:
        delta = (today - last_verified).days
        if delta > freshness:
            return "stale"
        return "active"

    outcome = fm.get("outcome")
    pattern = fm.get("pattern")
    if isinstance(outcome, str) and outcome.strip() and (
        pattern is None or (isinstance(pattern, str) and not pattern.strip())
    ):
        return "under-review"

    return "unknown"


def derive_rule_id(file_path: Path, fm: dict[str, Any]) -> str:
    """Stable rule_id: prefer explicit field, else file stem."""
    explicit = fm.get("rule_id") or fm.get("id") or fm.get("name")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return file_path.stem


def derive_skill_link(fm: dict[str, Any]) -> str:
    """Pull an executable-skill pointer from common frontmatter fields."""
    for key in ("skill", "executable_skill", "routes_to", "skill_id"):
        val = fm.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def derive_first_h1(text: str) -> str:
    """Return the first H1 heading from the markdown body, lowercase."""
    body = text
    if text.startswith("---"):
        m = re.match(r"^---\n.*?\n---\s*", text, re.DOTALL)
        if m:
            body = text[m.end():]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip().lower()
    return ""


def derive_subject_tokens(fm: dict[str, Any], h1: str) -> set[str]:
    """Return a set of normalized subject tokens for overlap matching.

    Tokens come from frontmatter `topic` or `subject` (string or list)
    and from the first H1 of the markdown body. All tokens are lower-cased
    and split on whitespace. Empty strings filtered out.
    """
    tokens: set[str] = set()
    for key in ("topic", "subject"):
        val = fm.get(key)
        if isinstance(val, str):
            for tok in val.lower().split():
                if tok:
                    tokens.add(tok)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    for tok in item.lower().split():
                        if tok:
                            tokens.add(tok)
    if h1:
        for tok in h1.split():
            if tok:
                tokens.add(tok)
    return tokens


def derive_pattern(fm: dict[str, Any]) -> str:
    """Return the normalized `pattern` field, or empty string."""
    val = fm.get("pattern")
    if isinstance(val, str):
        return val.strip().lower()
    return ""


def collect_rules(meta_dir: Path) -> list[dict[str, Any]]:
    """Walk the four typed-memory folders, emit one row per parseable file."""
    rules: list[dict[str, Any]] = []
    today = dt.date.today()

    for folder_name in TYPED_FOLDERS:
        folder = meta_dir / folder_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = parse_frontmatter(text)
            if fm is None:
                continue

            h1 = derive_first_h1(text)
            rules.append({
                "rule_id": derive_rule_id(path, fm),
                "type": TYPE_BY_FOLDER[folder_name],
                "status": derive_status(fm, today),
                "last_verified": str(fm.get("last_verified") or ""),
                "last_verified_date": parse_iso_date(fm.get("last_verified")),
                "freshness_days": fm.get("freshness_days") if isinstance(
                    fm.get("freshness_days"), int
                ) else "",
                "owner": str(fm.get("owner") or ""),
                "source_path": str(path.relative_to(meta_dir.parent)),
                "skill_link": derive_skill_link(fm),
                "pattern": derive_pattern(fm),
                "subject_tokens": derive_subject_tokens(fm, h1),
                "h1": h1,
                "outcome": str(fm.get("outcome") or "").strip(),
                "abs_path": str(path),
                "superseded_by": "",
                "branch_merge_with": "",
            })

    return rules


_NEGATION_TOKENS = {
    "no", "not", "never", "without", "stop", "kill", "block", "deny",
    "reject", "off", "disable", "drop", "skip", "decline", "exclude",
    "abort",
}
_AFFIRM_TOKENS = {
    "yes", "always", "ship", "approve", "approved", "enable", "allow",
    "permit", "include", "go", "launch", "publish", "accept",
}


def _outcome_polarity(outcome: str) -> tuple[set[str], set[str]]:
    """Split an outcome into (negation_tokens, affirm_tokens) seen.

    A naive contradiction signal: when one outcome carries negation tokens
    the other does not, and the other carries affirmation tokens the first
    does not, treat them as opposites. This is intentionally cheap; the
    operator review at the merge prompt is where real reconciliation happens.
    """
    text = (outcome or "").lower()
    tokens = set(re.findall(r"[a-z][a-z0-9'-]*", text))
    neg = tokens & _NEGATION_TOKENS
    aff = tokens & _AFFIRM_TOKENS
    return neg, aff


def outcomes_contradict(a: str, b: str) -> bool:
    """Return True when two outcome strings look like opposites.

    Cheap signal: one carries a negation lexeme the other does not, and the
    other carries an affirmation lexeme the first does not. Or, neither
    carries affirmations and one carries negations the other lacks. Falls
    back to a token-overlap check: when stems share <40% of non-stopword
    tokens AND both are non-empty, treat as contradicting too.
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return False
    if a.lower() == b.lower():
        return False

    neg_a, aff_a = _outcome_polarity(a)
    neg_b, aff_b = _outcome_polarity(b)
    if (neg_a and not neg_b and (aff_b or not aff_a)) or (
        neg_b and not neg_a and (aff_a or not aff_b)
    ):
        return True
    if aff_a and aff_b and not (neg_a or neg_b):
        return False

    stop = {
        "the", "a", "an", "is", "are", "was", "were", "be", "to", "of",
        "and", "or", "for", "with", "in", "on", "at", "by", "this", "that",
    }
    ta = {t for t in re.findall(r"[a-z][a-z0-9'-]*", a.lower()) if t not in stop}
    tb = {t for t in re.findall(r"[a-z][a-z0-9'-]*", b.lower()) if t not in stop}
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / max(len(ta), len(tb))
    return overlap < 0.40


def detect_branch_merges(
    rules: list[dict[str, Any]],
    window_days: int = BRANCH_MERGE_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Find pairs of rules that share `pattern` AND contradict in outcome
    AND have last_verified within `window_days` of each other.

    These are NOT supersessions (neither is clearly newer); they are parallel
    branches that need an operator merge. Returns one record per pair:

      {
        "pattern": "...",
        "branch_a": {"rule_id", "source_path", "abs_path",
                     "last_verified", "outcome", "type"},
        "branch_b": {...},
        "delta_days": int,
      }

    Side effect: each branch rule gets `branch_merge_with` set to the other
    rule_id so the rendered table can carry the marker. Status is left
    untouched (these are not superseded).
    """
    branches: list[dict[str, Any]] = []
    by_pattern: dict[str, list[dict[str, Any]]] = {}
    for r in rules:
        if not r["pattern"] or not r.get("outcome"):
            continue
        if not r.get("last_verified_date"):
            continue
        by_pattern.setdefault(r["pattern"], []).append(r)

    seen: set[tuple[str, str]] = set()
    for pattern, group in by_pattern.items():
        n = len(group)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = group[i], group[j]
                shared = a["subject_tokens"] & b["subject_tokens"]
                if not shared:
                    continue
                if not outcomes_contradict(a["outcome"], b["outcome"]):
                    continue
                delta = abs((a["last_verified_date"] - b["last_verified_date"]).days)
                if delta > window_days:
                    continue
                pair = tuple(sorted([a["rule_id"], b["rule_id"]]))
                if pair in seen:
                    continue
                seen.add(pair)
                a["branch_merge_with"] = b["rule_id"]
                b["branch_merge_with"] = a["rule_id"]
                branches.append({
                    "pattern": pattern,
                    "branch_a": {
                        "rule_id": a["rule_id"],
                        "source_path": a["source_path"],
                        "abs_path": a.get("abs_path", ""),
                        "last_verified": a["last_verified"],
                        "outcome": a["outcome"],
                        "type": a["type"],
                    },
                    "branch_b": {
                        "rule_id": b["rule_id"],
                        "source_path": b["source_path"],
                        "abs_path": b.get("abs_path", ""),
                        "last_verified": b["last_verified"],
                        "outcome": b["outcome"],
                        "type": b["type"],
                    },
                    "delta_days": delta,
                })
    return branches


def detect_conflicts(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find pairs of rules that share a non-empty `pattern` AND overlap on
    subject tokens.

    Returns a list of conflict records:
      {pattern, members: [rule_id, ...], winner_rule_id, superseded: [rule_id, ...]}

    Side effects on the input list: the older rule(s) in each conflict
    group get status='superseded' and superseded_by=winner_rule_id.
    """
    conflicts: list[dict[str, Any]] = []
    rules_by_pattern: dict[str, list[dict[str, Any]]] = {}
    for r in rules:
        if r["pattern"]:
            rules_by_pattern.setdefault(r["pattern"], []).append(r)

    seen_pairs: set[tuple[str, str]] = set()

    for pattern, group in rules_by_pattern.items():
        if len(group) < 2:
            continue
        # Build adjacency by subject-token overlap.
        # A pair conflicts when shared tokens > 0.
        n = len(group)
        adjacency: list[set[int]] = [set() for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                shared = group[i]["subject_tokens"] & group[j]["subject_tokens"]
                if shared:
                    adjacency[i].add(j)
                    adjacency[j].add(i)

        # Connected-component grouping.
        visited = [False] * n
        for start in range(n):
            if visited[start]:
                continue
            component: list[int] = []
            stack = [start]
            while stack:
                idx = stack.pop()
                if visited[idx]:
                    continue
                visited[idx] = True
                component.append(idx)
                for nb in adjacency[idx]:
                    if not visited[nb]:
                        stack.append(nb)
            if len(component) < 2:
                continue

            members = [group[i] for i in component]
            # Winner = most recent last_verified_date. Ties -> rule_id sort.
            def sort_key(r: dict[str, Any]) -> tuple:
                lvd = r.get("last_verified_date")
                # Rules without a date sort oldest.
                rank = lvd.toordinal() if lvd is not None else -1
                return (-rank, r["rule_id"])

            members_sorted = sorted(members, key=sort_key)
            winner = members_sorted[0]
            losers = members_sorted[1:]

            # Annotate losers in-place. Only override status if it would
            # demote (active/unknown/under-review -> superseded). We keep
            # 'stale' for visibility but still mark superseded_by.
            for loser in losers:
                loser["status"] = "superseded"
                loser["superseded_by"] = winner["rule_id"]

            # Stable de-dupe: skip if we have already seen this exact group.
            sig = tuple(sorted(m["rule_id"] for m in members))
            if sig in seen_pairs:
                continue
            seen_pairs.add(sig)

            conflicts.append({
                "pattern": pattern,
                "members": [m["rule_id"] for m in members_sorted],
                "winner_rule_id": winner["rule_id"],
                "winner_source": winner["source_path"],
                "winner_last_verified": winner["last_verified"],
                "superseded": [
                    {
                        "rule_id": m["rule_id"],
                        "source_path": m["source_path"],
                        "last_verified": m["last_verified"],
                    }
                    for m in losers
                ],
            })

    return conflicts


def render_resolver(
    rules: list[dict[str, Any]],
    vault_root: Path,
    conflicts: list[dict[str, Any]] | None = None,
    branch_merges: list[dict[str, Any]] | None = None,
) -> str:
    """Emit the stable RESOLVER.md body."""
    today = dt.date.today().isoformat()
    counts = {
        "active": 0,
        "stale": 0,
        "superseded": 0,
        "under-review": 0,
        "unknown": 0,
    }
    for r in rules:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    conflicts = conflicts or []
    branch_merges = branch_merges or []

    lines: list[str] = []
    lines.append("---")
    lines.append("type: resolver")
    lines.append(f"last_built: {today}")
    lines.append(f"vault_root: {vault_root}")
    lines.append(f"conflict_count: {len(conflicts)}")
    lines.append(f"branch_merge_count: {len(branch_merges)}")
    lines.append("---")
    lines.append("")
    lines.append("# RESOLVER")
    lines.append("")
    lines.append(
        "RESOLVER.md is the single file that routes natural-language "
        "queries to executable skills with current policy constraints."
    )
    lines.append("")
    lines.append(
        "Auto-generated by `scripts/resolver-build.py`. Do not edit by "
        "hand. The aggregator walks `Meta/Decisions/`, `Meta/Workflows/`, "
        "`Meta/Exceptions/`, `Meta/Facts/`, parses YAML frontmatter on "
        "each `.md` file, and renders one row per rule. Rerun the script "
        "after any rule changes."
    )
    lines.append("")
    lines.append("## Bi-temporal model")
    lines.append("")
    lines.append(
        "Each row carries two clocks. The validity-time clock is "
        "`last_verified` (or `decision_date`): when the rule was last "
        "confirmed to describe the world. The transaction-time clock is "
        "the git commit time on the source file: when the rule was "
        "written into the vault. A rule is `stale` when "
        "`(today - last_verified) > freshness_days`, regardless of when "
        "it was committed."
    )
    lines.append("")
    lines.append(
        "When two rules collide on the same `pattern` and overlapping "
        "subject tokens, the one with the more recent `last_verified` "
        "wins. The older rule is marked `superseded` and the row carries "
        "a `superseded_by` pointer at the winner. Validity-time precedence."
    )
    lines.append("")
    lines.append("## Status counts")
    lines.append("")
    lines.append(f"- active: {counts['active']}")
    lines.append(f"- stale: {counts['stale']}")
    lines.append(f"- superseded: {counts['superseded']}")
    lines.append(f"- under-review: {counts['under-review']}")
    lines.append(f"- unknown: {counts['unknown']}")
    lines.append(f"- total rules: {len(rules)}")
    lines.append(f"- conflict groups: {len(conflicts)}")
    lines.append(f"- branch-merge candidates: {len(branch_merges)}")
    lines.append("")

    if branch_merges:
        lines.append("## Branch-merge candidates")
        lines.append("")
        lines.append(
            "Each pair below shares a `pattern`, contradicts in `outcome`, "
            "and has `last_verified` within "
            f"{BRANCH_MERGE_WINDOW_DAYS} days. Neither is clearly newer, so "
            "this is not a supersession; an operator merge decision is "
            "required. Run `scripts/resolver-branch-merge-prompt.py` to "
            "draft a per-pair merge prompt."
        )
        lines.append("")
        for bm in branch_merges:
            lines.append(f"### Pattern: `{bm['pattern']}` (delta {bm['delta_days']}d)")
            lines.append("")
            for label, branch in (("branch A", bm["branch_a"]), ("branch B", bm["branch_b"])):
                lines.append(
                    f"- {label}: `{branch['rule_id']}` "
                    f"(last_verified {branch['last_verified'] or 'unknown'}, "
                    f"outcome \"{branch['outcome']}\") "
                    f"[[{branch['source_path']}]]"
                )
            lines.append("")

    if conflicts:
        lines.append("## Conflicts")
        lines.append("")
        lines.append(
            "Each group below shares a `pattern` and overlaps on subject "
            "tokens. The winner row keeps its current status. Loser rows "
            "are marked `superseded` and carry `superseded_by` in the "
            "Rules table."
        )
        lines.append("")
        for c in conflicts:
            lines.append(f"### Pattern: `{c['pattern']}`")
            lines.append("")
            lines.append(
                f"- winner: `{c['winner_rule_id']}` "
                f"(last_verified {c['winner_last_verified'] or 'unknown'}) "
                f"[[{c['winner_source']}]]"
            )
            for loser in c["superseded"]:
                lines.append(
                    f"- superseded: `{loser['rule_id']}` "
                    f"(last_verified {loser['last_verified'] or 'unknown'}) "
                    f"[[{loser['source_path']}]]"
                )
            lines.append("")

    lines.append("## Rules")
    lines.append("")
    lines.append(
        "| rule_id | type | status | last_verified | freshness_days | "
        "owner | source | skill | superseded_by | branch_merge_with |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|---|---|"
    )

    if not rules:
        lines.append("| (no rules found) |  |  |  |  |  |  |  |  |  |")
    else:
        for r in sorted(
            rules,
            key=lambda x: (
                {
                    "stale": 0,
                    "superseded": 1,
                    "under-review": 2,
                    "active": 3,
                    "unknown": 4,
                }.get(x["status"], 5),
                x["type"],
                x["rule_id"],
            ),
        ):
            source_link = f"[[{r['source_path']}]]"
            skill = f"[[{r['skill_link']}]]" if r["skill_link"] else ""
            superseded_by = (
                f"`{r['superseded_by']}`" if r.get("superseded_by") else ""
            )
            branch_merge = (
                f"`{r['branch_merge_with']}`" if r.get("branch_merge_with") else ""
            )
            lines.append(
                f"| {r['rule_id']} | {r['type']} | {r['status']} | "
                f"{r['last_verified']} | {r['freshness_days']} | "
                f"{r['owner']} | {source_link} | {skill} | {superseded_by} | "
                f"{branch_merge} |"
            )

    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build Meta/RESOLVER.md from typed-memory primitives "
            "(decisions, workflows, exceptions, facts)."
        )
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=Path(os.environ.get("VAULT_ROOT", Path.cwd())),
        help="Vault root containing the Meta folder.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Override output path. Default: <meta>/RESOLVER.md.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rendered file without writing to disk.",
    )
    args = parser.parse_args()

    vault_root = args.vault_root.resolve()
    meta_dir = find_meta_dir(vault_root)
    if meta_dir is None:
        print(
            f"ERROR: no Meta folder under {vault_root}.",
            file=sys.stderr,
        )
        return 1

    rules = collect_rules(meta_dir)
    conflicts = detect_conflicts(rules)
    branch_merges = detect_branch_merges(rules)
    rendered = render_resolver(rules, vault_root, conflicts, branch_merges)

    out_path = args.out if args.out is not None else (meta_dir / "RESOLVER.md")

    if args.dry_run:
        print(f"--- DRY RUN: would write {out_path} ---")
        print(rendered)
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    print(
        f"Wrote {out_path} ({len(rules)} rule(s), "
        f"{len(conflicts)} conflict group(s), "
        f"{len(branch_merges)} branch-merge candidate(s), "
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
