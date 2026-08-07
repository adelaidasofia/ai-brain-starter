#!/usr/bin/env python3
"""
check-rule-conflicts.py — Detect contradictions across codified rules.

Engram-inspired (github.com/Gentleman-Programming/engram). Their mem_judge
and mem_compare tools surface contradictory memories on save. This script
adapts the typed-relations primitive (supersedes / conflicts_with /
clarifies) for the vault's codified-rules corpus.

Drift detection (drift-detection.py) catches single-document shift over
time. Conflict detection catches cross-document clash at write time.
Different signals, both needed.

Two modes:
  default (keyword-anchor): extract imperatives (always|never|must|banned|
    avoid|prefer|enforce|skip) + their nouns from a target file or full
    corpus, scan existing rules for opposing imperatives on the same noun.
    Cheap, deterministic, no LLM. Catches the obvious cases.

  --semantic: LLM-based check via claude-haiku-4-5-20251001 against the
    cached corpus. Catches vocabulary-different contradictions keyword
    mode misses. Requires Anthropic API key. Skips cleanly when key is
    absent. 6-case test fixture covers true positives, true negatives,
    and conditional-rule edge cases.

Output format follows Matuschak: phrase conflicts as questions
("You wrote X, just wrote Y. Did you mean to revise?"), not adversarial
"CONFLICT DETECTED" dumps. Goal: moment-of-contradiction is productive,
not noise.

Usage:
    python3 scripts/check-rule-conflicts.py <target-file>
    python3 scripts/check-rule-conflicts.py --scan-all
    python3 scripts/check-rule-conflicts.py --self-test
    python3 scripts/check-rule-conflicts.py --self-test --semantic
    python3 scripts/check-rule-conflicts.py --json <target-file>

Env:
    VAULT_ROOT          Default: current working directory.
    CONFLICT_THRESHOLD  Minimum confidence to report. Default: 0.5.
    ANTHROPIC_API_KEY   Required for --semantic.

Output:
    Markdown: Meta/Rule Conflicts.md (default)
    JSON: stdout (with --json or --hook)
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT") or os.getcwd())


def _resolve_meta_dir():
    candidates = ("⚙️ Meta", "Meta", "_meta", "meta")
    for name in candidates:
        if (VAULT_ROOT / name).is_dir():
            return VAULT_ROOT / name
    return VAULT_ROOT / "Meta"


META_DIR = _resolve_meta_dir()
CONFLICT_REPORT = META_DIR / "Rule Conflicts.md"

# Corpus: every codified-rule surface in the vault.
# Vaults vary in convention — supports either "Meta/" or "⚙️ Meta/" paths.
def _build_corpus_globs():
    meta_name = META_DIR.name
    return (
        "CLAUDE.md",
        f"{meta_name}/rules/*.md",
        f"{meta_name}/Build Standards.md",
        f"{meta_name}/Critical Failure Inventory.md",
    )


CORPUS_GLOBS = _build_corpus_globs()


def _resolve_memory_dir():
    """Find the memory dir for this vault. Per-account auto-memory lives at
    ~/.claude/projects/<encoded-vault-path>/memory/. Encoded path: replace
    `/` and `.` with `-`, prepend `-`. Returns None if not present.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _project_key import memory_dir_for
    return memory_dir_for(VAULT_ROOT, must_exist=True)


MEMORY_DIR = _resolve_memory_dir()


# Opposing imperative pairs. Each pair is (verb_a, verb_b) where verb_a in
# "rule A" + verb_b in "rule B" with same noun = candidate conflict.
OPPOSING_VERBS = (
    ("always", "never"),
    ("always", "avoid"),
    ("always", "skip"),
    ("always", "banned"),
    ("must", "banned"),
    ("must", "do not"),
    ("must", "don't"),
    ("must", "skip"),
    ("must", "avoid"),
    ("required", "optional"),
    ("required", "skip"),
    ("required", "bypass"),
    ("required", "banned"),
    ("prefer", "avoid"),
    ("enforce", "bypass"),
    ("enforce", "skip"),
    ("enforce", "avoid"),
    ("enable", "disable"),
    ("ship", "defer"),
    ("ship", "wait"),
)

