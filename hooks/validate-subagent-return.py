#!/usr/bin/env python3
"""
PostToolUse hook for the Agent (Task) tool: warn when the subagent's
return payload looks incomplete, empty, or error-stubbed.

Why (codified 2026-05-24, AgriciDaniel/claude-ads cherry-pick — Gate 5):
parent sessions sometimes treat a subagent return as input to aggregation
without first checking the return was complete. When a subagent fails
mid-task and returns an empty / partial / error-stub payload, the parent
silently aggregates it and produces a finding that omits whatever the
failed subagent was supposed to surface. The bug class is
PARTIAL-AGGREGATION-SILENT-CORRUPTION.

claude-ads SKILL.md verbatim: "Validate subagent returns before aggregating."
The repo encodes this as a step in their parallel-agents orchestration; the
hook surfaces it for caller-side discipline in this stack.

This hook does NOT block. It nudges the parent to verify before next call
treats the payload as authoritative.

Bypass: SUBAGENT_RETURN_VALIDATE_BYPASS=1 in env.

PostToolUse matcher: "Agent|Task" -- the harness names this tool either way.
"""
from __future__ import annotations

import json
import os
import re
import sys


EMPTY_RE = re.compile(r"^\s*(\{\s*\}|\[\s*\]|null|none|n/a|\-+)?\s*$", re.IGNORECASE)
ERROR_STUB_RE = re.compile(
    r"\b(error|exception|failed|unable to|could not|timeout|timed out|"
    r"i (don't|do not) have access|cannot complete|no results found|"
    r"insufficient context)\b",
    re.IGNORECASE,
)
PARTIAL_HINT_RE = re.compile(
    r"(partial results? (below|follow(ing|s)?|so far|attached|are listed)|"
    r"results? (are|were|came back|came in) partial|"
    r"(this|the) (output|response|return|result|answer) (is|was|seems|appears) (partial|truncated|incomplete)|"
    r"truncated (due to|because|by) (length|limit|exceed|context|rate|token|size)|"
    r"incomplete (due to|because) (length|limit|exceed|context|rate|token|time)|"
    r"hit (the |a )?(token|context|rate|max(imum)?) limit|"
    r"stopping (early|here) (due to|because)|"
    r"only (got|found|read|processed|scanned) \d+ of \d+|"
    r"could not finish, returning|"
    r"returning what I have so far)",
    re.IGNORECASE,
)


def warn(message: str) -> None:
    sys.stderr.write(f"SUBAGENT RETURN VALIDATE: {message}\n")
    sys.stderr.flush()


def main() -> None:
    if os.environ.get("SUBAGENT_RETURN_VALIDATE_BYPASS") == "1":
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if payload.get("tool_name") not in ("Agent", "Task"):
        sys.exit(0)

    tin = payload.get("tool_input", {}) or {}
    subagent = tin.get("subagent_type") or "general-purpose"
    description = (tin.get("description") or "")[:80]

    # The PostToolUse payload shape: tool_response.content is the agent's return.
    response = payload.get("tool_response", {}) or {}
    content = response.get("content") or response.get("text") or ""
    if isinstance(content, list):
        content = "\n".join(
            str(c.get("text", "")) if isinstance(c, dict) else str(c)
            for c in content
        )
    content = str(content)

    issues: list[str] = []

    if EMPTY_RE.match(content) or len(content.strip()) < 50:
        issues.append(
            f"return payload is {len(content.strip())} chars (empty / "
            "near-empty). Subagent likely failed silently. Re-spawn with a "
            "corrected briefing OR fail loudly — do NOT aggregate this as a finding."
        )

    if ERROR_STUB_RE.search(content[:1000]):
        issues.append(
            "return payload contains error-stub language in the first 1000 "
            "chars ('error' / 'failed' / 'unable to' / 'cannot complete'). "
            "Read the payload before treating it as authoritative input. "
            "Aggregating an error-stub as a finding silently corrupts the "
            "parent analysis."
        )

    if PARTIAL_HINT_RE.search(content[:2000]):
        issues.append(
            "return payload signals partial completion ('partial' / "
            "'truncated' / 'incomplete' / 'hit limit' / 'only got N of M'). "
            "Address the truncation BEFORE aggregating, or note explicitly "
            "in the finding that this subagent returned partial data."
        )

    if not issues:
        sys.exit(0)

    warn(
        f"Agent ({subagent}) returned a payload that may be incomplete. "
        f"Task was: {description!r}. Gate 5 of agent-briefing (CLAUDE.md): "
        "validate subagent returns BEFORE aggregating. Issues:"
    )
    for i, msg in enumerate(issues, 1):
        warn(f"  [{i}] {msg}")
    warn(
        "Per CLAUDE.md (see canonical rule) Candidate 1 + CLAUDE.md "
        "Agent-briefing Gate 5. Bypass: SUBAGENT_RETURN_VALIDATE_BYPASS=1."
    )
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
