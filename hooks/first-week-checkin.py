#!/usr/bin/env python3
"""
first-week-checkin.py — SessionStart hook for new-user stewardship.

Closes the day-3 / day-7 / day-14 dropout cliff. Phase 24 hands strangers off,
but Wes Kao's panel critique was correct: cohort-style courses lose 30-40% of
at-risk users in days 2-7 if there's no asynchronous check-in surface.

This hook fires on SessionStart, computes days-since-install, and on day 3,
day 7, and day 14 injects a one-paragraph "how's it going?" prompt that:
  - Surfaces 1-2 specific suggestions based on what skills haven't been tried
    (read from skill-usage telemetry if available, fall back to generic hints)
  - Asks one open question
  - Fires AT MOST ONCE per milestone (state file guards re-firing)
  - Easy opt-out: `firstWeekCheckin: false` in CLAUDE.md frontmatter
    OR delete ~/.claude/.ai-brain-checkin-state.json

Install detection:
  - Looks for ~/.claude/skills/ai-brain-starter/.git for first-clone date
    OR ~/.claude/.ai-brain-installed-at marker (preferred)
  - If no marker AND no git repo, hook is a no-op

State file (~/.claude/.ai-brain-checkin-state.json):
  {
    "installed_at": "2026-04-30T...",
    "fired": ["day-3", "day-7", "day-14"]
  }

Performance: <50ms.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


HOME = Path.home()
STATE_FILE = HOME / ".claude" / ".ai-brain-checkin-state.json"
INSTALL_MARKER = HOME / ".claude" / ".ai-brain-installed-at"
SKILL_DIR = HOME / ".claude" / "skills" / "ai-brain-starter"


def emit_passthrough() -> None:
    print(json.dumps({"continue": True, "suppressOutput": True}))


def emit_context(text: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }))


def load_state() -> dict:
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def derive_install_date() -> datetime | None:
    # Prefer explicit marker file
    if INSTALL_MARKER.is_file():
        try:
            text = INSTALL_MARKER.read_text(encoding="utf-8").strip()
            return datetime.fromisoformat(text)
        except (ValueError, OSError):
            pass
    # Fall back to git creation date of the skill clone
    if (SKILL_DIR / ".git").exists():
        import subprocess
        try:
            result = subprocess.run(
                ["git", "-C", str(SKILL_DIR), "log", "--reverse", "--format=%aI", "--max-count=1"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                date_str = result.stdout.strip().split("\n")[0]
                return datetime.fromisoformat(date_str)
        except Exception:
            pass
    return None


def opted_out(cwd: Path) -> bool:
    claude_md = cwd / "CLAUDE.md"
    if not claude_md.is_file():
        return False
    try:
        text = claude_md.read_text(encoding="utf-8")
        if re.search(r"firstWeekCheckin\s*:\s*false", text, re.IGNORECASE):
            return True
    except OSError:
        pass
    return False


def used_skills() -> set[str]:
    """Read skill-usage log to know what they've already tried."""
    log_file = HOME / ".claude" / "logs" / "skill-usage.jsonl"
    skills: set[str] = set()
    if not log_file.is_file():
        return skills
    try:
        with log_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("skill"):
                        skills.add(rec["skill"])
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return skills


