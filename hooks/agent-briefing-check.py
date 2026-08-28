#!/usr/bin/env python3
"""
PreToolUse hook for the Agent (Task) tool: warn when the prompt looks
unbounded, vague, or bundles multiple tasks.

Why: 2026-04-26 weekly performance digest flagged avg agent turns at 19.4
(target <8). Worst offender was a 136-char prompt: "look at all recently
imported notes and make sure they are optimized with wikilinks and
everything in your claude.md instructions please" — 485 turns. Five top
offenders all shared one or more failure patterns:
  - unbounded scope ("all", "every", "everything", "make sure")
  - no exit criterion (no count, no time cap, no done-when)
  - no file paths (vague target)
  - bundled multi-task work in one agent

This hook does NOT block. It nudges the parent session to refine the
prompt before spawning. Agents are still useful; vague briefings are not.

Bypass: AGENT_BRIEFING_BYPASS=1 in env.
"""
from __future__ import annotations

import json
import os
import re
import sys


UNBOUNDED_RE = re.compile(
    r"\b(all|every|everything|each|whole|entire|make sure|ensure|optimize)\b",
    re.IGNORECASE,
)
COUNT_RE = re.compile(
    r"\b(\d+\s*(files?|items?|notes?|rows?|chunks?|max|maximum)|under \d+|"
    r"top \d+|first \d+|stop after|exit when|done when|until)\b",
    re.IGNORECASE,
)
PATH_RE = re.compile(r"(?:/[^\s]+|\.(md|py|sh|json|ts|tsx|js)\b)")
BUNDLE_HINT_RE = re.compile(
    r"(?im)^\s*(##\s|\d+\.\s|task\s*\d|step\s*\d).*(\n.*){0,40}^\s*(##\s|\d+\.\s|task\s*\d|step\s*\d)"
)
# Anti-patterns lifted 2026-05-24 from crewAI collaboration docs (Candidate 2,
# CLAUDE.md (see canonical rule)). The framework names 4 delegation anti-patterns;
# 2 are net-new to this hook's coverage.
CIRCULAR_DELEGATION_RE = re.compile(
    r"\b(spawn|launch|invoke|create|dispatch)\s+(an?\s+)?(sub)?agent\b.*"
    r"(spawn|launch|invoke|create|dispatch).*(sub)?agent",
    re.IGNORECASE | re.DOTALL,
)
ROLE_OVERLAP_HINT_RE = re.compile(
    r"\b(code-?(explorer|architect|reviewer)|feature-?dev|general-?purpose|plan|explore)\b.*"
    r"\b(or|and|either|also use|could use)\b.*"
    r"\b(code-?(explorer|architect|reviewer)|feature-?dev|general-?purpose|plan|explore)\b",
    re.IGNORECASE,
)
# Gate 6 (2026-05-25, from Hainrixz/cyber-neo SKILL.md L153-156): subagents do
# NOT have access to ${CLAUDE_SKILL_DIR} / ${SKILL_DIR} / parent-relative paths.
# Embedding references via interpolated env vars or skill-dir-relative paths
# silently fails (subagent reads empty / errors). Pattern catches the symptom
# at briefing time so the parent embeds reference content or absolute paths
# instead.
SKILL_DIR_RELATIVE_RE = re.compile(
    r"\$\{?CLAUDE_SKILL_DIR\}?|"
    r"\$\{?SKILL_DIR\}?|"
    r"\$\{?CLAUDE_PROJECT_DIR\}?/skills/|"
    r"(?<![\w/])\./references/|"
    r"(?<![\w/])\./scripts/",
    re.IGNORECASE,
)
# Router-persona anti-pattern (2026-05-26, from addyosmani/agent-skills
# references/orchestration-patterns.md Anti-pattern A, audited at
# CLAUDE.md (see canonical rule) Candidate 5). A subagent whose job is
# to decide WHICH OTHER subagent to call. Pure routing layer with no domain
# value: adds two paraphrasing hops (information loss + ~2x token cost) and
# replicates work that umbrella dispatchers + intent mapping already do. The
# umbrella IS the router; subagents should be specialists, never dispatchers.
# Sibling of CIRCULAR_DELEGATION_RE (which catches the cascading multi-hop
# case); this catches the single-hop dispatcher case.
ROUTER_PERSONA_RE = re.compile(
    r"\b("
    r"decide\s+which\s+|"
    r"pick\s+|"
    r"dispatch\s+to\s+|"
    r"route\s+to\s+|"
    r"delegate\s+to\s+|"
    r"choose\s+"
    r")"
    r"(the\s+|an?\s+)?"
    r"(right\s+|appropriate\s+|correct\s+|best\s+)?"
    r"(sub[-\s]?)?"
    r"(agent|persona|specialist)\b"
    r"|"
    r"\bmeta[-\s]?orchestrator\b"
    r"|"
    r"\brouter\s+(sub[-\s]?)?(agent|persona)\b"
    r"|"
    r"\bwhich\s+(persona|sub[-\s]?agent|specialist)\s+(should|to)\s+(call|spawn|invoke|use|run)\b",
    re.IGNORECASE,
)


