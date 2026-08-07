#!/usr/bin/env python3
"""hallucination-sample-audit.py — sampled ground-truth audit of Claude's recent outputs.

Closes the denominator gap in hallucination-watch.py. Hook fires + CFI rows
count fabrications NOTICED. This script samples random factual claims from
recent assistant turns, vault-greps the keywords, and asks a separate Claude
session to classify each claim as supported / contradicted / unverifiable.

Output: ⚙️ Meta/Hallucination Sample Audit.md

Per-run measurement: claims_supported / (claims_supported + claims_contradicted)
= "verified fraction" — the closest thing to a defensible hit rate. Claims
classed `unverifiable_from_vault` are excluded from the denominator (those are
not fabrications, just out-of-vault assertions).

Trended over time, this is what we can honestly compare to industry benchmarks
in context (TruthfulQA, HaluEval, FActScore, DELEGATE-52). It does NOT
replicate any benchmark; it measures THIS workflow against THIS vault.

Flags:
  --sessions N      sessions to sample from (default 5, most recent)
  --turns K         assistant turns to sample across all sessions (default 6)
  --claims-per-turn M  max claims to extract per turn (default 3)
  --seed S          random seed for reproducibility (default time-based)
  --model M         haiku / sonnet / opus (default haiku — cheap + sufficient)
  --json            machine-readable output (skip markdown write)
  --quiet           suppress stdout summary

Requires `_claude_router.py` (Tier-1 Max plan via `claude -p`, zero per-token cost).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from _claude_router import call_claude_text, RouterUnavailable  # noqa: E402

def _derive_sessions_dir(vault: Path) -> Path:
    """Claude Code projects dir = ~/.claude/projects/<sanitized vault path>/"""
    base = Path.home() / ".claude" / "projects"
    sanitized = str(vault).replace("/", "-")
    candidate = base / sanitized
    if candidate.exists():
        return candidate
    needle = vault.name.replace(" ", "")
    for project in base.glob("*"):
        if project.is_dir() and needle.lower() in project.name.lower().replace("-", ""):
            return project
    return candidate


def _resolve_vault() -> Path:
    """VAULT_ROOT env var, or cwd if it's an Obsidian vault, else fail loud."""
    env = os.environ.get("VAULT_ROOT")
    if env:
        return Path(env)
    cwd = Path.cwd()
    if (cwd / ".obsidian").exists():
        return cwd
    raise SystemExit(
        "VAULT_ROOT not set and cwd is not an Obsidian vault. "
        "Run from inside the vault, or `export VAULT_ROOT=/path/to/vault`."
    )


VAULT = _resolve_vault()
SESSIONS_DIR = _derive_sessions_dir(VAULT)
OUTPUT = VAULT / "⚙️ Meta" / "Hallucination Sample Audit.md"
HISTORY = VAULT / "⚙️ Meta" / "Hallucination Sample Audit History.jsonl"

EXTRACT_SYSTEM = """You extract verifiable factual claims from a Claude assistant turn.

A verifiable factual claim is a statement about a person, organization, date,
event, file path, number, or relationship that COULD be checked against a
source. Examples: "Person X co-founded Org Y", "the watch script lives at
<path>", "Project Z is the primary funding vehicle".

NOT claims: opinions ("this is good"), instructions ("you should X"), code
fragments, generic AI commentary, restatements of the user's prompt,
hypotheticals, panel voices in obvious panel format ("<voice name>:"), questions.

Return a JSON list (max N items) of {claim, search_keywords}. Each
search_keywords is 2-4 strong terms (proper nouns, file paths, distinctive
phrases) for a vault grep. No prose, ONLY JSON. If no claims, return [].
"""

CLASSIFY_SYSTEM = """You classify factual claims against vault excerpts.

For each claim + its grep excerpts, return:
  - `supported` — excerpts directly confirm the claim
  - `contradicted` — excerpts state something incompatible with the claim
  - `unverifiable_from_vault` — excerpts neither confirm nor contradict
      (the claim may still be true; the vault just doesn't speak to it)

Be strict. "Mentioned nearby" is NOT support. "Person X is a founder" + an
excerpt that lists Person X as founder of Org Y IS support. A claim about
Person Z's role with no excerpt mentioning Person Z's role is unverifiable.

Return a JSON list of {claim, verdict, reasoning}. Match the input order
exactly. No prose outside the JSON.
"""