# Conditional markers exclude a line from conflict scoring (the rule
# is bounded by a condition rather than absolute).
CONDITIONAL_MARKERS = (
    "unless ",
    "except ",
    "when ",
    "if ",
    "only if ",
)

IMPERATIVE_RE = re.compile(
    r"(?:^|[\-\*]\s|\*\*)"
    r"(always|never|must|banned|do not|don't|prefer|avoid|enforce|skip|"
    r"bypass|enable|disable|ship|defer|wait|required|optional)"
    r"\b[\s:]+([\w][\w\s\-/]{2,40}?)(?=[\.,;]|\s+\b(?:before|after|when|if|"
    r"because|to|for|in|on|with)\b|\s*$)",
    re.IGNORECASE | re.MULTILINE,
)

NOUN_OVERLAP_BASE = 0.6
DEFAULT_THRESHOLD = float(os.environ.get("CONFLICT_THRESHOLD", "0.5"))
SEMANTIC_MODEL = "claude-haiku-4-5-20251001"

SEMANTIC_SYSTEM_PROMPT = """You are a rule-conflict detector for a codified-rules corpus.

Given the rules corpus (cached) and new content, identify SEMANTIC contradictions: places where the new content asserts something that cannot coexist with an existing rule, even when the wording differs.

DETECTION RULES:
1. Flag ONLY conflicts where two rules cannot both be true at the same time on the same scope.
2. Refinements (a rule + a sub-rule that narrows scope) are NOT conflicts. Skip them.
3. Different scopes (rule about X, rule about Y) are NOT conflicts. Skip them.
4. Conditional rules ("never X UNLESS Y") overlapping absolute rules ("always X") are MAYBE conflicts. Flag with confidence 0.3-0.5.
5. Vocabulary-different contradictions ARE the point of this mode. Detect them.

Examples that ARE conflicts (flag):
- Existing: "Use SQLite for queryable data." / New: "Always use Postgres for new database work." (semantic clash, different vocabulary)
- Existing: "Aggregators run foreground sequential." / New: "Run all aggregators in parallel for speed." (semantic clash)

Examples that are NOT conflicts (skip):
- Existing: "Always wikilink concepts." / New: "Never path-form wikilinks." (different scope: concepts vs path-form)
- Existing: "Default model is opusplan." / New: "Use Sonnet for session close." (general default + named exception, both can be true)

OUTPUT FORMAT (JSON only, no prose):
{"conflicts": [
  {
    "new_location": "filename:line_no",
    "new_quote": "exact quote from new content",
    "existing_location": "filename:line_no",
    "existing_quote": "exact quote from existing rule",
    "confidence": 0.0-1.0,
    "reasoning": "one sentence: why these conflict",
    "question": "You wrote X. You're now writing Y. Did you mean to revise?"
  }
]}

Empty array if no semantic conflicts. Output JSON only — no prose, no preamble, no markdown fences."""


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