def warn(message: str) -> None:
    sys.stderr.write(f"AGENT BRIEFING WARN: {message}\n")
    sys.stderr.flush()


def main() -> None:
    if os.environ.get("AGENT_BRIEFING_BYPASS") == "1":
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if payload.get("tool_name") != "Agent":
        sys.exit(0)

    tin = payload.get("tool_input", {}) or {}
    prompt = tin.get("prompt", "") or ""
    subagent = tin.get("subagent_type") or "general-purpose"

    # Explore agents are inherently multi-step — relax the check for them.
    if subagent in {"Explore", "explore", "Plan"}:
        sys.exit(0)

    issues: list[str] = []

    if len(prompt) < 150:
        issues.append(
            f"prompt is {len(prompt)} chars — too short to brief a subagent. "
            "Brief like a colleague who walked in cold: file paths, expected "
            "output, exit criterion."
        )

    if UNBOUNDED_RE.search(prompt) and not COUNT_RE.search(prompt):
        issues.append(
            "unbounded scope (uses 'all'/'every'/'everything'/'make sure') "
            "with no count or exit criterion. Add a bound: 'top 20', "
            "'first 10 files', 'stop after 30 minutes', or list specific paths."
        )

    if not PATH_RE.search(prompt):
        issues.append(
            "no file paths or extensions in prompt. Subagent has no anchor "
            "for what to read. Include absolute paths or specific filenames."
        )

    if "report" not in prompt.lower() and "return" not in prompt.lower() \
       and "output" not in prompt.lower():
        issues.append(
            "no expected output format. State 'report under 200 words', "
            "'return JSON with fields X/Y/Z', or 'list of paths only'."
        )

    if BUNDLE_HINT_RE.search(prompt) and prompt.lower().count("\n## ") >= 2:
        issues.append(
            "prompt looks like multiple bundled tasks (2+ ## sections). "
            "Split into separate Agent calls or run sequentially in main."
        )

    if CIRCULAR_DELEGATION_RE.search(prompt):
        issues.append(
            "circular delegation hint: prompt tells the subagent to spawn "
            "another subagent. Nested Agent calls usually indicate the parent "
            "should plan the work directly (Plan agent) instead of cascading. "
            "Per crewAI anti-pattern: 'Setting all agents to allow_delegation=True "
            "without hierarchy creates infinite back-and-forth.'"
        )

    if ROLE_OVERLAP_HINT_RE.search(prompt):
        issues.append(
            "role overlap hint: prompt names 2+ overlapping subagent types "
            "(e.g. code-explorer OR code-architect OR general-purpose) without "
            "picking one. Pick the most specific match; the umbrella's job is "
            "to route, the caller's job is to choose."
        )

    if SKILL_DIR_RELATIVE_RE.search(prompt):
        issues.append(
            "skill-dir-relative path in subagent prompt: subagents do NOT have "
            "access to ${CLAUDE_SKILL_DIR} / ${SKILL_DIR} / ${CLAUDE_PROJECT_DIR}/skills/ "
            "/ ./references/ / ./scripts/. The path resolves to empty or fails "
            "silently. Instead: (1) resolve to absolute path in the parent and "
            "interpolate the resolved value, OR (2) embed the file CONTENTS "
            "directly in the prompt. Per Hainrixz/cyber-neo SKILL.md L153-156 "
            "(audited 2026-05-25 at CLAUDE.md (see canonical rule))."
        )

    if ROUTER_PERSONA_RE.search(prompt):
        issues.append(
            "router-persona / meta-orchestrator anti-pattern: prompt asks the "
            "subagent to decide WHICH OTHER agent / persona / specialist to "
            "call. Subagents are specialists, not dispatchers. The parent "
            "picks the specialist in the parent session; never delegate the "
            "picking. Pure routing layer = info-loss + ~2x token cost + "
            "duplicates umbrella dispatch. Fix: caller picks one specific "
            "subagent_type and brief THAT one with the work. Per "
            "addyosmani/agent-skills orchestration-patterns Anti-pattern A "
            "(audited 2026-05-26 at CLAUDE.md (see canonical rule))."
        )

    if not issues:
        sys.exit(0)

    warn(
        f"Agent briefing for {subagent} has likely-verbose patterns. A vague "
        "or unbounded brief drives the subagent's turn count up. Issues:"
    )
    for i, msg in enumerate(issues, 1):
        warn(f"  [{i}] {msg}")
    warn(
        "Refine before spawning, or set AGENT_BRIEFING_BYPASS=1 if this is a "
        "deliberately exploratory call."
    )
    # Exit 0 = warn only, don't block.
    sys.exit(0)


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    # A hook that print()s non-ASCII raises UnicodeEncodeError on a cp1252
    # console: the gate then fails silently OPEN, or denies the tool call with
    # no legible cause. Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