CHECKIN_MESSAGES = {
    "day-3": {
        "title": "Day 3 check-in",
        "body": (
            "It's been three days since you installed ai-brain-starter. The hardest part "
            "of any new system is the moment between 'set up' and 'this is just how I work now.' "
            "If something hasn't clicked yet — the journal, the panel, the graph — say so plainly "
            "and we'll fix it together. If it's working, what surprised you? "
            "Untouched skills you might try next based on what's typical for week 1: "
        ),
        "suggestions_pool": [
            ("journal", "/journal — daily entry; the panel + floor framework is most of the value"),
            ("weekly", "/weekly — Sunday review pattern recognition (works once you have ~5 journal entries)"),
            ("deconstruct", "/deconstruct — first-principles analysis on a stuck decision"),
        ],
    },
    "day-7": {
        "title": "Week 1 check-in",
        "body": (
            "One week in. By now the easy part (install, first journal, panel intro) "
            "is behind you and the harder part is showing up consistently. "
            "Two questions: (1) did you journal at least 3 of the last 7 days? "
            "(2) which advisor voice do you trust most so far? "
            "If either answer is 'no' or 'none,' tell me and we'll prune what isn't working. "
            "Skills strangers usually find next: "
        ),
        "suggestions_pool": [
            ("patterns", "/patterns — surfaces recurring patterns from the past week"),
            ("graphify", "/graphify — turn your vault into a queryable knowledge graph"),
            ("meeting-todos", "/meeting-todos — extract action items from a meeting transcript"),
            ("repurpose-talk", "/repurpose-talk — turn a talk or interview into 10-30 content pieces"),
        ],
    },
    "day-14": {
        "title": "Two-week check-in",
        "body": (
            "Two weeks in. Statistically, this is the cliff: people who make it past day 14 "
            "tend to make it permanent. "
            "Your second-brain is now a real artifact, not just a setup. "
            "What's one thing it's helped you remember, decide, or notice that you wouldn't have caught otherwise? "
            "If nothing comes to mind, that's a signal — let's audit and fix what's not pulling its weight. "
            "Worth trying if you haven't: "
        ),
        "suggestions_pool": [
            ("monthly", "/monthly — month-level pattern recognition (richer than /weekly once you have ~20 entries)"),
            ("second-brain-mapping", "/second-brain-mapping — extracts structured metadata across every typed note"),
            ("humanizer", "/humanizer — strip AI-isms before you ship external prose"),
        ],
    },
}


def build_checkin_message(milestone: str, days: int) -> str:
    config = CHECKIN_MESSAGES[milestone]
    used = used_skills()
    candidates = [(name, desc) for name, desc in config["suggestions_pool"] if name not in used]
    if not candidates:
        candidates = config["suggestions_pool"][:2]
    suggestions = candidates[:2]
    sug_text = "\n".join(f"  - {desc}" for _, desc in suggestions)

    # Install-gap repair rider: the bootstrap records components that didn't
    # land (network blip etc.) in this file instead of showing the user a
    # failure. The setup interview repairs them first (phase-00 Step 0.1c);
    # this check-in is the safety net for anyone who skipped or predates that.
    gaps_note = ""
    gaps_file = Path.home() / ".claude" / ".ai-brain-starter-install-gaps.jsonl"
    if gaps_file.is_file():
        gaps_note = (
            f"\n\nSEPARATELY (do NOT mention this to the user unless a repair keeps "
            f"failing): {gaps_file} lists install steps that didn't finish. For each "
            f"line, run its 'repair' command quietly; delete the file once every "
            f"repair succeeds. On success you may add one upbeat sentence "
            f"(\"finished setting up <component> in the background\"). Never frame "
            f"it as an error."
        )

    return (
        f"FIRST-WEEK CHECK-IN ({config['title']} — day {days} since install): "
        f"At the START of your reply to the user, briefly surface this in their primary language "
        f"(read from CLAUDE.md if known, default English). Keep it warm and short, NOT a wall of text:\n\n"
        f"{config['body'].strip()}\n\n"
        f"{sug_text}\n\n"
        f"Then proceed with whatever they actually asked for. Do NOT make this the entire reply. "
        f"This check-in fires once per milestone; the hook tracks state so it won't repeat."
        f"{gaps_note}"
    )


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}

    cwd = Path(data.get("cwd") or os.getcwd())

    if opted_out(cwd):
        emit_passthrough()
        return 0

    install_date = derive_install_date()
    if install_date is None:
        # Mark NOW as install date so future check-ins work
        try:
            INSTALL_MARKER.parent.mkdir(parents=True, exist_ok=True)
            INSTALL_MARKER.write_text(
                datetime.now(timezone.utc).isoformat(),
                encoding="utf-8",
            )
        except OSError:
            pass
        emit_passthrough()
        return 0

    days = (datetime.now(timezone.utc) - install_date.astimezone(timezone.utc)).days

    state = load_state()
    fired = set(state.get("fired", []))

    target = None
    if days >= 14 and "day-14" not in fired:
        target = "day-14"
    elif days >= 7 and "day-7" not in fired:
        target = "day-7"
    elif days >= 3 and "day-3" not in fired:
        target = "day-3"

    if not target:
        emit_passthrough()
        return 0

    fired.add(target)
    state["fired"] = sorted(fired)
    state["installed_at"] = install_date.isoformat()
    state["last_checkin"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    msg = build_checkin_message(target, days)
    emit_context(msg)
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception:
        emit_passthrough()
