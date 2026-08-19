#!/usr/bin/env python3
"""
migrate-to-user-level.py — SessionStart hook that detects project-level
installs of ai-brain-starter hooks and offers to migrate to user-level.

Why: project-level hooks (in <project>/.claude/settings.json or
.claude/settings.local.json) silently don't fire when cwd is inside
<project>/.claude/worktrees/<name>/. User-level hooks
(~/.claude/settings.json) fire universally.

Behavior:
  - Fires once per session (SessionStart).
  - Detects ai-brain-starter hooks at project level via fingerprint.
  - If detected AND user-level install is missing the same fingerprint,
    injects a one-paragraph context message offering migration.
  - Fires AT MOST ONCE PER VAULT (state file at ~/.claude/.abs-migration-state.json).
  - Easy opt-out: `migrationDeclined: true` in CLAUDE.md frontmatter.

The migration itself is performed by scripts/install-hooks-user-level.py
(the user runs it once they accept; this hook only surfaces the prompt).

Performance: <100ms.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


HOME = Path.home()
STATE_FILE = HOME / ".claude" / ".abs-migration-state.json"
USER_SETTINGS = HOME / ".claude" / "settings.json"

ABS_FINGERPRINTS = [
    "ai-brain-starter/hooks/detect-closing-signal.py",
    "ai-brain-starter/hooks/lint-vault-frontmatter.py",
    "ai-brain-starter/hooks/log-skill-usage.py",
    "ai-brain-starter/hooks/first-week-checkin.py",
]


def emit_passthrough() -> None:
    print(json.dumps({"continue": True, "suppressOutput": True}))


def emit_context(text: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }))


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None


def find_project_settings(cwd: Path) -> Path | None:
    """Walk up from cwd to find a project-level .claude/settings*.json."""
    p = cwd
    for _ in range(8):
        for name in ("settings.json", "settings.local.json"):
            candidate = p / ".claude" / name
            if candidate.is_file():
                return candidate
        if p.parent == p:
            break
        p = p.parent
    return None


def has_abs_hooks(settings: dict | None) -> bool:
    if not settings or "hooks" not in settings:
        return False
    text = json.dumps(settings.get("hooks") or {}, ensure_ascii=False)
    return any(fp in text for fp in ABS_FINGERPRINTS)


def opted_out(cwd: Path) -> bool:
    claude_md = cwd / "CLAUDE.md"
    if not claude_md.is_file():
        return False
    try:
        text = claude_md.read_text(encoding="utf-8", errors="replace")
        return bool(re.search(r"migrationDeclined\s*:\s*true", text, re.IGNORECASE))
    except OSError:
        return False


def load_state() -> dict:
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


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

    project_settings_path = find_project_settings(cwd)
    project_settings = load_json(project_settings_path) if project_settings_path else None
    user_settings = load_json(USER_SETTINGS)

    project_has = has_abs_hooks(project_settings)
    user_has = has_abs_hooks(user_settings)

    state = load_state()
    cwd_key = str(cwd.resolve())
    notified_for = set(state.get("notified_for", []))

    # Fire if project-level hooks are present but user-level isn't, AND we
    # haven't already notified for this vault path.
    if project_has and not user_has and cwd_key not in notified_for:
        notified_for.add(cwd_key)
        state["notified_for"] = sorted(notified_for)
        state["last_check"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

        msg = (
            "AI BRAIN STARTER MIGRATION DETECTED: ai-brain-starter hooks are "
            f"installed at PROJECT level ({project_settings_path}) but NOT at "
            f"USER level ({USER_SETTINGS}). Project-level hooks silently "
            "fail to fire when Claude Code runs inside a git worktree (your "
            f".claude/worktrees/<name>/ paths). The fix is to migrate to "
            "user-level, which fires universally.\n\n"
            "At the START of your reply to the user, surface this in their "
            "primary language (read from CLAUDE.md if known, default English). "
            "Keep it ONE short paragraph plus the command. Example:\n\n"
            "  \"Heads up: your ai-brain-starter hooks are installed at project "
            "level, which means they don't fire when you work inside a git "
            "worktree. To migrate to user-level (universal): run "
            "`python3 ~/.claude/skills/ai-brain-starter/scripts/install-hooks-user-level.py`. "
            "This is additive — your existing hooks stay, and there's a backup. "
            "Want me to run it now?\"\n\n"
            "If they say yes: run the command. If they say no or 'later': set "
            "`migrationDeclined: true` in their CLAUDE.md frontmatter so this "
            "doesn't fire again. The hook tracks state per-vault — won't "
            "re-prompt for this vault unless they explicitly clear the marker."
        )
        emit_context(msg)
        return 0

    emit_passthrough()
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception:
        emit_passthrough()