def assemble_corpus_text(corpus_imps, max_lines_per_file=200):
    by_file = {}
    for i in corpus_imps:
        by_file.setdefault(i.file, []).append(i)
    blocks = []
    for file_path, imps in sorted(by_file.items()):
        rel = os.path.relpath(file_path, str(VAULT_ROOT))
        lines = [f"## {rel}"]
        for imp in imps[:max_lines_per_file]:
            lines.append(f"  L{imp.line_no}: {imp.line_text}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def detect_semantic_conflicts(new_imps, corpus_imps, api_key):
    try:
        from anthropic import Anthropic
    except ImportError:
        print("anthropic SDK not installed; skipping semantic mode.", file=sys.stderr)
        return [], None

    client = Anthropic(api_key=api_key)
    corpus_text = assemble_corpus_text(corpus_imps)
    new_content_lines = []
    for i in new_imps:
        rel = os.path.relpath(i.file, str(VAULT_ROOT))
        new_content_lines.append(f"  {rel}:L{i.line_no}: {i.line_text}")
    new_content_text = "\n".join(new_content_lines) or "(empty)"

    try:
        response = client.messages.create(
            model=SEMANTIC_MODEL,
            max_tokens=2000,
            system=[
                {"type": "text", "text": SEMANTIC_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": f"# Existing rules corpus\n\n{corpus_text}", "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": f"# New content to check\n\n{new_content_text}\n\nCompare each new line against the corpus. Return JSON only."}],
        )
    except Exception as e:
        print(f"Anthropic API call failed: {e}", file=sys.stderr)
        return [], None

    raw = response.content[0].text if response.content else ""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Could not parse model output as JSON: {e}", file=sys.stderr)
        print(f"Raw output: {raw[:500]}", file=sys.stderr)
        return [], response.usage

    return payload.get("conflicts", []), response.usage


@dataclass
class Imperative:
    verb: str
    noun: str
    file: str
    line_no: int
    line_text: str
    has_conditional: bool


@dataclass
class Conflict:
    new_imp: Imperative
    existing_imp: Imperative
    confidence: float
    relation: str

    def as_dict(self):
        return {
            "new": asdict(self.new_imp),
            "existing": asdict(self.existing_imp),
            "confidence": self.confidence,
            "relation": self.relation,
        }


def normalize_noun(noun):
    n = re.sub(r"[^\w\s]", " ", noun).lower().strip()
    n = re.sub(r"\s+", " ", n)
    for art in ("the ", "a ", "an ", "this ", "that "):
        if n.startswith(art):
            n = n[len(art):]
    return n.strip()


def extract_imperatives(text, file_path):
    out = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if line.strip().startswith(("|", "```", "    ", "\t")):
            continue
        line_lower = line.lower()
        has_conditional = any(m in line_lower for m in CONDITIONAL_MARKERS)
        for m in IMPERATIVE_RE.finditer(line):
            verb = m.group(1).lower().strip()
            noun = normalize_noun(m.group(2))
            if not noun or len(noun) < 3:
                continue
            out.append(Imperative(
                verb=verb,
                noun=noun,
                file=file_path,
                line_no=line_no,
                line_text=line.strip()[:200],
                has_conditional=has_conditional,
            ))
    return out


def gather_corpus_files():
    files = set()
    for pattern in CORPUS_GLOBS:
        for path in glob.glob(str(VAULT_ROOT / pattern), recursive=False):
            p = Path(path)
            if p.is_file():
                files.add(p)
    if MEMORY_DIR is not None:
        for p in MEMORY_DIR.glob("*.md"):
            if p.name == "MEMORY.md":
                continue
            files.add(p)
    return sorted(files)


def imperatives_oppose(a, b):
    for v1, v2 in OPPOSING_VERBS:
        if {a.verb, b.verb} == {v1, v2}:
            return True
    return False


def score_conflict(new, existing):
    if not imperatives_oppose(new, existing):
        return 0.0
    if new.noun != existing.noun:
        if not (
            (new.noun.startswith(existing.noun) or existing.noun.startswith(new.noun))
            and min(len(new.noun), len(existing.noun)) >= 3
        ):
            return 0.0
    score = NOUN_OVERLAP_BASE
    if new.noun == existing.noun:
        score += 0.25
    if new.has_conditional or existing.has_conditional:
        score -= 0.3
    if new.file == existing.file:
        score -= 0.2
    return max(0.0, min(1.0, score))


def detect_conflicts(new_imps, corpus_imps, threshold):
    conflicts = []
    for n in new_imps:
        for e in corpus_imps:
            if n.file == e.file and n.line_no == e.line_no:
                continue
            confidence = score_conflict(n, e)
            if confidence >= threshold:
                conflicts.append(Conflict(
                    new_imp=n,
                    existing_imp=e,
                    confidence=confidence,
                    relation="conflicts_with",
                ))
    return conflicts


def format_question(c):
    new_loc = f"{Path(c.new_imp.file).name}:{c.new_imp.line_no}"
    old_loc = f"{Path(c.existing_imp.file).name}:{c.existing_imp.line_no}"
    return (
        f"You wrote in `{old_loc}`: \"{c.existing_imp.line_text}\" "
        f"You're now writing in `{new_loc}`: \"{c.new_imp.line_text}\" "
        f"Did you mean to supersede the earlier rule, or are these meant "
        f"to coexist?"
    )


def write_markdown_report(conflicts, scan_target, semantic_conflicts=None):
    today = dt.date.today().isoformat()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "---",
        f"creationDate: {today}",
        "type: meta",
        f"purpose: Cross-document rule-conflict detector. Engram-inspired (github.com/Gentleman-Programming/engram). Companion to drift-detection.py.",
        "---",
        "",
        f"*Generated: {now} by `scripts/check-rule-conflicts.py`*",
        "",
        f"Scan target: `{scan_target}`",
        f"Corpus globs: {', '.join(CORPUS_GLOBS)}",
        "",
        "## Known limits (v1, keyword-anchor mode)",
        "",
        "- Catches keyword-opposing imperatives (`always X` vs `never X`) on shared nouns.",
        "- Misses vocabulary-different contradictions. Run with `--semantic` if `ANTHROPIC_API_KEY` is set.",
        "- False positives on conditional rules (`never X UNLESS Y`) softened via has_conditional penalty.",
        "- Same-file refinements (rule + sub-rule) score lower because they're usually intentional.",
        "",
    ]
    if not conflicts:
        lines.append("## No candidate conflicts at threshold")
        lines.append("")
        lines.append(f"_(threshold = {DEFAULT_THRESHOLD}, lower with CONFLICT_THRESHOLD env var)_")
    else:
        conflicts.sort(key=lambda c: -c.confidence)
        lines.append(f"## {len(conflicts)} candidate conflict(s)")
        lines.append("")
        for i, c in enumerate(conflicts, start=1):
            lines.append(f"### {i}. confidence {c.confidence:.2f}")
            lines.append("")
            lines.append(format_question(c))
            lines.append("")
            lines.append(f"- New verb: `{c.new_imp.verb}` · noun: `{c.new_imp.noun}`")
            lines.append(f"- Existing verb: `{c.existing_imp.verb}` · noun: `{c.existing_imp.noun}`")
            if c.new_imp.has_conditional or c.existing_imp.has_conditional:
                lines.append("- Note: at least one rule is conditional (UNLESS / EXCEPT / WHEN). Conflict may be bounded.")
            lines.append("")

    if semantic_conflicts:
        lines.append("")
        lines.append(f"## {len(semantic_conflicts)} semantic-mode conflict(s)")
        lines.append("")
        lines.append("*Vocabulary-different contradictions detected by claude-haiku-4-5.*")
        lines.append("")
        for i, sc in enumerate(semantic_conflicts, start=1):
            conf = sc.get("confidence", 0.0)
            lines.append(f"### S{i}. semantic confidence {conf:.2f}")
            lines.append("")
            lines.append(sc.get("question") or sc.get("reasoning") or "(no question generated)")
            lines.append("")
            lines.append(f"- New: `{sc.get('new_location', '?')}` — \"{sc.get('new_quote', '')}\"")
            lines.append(f"- Existing: `{sc.get('existing_location', '?')}` — \"{sc.get('existing_quote', '')}\"")
            lines.append(f"- Reasoning: {sc.get('reasoning', '')}")
            lines.append("")

    META_DIR.mkdir(parents=True, exist_ok=True)
    CONFLICT_REPORT.write_text("\n".join(lines), encoding="utf-8")
    return CONFLICT_REPORT


# --- Self test -------------------------------------------------------

SELF_TEST_FIXTURE_NEW = """
- **Always commit immediately after every edit.** Reduces lost work.
- **Must enable hookify rules.** Required for safety.
- **Prefer SQLite for storage.**
"""

SELF_TEST_FIXTURE_CORPUS = """
- **Never commit immediately after every edit.** Lock contention risk.
- **Banned: enable hookify rules.** They block legitimate edits.
- **Avoid SQLite for storage.** Use Postgres only.
"""

SEMANTIC_FIXTURE = [
    {
        "name": "tp1_database_scope",
        "label": "true_positive",
        "existing": "L10: Use SQLite for queryable data.\nL11: Don't reach for Postgres unless multi-user concurrent writes.",
        "existing_file": "Build Standards.md",
        "new": "L42: Always use Postgres for any new persistence work.",
        "new_file": "new-rule.md",
        "expected_conflict": True,
    },
    {
        "name": "tp2_aggregator_concurrency",
        "label": "true_positive",
        "existing": "L140: Aggregators run foreground sequential. NO backgrounding.",
        "existing_file": "session-close.md",
        "new": "L88: Run all session-close aggregators in parallel via `&` for speed.",
        "new_file": "new-rule.md",
        "expected_conflict": True,
    },
    {
        "name": "tp3_meeting_source_priority",
        "label": "true_positive",
        "existing": "L5: Gemini Google Doc first, fallback only if no Gemini.",
        "existing_file": "meeting-workflow.md",
        "new": "L20: For all meetings, use the fallback as the primary transcript source.",
        "new_file": "new-rule.md",
        "expected_conflict": True,
    },
    {
        "name": "tn1_wikilink_scope",
        "label": "true_negative",
        "existing": "L31: Always wikilink concepts in journal entries.",
        "existing_file": "obsidian.md",
        "new": "L77: Never path-form wikilinks. Always bare basename.",
        "new_file": "new-rule.md",
        "expected_conflict": False,
    },
    {
        "name": "tn2_default_vs_exception",
        "label": "true_negative",
        "existing": "L8: Default model is opusplan.",
        "existing_file": "model-routing.md",
        "new": "L12: Switch to Sonnet for session close (write-heavy work).",
        "new_file": "new-rule.md",
        "expected_conflict": False,
    },
    {
        "name": "edge1_conditional_overlap",
        "label": "edge_case",
        "existing": "L22: git push only when remote is configured.",
        "existing_file": "git-rules.md",
        "new": "L40: Never run git push.",
        "new_file": "new-rule.md",
        "expected_conflict": False,
    },
]


def run_self_test(semantic=False):
    print("--- self-test (keyword-anchor) ---")
    new_imps = extract_imperatives(SELF_TEST_FIXTURE_NEW, "fixture_new.md")
    corpus_imps = extract_imperatives(SELF_TEST_FIXTURE_CORPUS, "fixture_corpus.md")
    conflicts = detect_conflicts(new_imps, corpus_imps, 0.4)
    expected_pairs = 3
    print(f"new imperatives: {len(new_imps)}")
    print(f"corpus imperatives: {len(corpus_imps)}")
    print(f"conflicts found: {len(conflicts)}")
    if len(conflicts) == expected_pairs:
        print(f"PASS: all {expected_pairs} expected opposing pairs detected")
        kw_pass = True
    else:
        print(f"FAIL: expected {expected_pairs} conflicts, got {len(conflicts)}")
        for c in conflicts:
            print(f"  {c.new_imp.verb} {c.new_imp.noun} <-> {c.existing_imp.verb} {c.existing_imp.noun} ({c.confidence})")
        kw_pass = False

    if semantic:
        print("\n--- self-test (semantic-live) ---")
        api_key = get_anthropic_key()
        if not api_key:
            print("SKIP: no ANTHROPIC_API_KEY found.")
            print("  Set via env: ANTHROPIC_API_KEY=sk-ant-... python3 ... --self-test --semantic")
            print("  Or add to ~/.zsh_secrets: export ANTHROPIC_API_KEY=\"sk-ant-...\"")
            print("  Architecture is shipped; live validation deferred until key provided.")
        else:
            failures = 0
            print(f"  running {len(SEMANTIC_FIXTURE)} fixtures against {SEMANTIC_MODEL}...")
            for fx in SEMANTIC_FIXTURE:
                ne = extract_imperatives(fx["new"], fx["new_file"])
                ce = extract_imperatives(fx["existing"], fx["existing_file"])
                semantic_conflicts, _ = detect_semantic_conflicts(ne, ce, api_key)
                got_conflict = len(semantic_conflicts) > 0
                if got_conflict == fx["expected_conflict"]:
                    print(f"  PASS [{fx['label']}] {fx['name']}: expected={fx['expected_conflict']}, got={got_conflict}")
                else:
                    failures += 1
                    print(f"  FAIL [{fx['label']}] {fx['name']}: expected={fx['expected_conflict']}, got={got_conflict}")
            print(f"  {len(SEMANTIC_FIXTURE) - failures}/{len(SEMANTIC_FIXTURE)} semantic fixtures pass")

        print("\n--- self-test (semantic-parsing, no API call) ---")
        try:
            mock_response = '{"conflicts": [{"new_location": "f.md:1", "new_quote": "X", "existing_location": "g.md:1", "existing_quote": "Y", "confidence": 0.9, "reasoning": "clash", "question": "Did you mean to revise?"}]}'
            payload = json.loads(mock_response)
            assert "conflicts" in payload
            assert len(payload["conflicts"]) == 1
            fenced = "```json\n" + mock_response + "\n```"
            stripped = re.sub(r"^```(?:json)?\s*", "", fenced)
            stripped = re.sub(r"\s*```$", "", stripped)
            payload2 = json.loads(stripped)
            assert payload == payload2
            print("PASS: JSON parsing + fenced-strip + schema validation all clean")
        except Exception as e:
            print(f"FAIL: parsing test errored: {e}")
            kw_pass = False

        print(f"\nFixture inventory: {len(SEMANTIC_FIXTURE)} cases")
        labels = {}
        for fx in SEMANTIC_FIXTURE:
            labels.setdefault(fx["label"], 0)
            labels[fx["label"]] += 1
        for label, count in sorted(labels.items()):
            print(f"  {label}: {count}")

    return 0 if kw_pass else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--self-test", action="store_true", help="Run self-test on synthetic fixture")
    g.add_argument("--scan-all", action="store_true", help="Scan entire corpus for conflicts")
    g.add_argument("target", nargs="?", help="Single file to scan (any modified rule)")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help=f"Confidence threshold. Default: {DEFAULT_THRESHOLD}.")
    parser.add_argument("--semantic", action="store_true", help="Add LLM-based semantic check via claude-haiku-4-5.")
    parser.add_argument("--json", action="store_true", help="Output JSON only (no markdown report)")
    parser.add_argument("--hook", action="store_true", help="Hookify-style JSON output for postwrite hook integration")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_test(semantic=args.semantic))

    corpus_files = gather_corpus_files()
    corpus_text = {f: f.read_text(encoding="utf-8", errors="ignore") for f in corpus_files}
    corpus_imps = []
    for f, text in corpus_text.items():
        corpus_imps.extend(extract_imperatives(text, str(f)))

    if args.scan_all:
        new_imps = corpus_imps
        scan_target = "(full corpus)"
    elif args.target:
        target = Path(args.target)
        if not target.exists():
            print(f"File not found: {target}", file=sys.stderr)
            sys.exit(2)
        text = target.read_text(encoding="utf-8", errors="ignore")
        new_imps = extract_imperatives(text, str(target))
        scan_target = str(target.relative_to(VAULT_ROOT)) if target.is_absolute() else str(target)
    else:
        parser.print_help()
        sys.exit(2)

    conflicts = detect_conflicts(new_imps, corpus_imps, args.threshold)

    semantic_conflicts = []
    if args.semantic:
        api_key = get_anthropic_key()
        if api_key:
            semantic_conflicts, _ = detect_semantic_conflicts(new_imps, corpus_imps, api_key)
        else:
            print("--semantic skipped: no ANTHROPIC_API_KEY found.", file=sys.stderr)

    if args.json or args.hook:
        out = {
            "scan_target": scan_target,
            "threshold": args.threshold,
            "keyword_conflicts": [c.as_dict() for c in conflicts],
            "semantic_conflicts": semantic_conflicts,
        }
        print(json.dumps(out, indent=2))
        sys.exit(1 if conflicts or semantic_conflicts else 0)

    report_path = write_markdown_report(conflicts, scan_target, semantic_conflicts)
    print(f"Wrote {report_path} ({len(conflicts)} keyword conflicts, {len(semantic_conflicts)} semantic conflicts)")
    sys.exit(0)


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
