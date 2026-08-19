#!/usr/bin/env python3
"""
log-skill-usage.py — UserPromptSubmit hook. Logs which skills the user invokes.

Opt-in only. Disabled by default. Enable via `cascadeTelemetry: true` in
CLAUDE.md frontmatter or `SKILL_USAGE_TELEMETRY=1` env var.

What it does:
  - Detects `/skill-name` invocations in user prompts (regex `^/[a-z][a-z0-9-]+`)
  - Appends one JSONL line per invocation to ~/.claude/logs/skill-usage.jsonl
  - Records: timestamp, skill name, prompt-length-bucket, session_id (anonymized hash)
  - NEVER captures: full prompt content, file paths, vault data, names

Privacy:
  - Default: OFF
  - Opt-in mechanisms (any one enables):
      * `cascadeTelemetry: true` in CLAUDE.md frontmatter
      * `SKILL_USAGE_TELEMETRY=1` env var
      * `~/.claude/.telemetry-opt-in` file exists
  - Storage: local only (never sent over network)
  - Anonymization: session_id is SHA-256 truncated to 12 chars
  - Skill name is NOT hashed (it's a public identifier from your CLAUDE.md)
  - Prompt length bucketed into ranges, never exact
  - Opt-out at any time: delete ~/.claude/.telemetry-opt-in OR remove
    `cascadeTelemetry: true` from CLAUDE.md
  - Erase history: `rm ~/.claude/logs/skill-usage.jsonl`

Performance budget: <50ms (basically instantaneous regex + line append).

Why this exists: without usage data, every prioritization decision about which
skills to develop, deprecate, or surface is guesswork. The Matuschak panel
critique was correct: the maintainer ships features without knowing which ones
the median user actually uses after week 4. This hook closes that loop without
sacrificing privacy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def telemetry_enabled(cwd: Path) -> bool:
    if os.environ.get("SKILL_USAGE_TELEMETRY") == "1":
        return True
    if (Path.home() / ".claude" / ".telemetry-opt-in").is_file():
        return True
    # Check CLAUDE.md frontmatter
    claude_md = cwd / "CLAUDE.md"
    if claude_md.is_file():
        try:
            text = claude_md.read_text(encoding="utf-8", errors="replace")
            if re.search(r"cascadeTelemetry\s*:\s*true", text, re.IGNORECASE):
                return True
        except OSError:
            pass
    return False


def emit_passthrough() -> None:
    print(json.dumps({"continue": True, "suppressOutput": True}))


def length_bucket(n: int) -> str:
    if n < 50: return "xs"
    if n < 200: return "s"
    if n < 1000: return "m"
    if n < 5000: return "l"
    return "xl"


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            emit_passthrough()
            return 0
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        emit_passthrough()
        return 0

    prompt = (data.get("prompt") or "").strip()
    cwd = Path(data.get("cwd") or os.getcwd())
    session_id = data.get("session_id") or "unknown"

    if not prompt:
        emit_passthrough()
        return 0

    if not telemetry_enabled(cwd):
        emit_passthrough()
        return 0

    # Match /<skill-name> at start of prompt
    m = re.match(r"^/([a-z][a-z0-9-]{1,40})\b", prompt)
    if not m:
        emit_passthrough()
        return 0

    skill_name = m.group(1)

    # Anonymize session_id
    session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]

    # Schema matches existing scripts/skill-usage-report.py expectations:
    # `timestamp` (ISO), `skill`. `session_hash` and `prompt_bucket` are extras
    # ignored by the legacy reporter but consumed by newer analytics.
    record = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "skill": skill_name,
        "session_hash": session_hash,
        "prompt_bucket": length_bucket(len(prompt)),
    }

    # Write to BOTH locations (if vault found): vault Meta for vault-aware
    # reporters, ~/.claude/logs for tooling that runs without vault context.
    log_targets = []
    log_targets.append(Path.home() / ".claude" / "logs" / "skill-usage.jsonl")

    # Vault detection: walk up from cwd looking for a Meta folder
    p = cwd
    for _ in range(6):
        for child in (p.iterdir() if p.is_dir() else []):
            if child.is_dir() and child.name.endswith("Meta"):
                log_targets.append(child / "skill-usage-log.jsonl")
                break
        if any(t.parent.name.endswith("Meta") for t in log_targets[1:]):
            break
        if p.parent == p:
            break
        p = p.parent

    line = json.dumps(record, ensure_ascii=False) + "\n"
    for target in log_targets:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # never block on log write failure

    emit_passthrough()
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception:
        emit_passthrough()
