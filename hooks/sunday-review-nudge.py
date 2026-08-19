#!/usr/bin/env python3
"""SessionStart hook: nudge /sunday-review on Sundays.

Reads `<vault>/⚙️ Meta/skill-usage-log.jsonl` and emits a nudge to
stderr (which Claude Code SessionStart hooks treat as injected context)
only when:

  1. Today is Sunday (weekday() == 6)
  2. No `sunday-review` or `insights` skill invocation is present in the
     skill-usage log within the last 6 days

Vault root resolution order:
  1. SUNDAY_NUDGE_VAULT env var (explicit override)
  2. CLAUDE_PROJECT_DIR env var (set by Claude Code in CWD-aware contexts)

If neither is set, the hook silently no-ops (avoids accidental fires
in a project that does not have a vault).

Bypass: SUNDAY_NUDGE_BYPASS=1
Skill log location override: SUNDAY_NUDGE_LOG_PATH
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


WEEKDAY_SUNDAY = 6
LOOKBACK_DAYS = 6
TRIGGER_SKILLS = {"sunday-review", "insights"}


def resolve_log_path() -> Path | None:
    explicit = os.environ.get("SUNDAY_NUDGE_LOG_PATH")
    if explicit:
        return Path(explicit)

    vault_env = os.environ.get("SUNDAY_NUDGE_VAULT") or os.environ.get(
        "CLAUDE_PROJECT_DIR"
    )
    if vault_env:
        return Path(vault_env) / "⚙️ Meta" / "skill-usage-log.jsonl"

    return None


def fired_recently(log: Path, cutoff: datetime) -> bool:
    try:
        with log.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("skill") not in TRIGGER_SKILLS:
                    continue
                try:
                    rec_time = datetime.fromisoformat(rec.get("timestamp", ""))
                except ValueError:
                    continue
                if rec_time >= cutoff:
                    return True
    except OSError:
        pass
    return False


def main() -> int:
    if os.environ.get("SUNDAY_NUDGE_BYPASS") == "1":
        return 0

    today = datetime.now()
    if today.weekday() != WEEKDAY_SUNDAY:
        return 0

    log = resolve_log_path()
    if log is None or not log.exists():
        return 0

    cutoff = today - timedelta(days=LOOKBACK_DAYS)
    if fired_recently(log, cutoff):
        return 0

    print(
        "[sunday-nudge] It is Sunday and /sunday-review (or /weekly) has not "
        f"fired in the last {LOOKBACK_DAYS} days. Run /sunday-review to "
        "orchestrate /weekly + /patterns + vault-hygiene + claude-md-drift + "
        "decision-retrospective before drilling into other work.",
        file=sys.stderr,
    )
    return 0


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
    sys.exit(main())
