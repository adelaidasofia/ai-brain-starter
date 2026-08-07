#!/usr/bin/env python3
"""
drift-detection.py — Multi-edit semantic drift audit for vault files.

Microsoft DELEGATE-52 (arxiv.org/abs/2604.15597, Apr 2026): frontier LLMs
corrupt ~25% of professional content over 20 edit interactions; average
across all 19 tested models is ~50%. The paper concludes domain-specific
verification harnesses are the only reliable mitigation.

This script surfaces vault files edited MIN_EDITS+ times in the last
DAYS_BACK days as candidates for human review of semantic drift. Pure
git + Python by default (paper rules out LLM-as-judge: ≤25% variance
correlation with parsing-based metrics).

Optional `--semantic` mode adds Engram-inspired intent-shift judgment
via claude-haiku-4-5. The paper's caveat applies: LLM judgment is a
hint, not a verdict. Use it to *prioritize* candidates for human review.

Pair this script with check-rule-conflicts.py (cross-document clash at
write time) — drift is single-document shift over time. Different
signals, both needed.

Usage:
    python3 scripts/drift-detection.py
    python3 scripts/drift-detection.py --include "Meta/rules/*.md" --min-edits 3
    python3 scripts/drift-detection.py --semantic

Env overrides:
    VAULT_ROOT       Default: current working directory (must be a git repo).
    DRIFT_DAYS       Default: 30
    DRIFT_MIN_EDITS  Default: 5
    DRIFT_TOP_N      Default: 30
    ANTHROPIC_API_KEY required for --semantic.

Output:
    Meta/Drift Audit.md  (or whichever Meta/ folder convention your vault uses)
"""

import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT") or os.getcwd())

# Resolve Meta/ folder. Vaults vary: "Meta/", "⚙️ Meta/", "_meta/", etc.
# Pick the first one that exists; fall back to "Meta/" for new installs.
def _resolve_meta_dir():
    candidates = ("⚙️ Meta", "Meta", "_meta", "meta")
    for name in candidates:
        if (VAULT_ROOT / name).is_dir():
            return VAULT_ROOT / name
    return VAULT_ROOT / "Meta"


META_DIR = _resolve_meta_dir()

# Auto-generated, aggregated, or by-design accumulating paths. Skip to surface
# real drift signal (rules, runbooks, codified intent) rather than churn or
# accumulation. The goal is to flag rule files / canonical claims that drift,
# not journals or auto-aggregations that accumulate by design.
SKIP_PREFIXES = (
    f"{META_DIR.name}/Sessions/",
    f"{META_DIR.name}/Decisions/",
    f"{META_DIR.name}/Decision Log",
    f"{META_DIR.name}/Decision Log Archive",
    f"{META_DIR.name}/Last Session",
    f"{META_DIR.name}/Session Performance Log",
    f"{META_DIR.name}/Panel Feedback Log",
    f"{META_DIR.name}/Drift Audit",
    f"{META_DIR.name}/graphify-out/",
    "AI Chats/",
    "🤖 AI Chats/",
    "Journals/",
    "📓 Journals/",
)

DAYS_BACK = int(os.environ.get("DRIFT_DAYS", "30"))
MIN_EDITS = int(os.environ.get("DRIFT_MIN_EDITS", "5"))
TOP_N = int(os.environ.get("DRIFT_TOP_N", "30"))
INCLUDE_GLOB = "*.md"

SEMANTIC_MODEL = "claude-haiku-4-5-20251001"
SEMANTIC_MAX_DIFF_CHARS = 12000

SEMANTIC_DRIFT_PROMPT = """You are a semantic-drift auditor for a codified-rules corpus.

Given the original version of a rule file (FROM) and its current version (TO), determine whether the rule's *intent* has shifted, a guard has been softened, a numeric claim has drifted without sourcing, or whether the changes are merely refinement / clarification / additive.

VERDICT VALUES (return exactly one):
- "drift_detected"    — intent, guard strength, or canonical claim shifted in a meaningful way
- "softened"          — a guard was relaxed (block→warn, threshold loosened, exception added)
- "refinement"        — added detail, examples, sub-rules, or clarification; original intent preserved
- "additive"          — content added but no change to existing rules
- "neutral_rephrase"  — wording changed, semantics unchanged
- "uncertain"         — the diff is too ambiguous to judge without human review

OUTPUT FORMAT (JSON only, no prose, no fences):
{"verdict": "<value>", "evidence": "one short sentence pointing at the shift or noting why it's not a shift", "review_priority": "high|medium|low"}

Rules:
- review_priority "high" only if verdict in {drift_detected, softened}.
- review_priority "medium" for {uncertain}.
- review_priority "low" for {refinement, additive, neutral_rephrase}.
- Quote at most 10 words from the diff in evidence. Do not regurgitate large chunks.
- Output JSON only. No preamble, no markdown fences."""


