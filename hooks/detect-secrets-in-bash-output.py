#!/usr/bin/env python3
"""PostToolUse hook on Bash: detect secrets in tool output, alert immediately.

The Claude Code hook framework can't retroactively redact what the model
already saw on this turn, so this layer is detection + loud-alert + audit
log. Two outputs:

1. STDERR alert to the agent in the SAME turn ("secrets just landed in
   your transcript, rotation playbook at <path>"). The agent sees this as
   additional context and can react.

2. Append-only audit log at ~/.claude/hooks/secret-detection-log.jsonl.
   Each entry: timestamp, session_id, tool_input (command hash), patterns
   that matched, match counts. This is the corpus for spotting recurring
   gaps in PreToolUse coverage. Entries may also carry `fp_suppressed` for
   hits reclassified as content-addressed IDs (docker run -d / drizzle
   migration hashes) rather than secrets.

Pairs with:
- PreToolUse hookify rule `block-secret-dump-command-class` (vault
  .claude/hookify.*.local.md) — blocks the COMMAND.
- SessionEnd hook `scrub-session-jsonl.py` — redacts the JSONL on close.
- SessionStart hook `scan-prior-sessions-for-secrets.py` — warns on next
  session start if any prior JSONL still has unredacted secrets.

Codified 2026-05-13. Critical Failure Inventory: "the user's primary org production infra
surface" row 2.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

# Path adjustment so `_lib` resolves whether hook is invoked from
# ~/.claude/hooks or another cwd. Sibling _lib/ dir on PYTHONPATH.
HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))

from _lib.secret_patterns import filter_tool_output_false_positives, scan  # noqa: E402

LOG_PATH = HOOK_DIR / "secret-detection-log.jsonl"
PLAYBOOK_HINT = "⚙️ Meta/Handoffs/20260513-eng6-secrets-rotation-playbook.md"

# Per the user's stated preference (2026-05-23): grep'ing her own .env.local /
# admin.env / .zsh_secrets to verify what's stored is intentional, not a leak.
# Local-only vault, Anthropic doesn't share transcripts, threat model is local
# machine compromise (not per-key). Suppress the loud rotation alert in that
# narrow case but still write the audit log entry so we keep observability.
# Alert still fires for genuinely surprising leaks (broad-scope grep, CI log
# paste, third-party output, src/ code containing hardcoded secrets, etc).
READ_CMD_PREFIXES = (
    "rg ", "grep ", "cat ", "head ", "tail ", "less ", "more ", "bat ",
    "/usr/bin/grep ", "/usr/bin/cat ", "/bin/cat ",
)
DANGEROUS_SINKS = (" > ", " >> ", "| curl", "| nc ", "| tee ", "| mail ", "| pbcopy", "| ssh ", "| scp ")
SELF_SECRET_PATH_HINTS = ("/dev/", "/.claude/", ".zsh_secrets", ".zshenv")
SELF_SECRET_TARGET_HINTS = (".env", "admin.env", ".zsh_secrets", ".zshenv")


def _is_self_secret_inspection(payload: dict) -> bool:
    """True when the bash command is a read-only inspection of the user's own
    known-secrets stores (~/dev/*/.env*, ~/.claude/*/admin.env, ~/.zsh_secrets).
    Used to suppress the rotation alert (still logs to audit JSONL)."""
    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not cmd:
        return False
    stripped = cmd.lstrip()
    if not any(stripped.startswith(c) for c in READ_CMD_PREFIXES):
        return False
    if any(d in cmd for d in DANGEROUS_SINKS):
        return False
    if not any(g in cmd for g in SELF_SECRET_PATH_HINTS):
        return False
    if not any(t in cmd for t in SELF_SECRET_TARGET_HINTS):
        return False
    return True


def _read_payload() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _tool_output_text(payload: dict) -> str:
    """Pull the Bash tool's stdout+stderr from the hook payload.

    The hook framework passes different shapes across versions; this
    function tries the common keys and falls back to scanning the whole
    payload as a string.
    """
    tool_response = payload.get("tool_response") or {}
    parts: list[str] = []
    for key in ("stdout", "stderr", "output", "content"):
        v = tool_response.get(key)
        if isinstance(v, str):
            parts.append(v)
    if not parts:
        return json.dumps(payload)
    return "\n".join(parts)


def _hash_command(payload: dict) -> str:
    cmd = (payload.get("tool_input") or {}).get("command", "")
    return hashlib.sha256(cmd.encode("utf-8", "replace")).hexdigest()[:16]


def main() -> int:
    payload = _read_payload()
    output = _tool_output_text(payload)
    if not output:
        # Nothing to scan; benign exit.
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    hits = scan(output)
    if not hits:
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    # Alert-layer-only reclassification: docker-run-detached ID echoes and
    # drizzle migration-hash psql rows are content-addressed, not secrets.
    # scan()/redact() above are untouched — this only trims what THIS hook
    # alerts on. Suppressed hits are still audit-logged below with a reason.
    command = (payload.get("tool_input") or {}).get("command", "")
    hits, fp_suppressed = filter_tool_output_false_positives(hits, output, command)

    # Secrets just landed in the transcript. Log + (maybe) alert.
    self_inspection = _is_self_secret_inspection(payload)
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "session_id": payload.get("session_id", "unknown"),
        "command_sha": _hash_command(payload),
        "hits": [{"pattern": n, "count": c} for n, c in hits],
        "alert_suppressed": bool(self_inspection or not hits),
    }
    if fp_suppressed:
        entry["fp_suppressed"] = [
            {"pattern": n, "count": c, "reason": r} for n, c, r in fp_suppressed
        ]
    try:
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        # Log write failure shouldn't break the hook; alert path runs anyway.
        pass

    # Self-inspection of own .env / admin.env / .zsh_secrets: skip the alert.
    # Detection still in audit log; loud rotation message is just noise here.
    if self_inspection:
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    # Every match was a documented content-addressed ID shape (docker ID
    # echo / drizzle migration hash) — audit entry retained above, no loud
    # alert; nothing real is left in `hits` to report.
    if not hits:
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    summary = ", ".join(f"{n}×{c}" for n, c in hits)
    alert = (
        f"⚠️ SECRETS DETECTED in Bash tool output ({summary}). "
        f"This session's transcript now contains plaintext secrets. "
        f"Rotate per {PLAYBOOK_HINT}, then run "
        f"`bash \"⚙️ Meta/scripts/scrub-session-jsonl-20260513-secrets.sh\"` "
        f"(or the equivalent for any new pattern). "
        f"Audit log: {LOG_PATH}."
    )
    print(
        json.dumps(
            {
                "continue": True,
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": alert,
                },
            }
        )
    )
    # stderr also fires for visibility in terminal-attached runs.
    print(alert, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