def find_recent_sessions(n: int) -> list[Path]:
    """Most-recently-modified Claude transcript JSONLs (skip .bak)."""
    if not SESSIONS_DIR.exists():
        return []
    candidates = [
        p for p in SESSIONS_DIR.glob("*.jsonl")
        if ".bak" not in p.name
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[:n]


def extract_assistant_text(jsonl_path: Path) -> list[dict]:
    """Walk a transcript, return text-bearing assistant turns.

    Schema is defensive — Claude Code transcripts evolve. We look for any
    line with role=assistant or type=assistant and pull whatever 'text' or
    'content' field is present. Skip empty + tool-only turns.
    """
    turns = []
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = obj.get("role") or obj.get("type") or ""
                if role not in ("assistant", "model"):
                    msg = obj.get("message", {})
                    if isinstance(msg, dict):
                        role = msg.get("role", "")
                if role != "assistant":
                    continue
                text = ""
                content = obj.get("content") or obj.get("message", {}).get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            parts.append(block)
                    text = "\n".join(p for p in parts if p)
                text = text.strip()
                if len(text) < 100:  # skip tiny acks
                    continue
                turns.append({
                    "session": jsonl_path.stem,
                    "turn_index": i,
                    "text": text[:6000],  # cap per turn
                })
    except (OSError, IOError):
        return []
    return turns


def extract_claims(text: str, max_claims: int, model: str) -> list[dict]:
    """Ask Claude to pull 1-N verifiable factual claims from an assistant turn."""
    user = (
        f"Extract up to {max_claims} verifiable factual claims from this turn. "
        f"Return JSON list of {{claim, search_keywords}}.\n\n"
        f"---\n{text}\n---"
    )
    try:
        raw = call_claude_text(
            system=EXTRACT_SYSTEM, user=user, model=model, max_tokens=2048
        )
    except RouterUnavailable as e:
        print(f"[extract] router error: {e}", file=sys.stderr)
        return []
    return _parse_json_list(raw)


def _parse_json_list(raw: str) -> list:
    """Tolerant JSON list extraction — strip code fences, find first [ ... ]."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        result = json.loads(raw[start:end + 1])
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        return []


def vault_grep(keywords: list[str], max_excerpts: int = 5) -> list[str]:
    """Deterministic vault search. Returns up to N short excerpt blocks.

    Strategy: ripgrep with -F (fixed string), context lines. Searches the
    whole vault; authoritative folders (Meta, CRM, strategy docs) surface
    first by virtue of being more concentrated than journals.
    """
    excerpts = []
    if not keywords:
        return excerpts
    seen = set()
    for kw in keywords[:4]:  # cap keyword count
        if not kw or len(kw) < 3:
            continue
        try:
            result = subprocess.run(
                [
                    "rg", "-F", "--no-heading", "--max-count", "3",
                    "-C", "1", "-i", kw, str(VAULT),
                ],
                capture_output=True, text=True, timeout=20,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        out = result.stdout
        if not out:
            continue
        # Group every ~5 lines into one excerpt block
        block = []
        for line in out.splitlines()[:40]:
            if line.strip() == "--":
                if block:
                    chunk = "\n".join(block).strip()
                    key = chunk[:200]
                    if chunk and key not in seen:
                        excerpts.append(chunk)
                        seen.add(key)
                        if len(excerpts) >= max_excerpts:
                            return excerpts
                    block = []
                else:
                    continue
            else:
                block.append(line)
        if block:
            chunk = "\n".join(block).strip()
            key = chunk[:200]
            if chunk and key not in seen:
                excerpts.append(chunk)
                seen.add(key)
                if len(excerpts) >= max_excerpts:
                    return excerpts
    return excerpts


def classify_claims(
    claims: list[dict], excerpts_by_claim: list[list[str]], model: str
) -> list[dict]:
    """Send claims + their grep excerpts to Claude for verdict."""
    if not claims:
        return []
    payload_lines = []
    for i, claim in enumerate(claims):
        text = claim.get("claim", "")
        excerpts = excerpts_by_claim[i] if i < len(excerpts_by_claim) else []
        payload_lines.append(f"### Claim {i+1}\n{text}")
        if excerpts:
            payload_lines.append("\n**Vault excerpts:**\n")
            for j, ex in enumerate(excerpts[:5], 1):
                payload_lines.append(f"<<EXCERPT {j}>>\n{ex[:1200]}\n<<END>>")
        else:
            payload_lines.append("\n**Vault excerpts:** (none found)")
        payload_lines.append("")
    user = (
        "Classify each claim. Return JSON list matching input order. "
        "Verdict in {supported, contradicted, unverifiable_from_vault}.\n\n"
        + "\n".join(payload_lines)
    )
    try:
        raw = call_claude_text(
            system=CLASSIFY_SYSTEM, user=user, model=model, max_tokens=3072
        )
    except RouterUnavailable as e:
        print(f"[classify] router error: {e}", file=sys.stderr)
        return [
            {"claim": c.get("claim", ""), "verdict": "router_error", "reasoning": str(e)}
            for c in claims
        ]
    verdicts = _parse_json_list(raw)
    if len(verdicts) < len(claims):
        # Pad to match
        for i in range(len(verdicts), len(claims)):
            verdicts.append({
                "claim": claims[i].get("claim", ""),
                "verdict": "parse_error",
                "reasoning": "Claude response did not include this claim",
            })
    return verdicts


def render_report(run: dict) -> str:
    now = run["generated_at"]
    summary = run["summary"]
    samples = run["samples"]
    lines = []
    lines.append("---")
    lines.append(f"generated: {now}")
    lines.append(f"seed: {run['seed']}")
    lines.append(f"sessions_sampled: {run['sessions_sampled']}")
    lines.append(f"turns_sampled: {run['turns_sampled']}")
    lines.append(f"claims_extracted: {summary['claims_extracted']}")
    lines.append(f"verified_fraction: {summary['verified_fraction']:.3f}")
    lines.append("source_script: ⚙️ Meta/scripts/hallucination-sample-audit.py")
    lines.append("related: [[Hallucination Watch]], [[Substrate Maturity Methodology]]")
    lines.append("---")
    lines.append("")
    lines.append("# Hallucination Sample Audit")
    lines.append("")
    lines.append(
        f"Random sample of factual claims from Claude's recent assistant "
        f"turns, ground-truthed against the vault. Run "
        f"{now}."
    )
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(f"- **Claims extracted:** {summary['claims_extracted']}")
    lines.append(f"- **Supported by vault:** {summary['supported']}")
    lines.append(f"- **Contradicted by vault:** {summary['contradicted']}")
    lines.append(f"- **Unverifiable from vault:** {summary['unverifiable']}")
    lines.append(f"- **Parse / router errors:** {summary['errors']}")
    lines.append("")
    lines.append(
        f"**Verified fraction** = supported / (supported + contradicted) = "
        f"**{summary['verified_fraction']:.1%}** "
        f"({summary['supported']} / {summary['supported'] + summary['contradicted']})"
    )
    lines.append("")
    lines.append(
        "Read: `verified_fraction` is the denominator-anchored measurement. "
        "Unverifiable-from-vault claims are EXCLUDED (the vault may simply "
        "not speak to them — that's not a fabrication signal). "
        "Trend matters more than any single run; see history at "
        "`⚙️ Meta/Hallucination Sample Audit History.jsonl`."
    )
    lines.append("")
    lines.append("## How this maps to industry benchmarks")
    lines.append("")
    lines.append(
        "| Benchmark | Measures | Our analog |"
    )
    lines.append("|---|---|---|")
    lines.append(
        "| TruthfulQA | False statements on curated adversarial Q&A | Not measured here — adversarial framing absent |"
    )
    lines.append(
        "| FActScore | Atomic-fact verification on biographies vs Wikipedia | Closest analog — our `verified_fraction` is the structural twin against the vault |"
    )
    lines.append(
        "| HaluEval | Detection of hallucinated dialogue/QA/summary | Not measured — we measure production claims, not detection ability |"
    )
    lines.append(
        "| HHEM (Vectara) | Summary faithfulness vs source | Not directly comparable — we measure free-form assistant turns |"
    )
    lines.append(
        "| SimpleQA | Short-form factual accuracy | Closer to FActScore lineage; comparable in spirit |"
    )
    lines.append(
        "| DELEGATE-52 | Multi-turn corruption over 20 edits | Companion measure — this script + `hallucination-watch.py` together form the verification harness DELEGATE-52 identifies as the only mitigation |"
    )
    lines.append("")
    lines.append(
        "**Honest claim:** `verified_fraction` is structurally similar to "
        "FActScore (atomic-fact verification against a single ground-truth "
        "corpus) but the corpus is THIS vault, not Wikipedia. Numbers are "
        "not directly comparable across corpora."
    )
    lines.append("")
    lines.append("## Per-claim detail")
    lines.append("")
    if not samples:
        lines.append("_No samples this run._")
    for s_idx, s in enumerate(samples, 1):
        lines.append(f"### Sample {s_idx} — session `{s['session'][:8]}…` turn {s['turn_index']}")
        lines.append("")
        preview = s["text_preview"].replace("\n", " ").strip()
        lines.append(f"> {preview[:400]}{'…' if len(preview) > 400 else ''}")
        lines.append("")
        for i, claim in enumerate(s["claims"], 1):
            verdict = claim["verdict"]
            badge = {
                "supported": "✅ supported",
                "contradicted": "❌ contradicted",
                "unverifiable_from_vault": "⚪ unverifiable",
                "parse_error": "⚠️ parse error",
                "router_error": "⚠️ router error",
            }.get(verdict, f"⚠️ {verdict}")
            lines.append(f"**Claim {i}.** {claim['claim']}")
            lines.append(f"- Verdict: {badge}")
            reasoning = claim.get("reasoning", "").strip()
            if reasoning:
                lines.append(f"- Reasoning: {reasoning}")
            lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append(
        "1. Sample the N most-recently-modified session JSONLs from "
        "`~/.claude/projects/<sanitized-vault-path>/`.\n"
        "2. Extract text-bearing assistant turns from each session.\n"
        "3. Random-sample K turns across the pooled corpus (seeded for "
        "reproducibility).\n"
        "4. Per turn: Claude (Haiku via Max plan, zero per-token cost) "
        "extracts up to M verifiable factual claims as JSON.\n"
        "5. Per claim: deterministic ripgrep on the keywords, up to 5 "
        "vault excerpts.\n"
        "6. Per turn: Claude classifies all claims given their excerpts. "
        "Strict — \"mentioned nearby\" is not support.\n"
        "7. Aggregate: `verified_fraction = supported / (supported + "
        "contradicted)`. Unverifiable claims excluded from denominator.\n"
        "8. Append run to history JSONL for trend analysis."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=5)
    ap.add_argument("--turns", type=int, default=6)
    ap.add_argument("--claims-per-turn", type=int, default=3)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--model", type=str, default="haiku")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(seed)

    sessions = find_recent_sessions(args.sessions)
    if not sessions:
        print("no sessions found", file=sys.stderr)
        return 1

    pool: list[dict] = []
    for s in sessions:
        pool.extend(extract_assistant_text(s))
    if not pool:
        print("no assistant text found in sampled sessions", file=sys.stderr)
        return 1

    rng.shuffle(pool)
    sampled_turns = pool[: args.turns]

    samples: list[dict] = []
    sup = con = unv = err = total = 0

    for turn in sampled_turns:
        if not args.quiet:
            print(f"  ↳ {turn['session'][:8]}… turn {turn['turn_index']}", file=sys.stderr)
        claims = extract_claims(turn["text"], args.claims_per_turn, args.model)
        if not claims:
            continue
        excerpts_per_claim = [
            vault_grep(c.get("search_keywords", [])) for c in claims
        ]
        verdicts = classify_claims(claims, excerpts_per_claim, args.model)

        claim_records = []
        for claim, verdict in zip(claims, verdicts):
            total += 1
            v = verdict.get("verdict", "parse_error")
            if v == "supported":
                sup += 1
            elif v == "contradicted":
                con += 1
            elif v == "unverifiable_from_vault":
                unv += 1
            else:
                err += 1
            claim_records.append({
                "claim": claim.get("claim", ""),
                "search_keywords": claim.get("search_keywords", []),
                "verdict": v,
                "reasoning": verdict.get("reasoning", ""),
            })

        samples.append({
            "session": turn["session"],
            "turn_index": turn["turn_index"],
            "text_preview": turn["text"][:600],
            "claims": claim_records,
        })

    verified_denominator = sup + con
    verified_fraction = (sup / verified_denominator) if verified_denominator else 0.0

    run = {
        "generated_at": datetime.now().isoformat(),
        "seed": seed,
        "model": args.model,
        "sessions_sampled": len(sessions),
        "turns_sampled": len(samples),
        "summary": {
            "claims_extracted": total,
            "supported": sup,
            "contradicted": con,
            "unverifiable": unv,
            "errors": err,
            "verified_fraction": verified_fraction,
        },
        "samples": samples,
    }

    if args.json:
        print(json.dumps(run, indent=2))
        return 0

    report = render_report(run)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(report, encoding="utf-8")

    # Append to history for trend tracking
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "generated_at": run["generated_at"],
            "seed": seed,
            "model": args.model,
            "summary": run["summary"],
        }) + "\n")

    if not args.quiet:
        print(f"wrote {OUTPUT}")
        print(f"verified_fraction: {verified_fraction:.1%} "
              f"({sup}/{verified_denominator})")
        print(f"claims: {total} total — {sup} supported, {con} contradicted, "
              f"{unv} unverifiable, {err} errors")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