def get_anthropic_key():
    """Locate Anthropic API key from common locations."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    candidates = [
        Path.home() / ".zsh_secrets",
        Path.home() / ".env",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "ANTHROPIC_API_KEY" in line and "=" in line:
                    if line.lower().startswith("export "):
                        line = line[7:].strip()
                    _, _, value = line.partition("=")
                    return value.strip().strip('"').strip("'")
        except OSError:
            continue
    return None


def judge_semantic_drift(client, file_path, diff_text):
    truncated = diff_text[:SEMANTIC_MAX_DIFF_CHARS]
    if len(diff_text) > SEMANTIC_MAX_DIFF_CHARS:
        truncated += f"\n\n... [diff truncated at {SEMANTIC_MAX_DIFF_CHARS} chars]"
    user_text = (
        f"# File\n{file_path}\n\n"
        f"# Diff (FROM first commit in window → HEAD)\n```diff\n{truncated}\n```\n\n"
        "Return the JSON verdict only."
    )
    try:
        response = client.messages.create(
            model=SEMANTIC_MODEL,
            max_tokens=400,
            system=[
                {
                    "type": "text",
                    "text": SEMANTIC_DRIFT_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as e:
        print(f"  semantic judge failed for {file_path}: {e}", file=sys.stderr)
        return None
    raw = response.content[0].text.strip() if response.content else ""
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  semantic verdict not valid JSON for {file_path}: {raw[:200]}", file=sys.stderr)
        return None
    return {
        "verdict": payload.get("verdict", "uncertain"),
        "evidence": payload.get("evidence", "")[:500],
        "review_priority": payload.get("review_priority", "medium"),
    }


def git(args):
    r = subprocess.run(
        ["git", "-c", "core.quotepath=off"] + args,
        cwd=str(VAULT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Multi-edit drift audit.")
    parser.add_argument("--include", default=INCLUDE_GLOB, help=f"Pathspec glob. Default: '{INCLUDE_GLOB}'.")
    parser.add_argument("--days", type=int, default=DAYS_BACK, help=f"Look-back days. Default: {DAYS_BACK}.")
    parser.add_argument("--min-edits", type=int, default=MIN_EDITS, help=f"Min edit count. Default: {MIN_EDITS}.")
    parser.add_argument("--top", type=int, default=TOP_N, help=f"Top N candidates. Default: {TOP_N}.")
    parser.add_argument("--semantic", action="store_true", help="Add LLM-judged intent-shift verdict via claude-haiku-4-5. Skips silently if ANTHROPIC_API_KEY missing.")
    args = parser.parse_args()

    if not (VAULT_ROOT / ".git").exists():
        print(f"No .git at {VAULT_ROOT}. Set VAULT_ROOT to a git-tracked vault.", file=sys.stderr)
        return 2

    log = git([
        "log",
        f"--since={args.days}.days.ago",
        "--name-only",
        "--pretty=format:COMMIT",
        "--",
        args.include,
    ])

    if not log.strip():
        print(f"No vault commits in last {args.days} days.")
        return 0

    edits = Counter()
    for line in log.splitlines():
        s = line.strip()
        if not s or s == "COMMIT":
            continue
        if any(s.startswith(p) for p in SKIP_PREFIXES):
            continue
        edits[s] += 1

    candidates = [(p, n) for p, n in edits.items() if n >= args.min_edits]
    candidates.sort(key=lambda x: -x[1])
    candidates = candidates[:args.top]

    semantic_client = None
    semantic_skipped_reason = None
    if args.semantic:
        api_key = get_anthropic_key()
        if not api_key:
            semantic_skipped_reason = "no ANTHROPIC_API_KEY in env or ~/.zsh_secrets"
            print(f"semantic mode skipped: {semantic_skipped_reason}", file=sys.stderr)
        else:
            try:
                from anthropic import Anthropic
                semantic_client = Anthropic(api_key=api_key)
            except ImportError:
                semantic_skipped_reason = "anthropic SDK not installed (pip install anthropic)"
                print(f"semantic mode skipped: {semantic_skipped_reason}", file=sys.stderr)

    rows = []
    for path, n in candidates:
        commits = git([
            "log",
            f"--since={args.days}.days.ago",
            "--pretty=format:%H",
            "--",
            path,
        ]).strip().splitlines()
        if not commits:
            continue
        first = commits[-1]
        diff = git([
            "diff",
            "--shortstat",
            f"{first}^..HEAD",
            "--",
            path,
        ]).strip() or "(no net change)"

        full = VAULT_ROOT / path
        size_bytes = full.stat().st_size if full.exists() else 0
        size_kb = round(size_bytes / 1024, 1)

        row = {
            "path": path,
            "edits": n,
            "first": first[:8],
            "diff": diff,
            "size_kb": size_kb,
            "semantic": None,
        }

        if semantic_client is not None:
            full_diff = git(["diff", f"{first}^..HEAD", "--", path])
            if full_diff.strip():
                row["semantic"] = judge_semantic_drift(semantic_client, path, full_diff)

        rows.append(row)

    META_DIR.mkdir(parents=True, exist_ok=True)
    out = META_DIR / "Drift Audit.md"
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    body = [
        "---",
        f"creationDate: {today}",
        "type: meta",
        f"purpose: Multi-edit drift audit. Files edited {args.min_edits}+ times in last {args.days} days. Include: '{args.include}'.",
        "generator: scripts/drift-detection.py",
        "---",
        "",
        "# Drift Audit",
        "",
        f"*Generated: {now}*",
        "",
        f"Microsoft DELEGATE-52 ([arxiv.org/abs/2604.15597](https://arxiv.org/abs/2604.15597)): frontier LLMs corrupt ~25% of professional content over 20 edits, ~50% average across all tested models. Document size amplifies degradation 5× (1k vs 10k tokens at 20 interactions).",
        "",
        f"This audit surfaces vault files edited {args.min_edits}+ times in the last {args.days} days (scope: `{args.include}`) for human review of semantic drift.",
        "",
        f"Skipped prefixes (auto-generated, journals, sessions): `{', '.join(SKIP_PREFIXES)}`.",
        "",
        f"## Top {len(rows)} candidates",
        "",
    ]
    if rows:
        if args.semantic and semantic_client is not None:
            body.append("| File | Path | Edits | Size | First | Diff | Verdict | Priority |")
            body.append("|---|---|---|---|---|---|---|---|")
            for r in rows:
                wikilink = f"[[{Path(r['path']).stem}]]"
                sem = r.get("semantic")
                if sem:
                    verdict, priority = sem["verdict"], sem["review_priority"]
                else:
                    verdict, priority = "_(no judge)_", "—"
                body.append(
                    f"| {wikilink} | `{r['path']}` | {r['edits']} | {r['size_kb']} KB | `{r['first']}` | {r['diff']} | {verdict} | {priority} |"
                )
            high = [r for r in rows if (r.get("semantic") or {}).get("review_priority") == "high"]
            if high:
                body.append("")
                body.append(f"## {len(high)} HIGH-priority semantic findings")
                body.append("")
                for r in high:
                    sem = r["semantic"]
                    body.append(f"- **`{r['path']}`** — {sem['verdict']}: {sem['evidence']}")
                body.append("")
        else:
            body.append("| File | Path | Edits | Size | First | Diff |")
            body.append("|---|---|---|---|---|---|")
            for r in rows:
                wikilink = f"[[{Path(r['path']).stem}]]"
                body.append(f"| {wikilink} | `{r['path']}` | {r['edits']} | {r['size_kb']} KB | `{r['first']}` | {r['diff']} |")
            if args.semantic and semantic_skipped_reason:
                body.append("")
                body.append(f"_Semantic mode requested but skipped: {semantic_skipped_reason}_")
    else:
        body.append("_No candidates met the threshold._")

    body += [
        "",
        "## How to review",
        "",
        "For each candidate that looks suspicious, run:",
        "```bash",
        f'cd "{VAULT_ROOT}"',
        "git diff <first>^..HEAD -- '<file>'",
        "```",
        "",
        "Ask:",
        "1. Did the rule's intent shift, or just get rephrased?",
        "2. Did a canonical fact change without a `Decisions/` entry?",
        "3. Did a guard get softened (warn → silent, block → warn, threshold loosened)?",
        "4. Did a numeric claim drift across edits (TAM, headcount, runway, dates)?",
        "",
        "If any answer is yes, restore from `<first>` or write a `Decisions/` entry that records the intentional shift.",
        "",
        "## Why this exists",
        "",
        "DELEGATE-52 finding: cumulative drift across many edits is the failure mode no other guard catches. Hookify rules block at write-time. PreToolUse hooks gate dangerous tools. Static checks gate frontmatter. None of those see a rule that gets edited 12 times in 3 weeks and ends up meaning something subtly different than it started.",
        "",
        "Drift detection closes that loop. Run weekly from `/sunday-review`, or any time you suspect a high-edit document has wandered. Pair with `check-rule-conflicts.py` for cross-document clash detection.",
        "",
    ]

    out.write_text("\n".join(body), encoding="utf-8")
    print(f"Wrote {out} ({len(rows)} drift candidates)")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
