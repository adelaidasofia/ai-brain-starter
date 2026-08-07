#!/usr/bin/env python3
"""Block journal-file saves when Step 0's context preflight never ran.

Enforces Step 0 of daily-journal SKILL.md:
  "Run `journal-preflight.py` FIRST — the literal first tool call of every
   /journal, before the opener. Non-negotiable."

The preflight writes a marker at `<vault>/⚙️ Meta/.journal-context/<date>.json`
(also `Meta/...` for non-emoji vaults) recording that every configured source
was pulled. This guard is the backstop for when the model skips Step 0: if a
journal entry for <date> is about to be saved and that marker is ABSENT, the
save is blocked with instructions to run the preflight first.

Codified 2026-07-07 after a /journal session shipped the opener with ZERO
context (no calendar / messages / RescueTime / activity) — the user had to ask
"why didn't you pull everything?". Turns Step 0 from discipline into
infrastructure. Sibling of block-journal-save-without-panel-shown.py.

Triggered on (mirrors the panel-shown guard):
  - Write -> file_path matches /Journals/<Month YYYY>/<file>.md
  - Bash  -> command writes/appends to that path (cat >, tee, redirect, mv, cp)

Fails OPEN: any ambiguity (no vault root, no parseable date, IO error) -> allow.
It blocks ONLY when it positively determines the marker for the entry's date is
missing. Marker present is sufficient proof the preflight ran that day.

Bypass: JOURNAL_CONTEXT_BYPASS=1 (env or inline prefix) — addendum/out-of-band
edits to an already-contextualized entry.
"""

import json
import os
import re
import sys
import datetime
from pathlib import Path

# Inline-bypass support (mirrors the panel-shown guard): os.environ can't see an
# inline `VAR=1 cmd` prefix on the Bash path. Fail-open to no-op if _lib absent.
sys.path.insert(0, str(Path(__file__).resolve().parent / "_lib"))
try:
    from cmd_env import inline_bypass
except Exception:
    def inline_bypass(command, var):  # type: ignore
        return False

if os.environ.get("JOURNAL_CONTEXT_BYPASS") == "1":
    sys.exit(0)

JOURNAL_PATH_RE = re.compile(r"Journals/[A-Z][a-zA-Z]+\s+\d{4}/[^/\"']+\.md")
CREATION_DATE_RE = re.compile(r"creationDate:\s*(\d{4}-\d{2}-\d{2})")
DAY_BOUNDARY = (3, 45)  # 3:45am — entries before this belong to the prior day


def _target_today():
    now = datetime.datetime.now()
    b = now.replace(hour=DAY_BOUNDARY[0], minute=DAY_BOUNDARY[1], second=0, microsecond=0)
    d = now.date()
    if now < b:
        d -= datetime.timedelta(days=1)
    return d.isoformat()


def _vault_root(text):
    """Absolute dir before the '<optional emoji >Journals/<Month YYYY>/' segment.
    Anchored on the absolute path (starts at a real '/'), so a leading shell prefix
    like `cat > '/vault/.../x.md'` is NOT captured into the root (that was the
    2026-07-07 Bash-path fail-open bug). Quotes bound the segment on the Bash path."""
    m = re.search(r"(/[^\n\"']*?)/(?:[^/\n]*\s)?Journals/[A-Z][a-zA-Z]+\s+\d{4}/", text)
    return m.group(1) if m else None


def _marker_exists(vault, date_iso):
    for meta in ("⚙️ Meta", "Meta"):
        if os.path.exists(os.path.join(vault, meta, ".journal-context", f"{date_iso}.json")):
            return True
    return False


try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool_name = payload.get("tool_name", "")
tool_input = payload.get("tool_input", {}) or {}

blob = ""          # text to scan for path + date + vault root
if tool_name == "Write":
    fp = tool_input.get("file_path", "") or ""
    if JOURNAL_PATH_RE.search(fp):
        blob = fp + "\n" + (tool_input.get("content", "") or "")
elif tool_name == "Bash":
    cmd = tool_input.get("command", "") or ""
    # inline_bypass (shlex-based) can't parse a `cat << EOF` heredoc — and journals
    # are ALWAYS written as heredocs — so also accept a parse-independent env-prefix
    # form (`JOURNAL_CONTEXT_BYPASS=1 cat > ...`). Either satisfies the escape hatch.
    if inline_bypass(cmd, "JOURNAL_CONTEXT_BYPASS") or \
       re.search(r"(^|\s)JOURNAL_CONTEXT_BYPASS=1(\s|$)", cmd):
        sys.exit(0)
    if JOURNAL_PATH_RE.search(cmd) and any(
        m in cmd for m in ("cat >", "cat >>", "tee ", "tee -", " > ", " >> ", "mv ", "cp ", "rsync ")
    ):
        blob = cmd

if not blob:
    sys.exit(0)  # not a journal save

vault = _vault_root(blob)
if not vault or not os.path.isdir(vault):
    sys.exit(0)  # can't locate vault -> fail open

dm = CREATION_DATE_RE.search(blob)
date_iso = dm.group(1) if dm else _target_today()

if _marker_exists(vault, date_iso):
    sys.exit(0)  # preflight ran for this date -> allow

err = (
    "BLOCKED by warn-journal-saved-without-context hook.\n\n"
    f"No preflight marker for {date_iso} at\n"
    f"  {vault}/⚙️ Meta/.journal-context/{date_iso}.json\n"
    "-> Step 0's context pull never ran, so this journal would ship with no\n"
    "calendar / messages / RescueTime / activity context. That is the exact\n"
    "2026-07-07 failure this guard exists to stop.\n\n"
    "Fix (do this, then re-issue the save):\n"
    '  1. python3 "⚙️ Meta/scripts/journal-preflight.py"\n'
    "  2. Make the calendar + email + Slack + health MCP pulls it prints.\n"
    "  3. Fold the context into ## Today + a context_sources: frontmatter block.\n\n"
    "Bypass (addendum / pre-contextualized entry): JOURNAL_CONTEXT_BYPASS=1"
)
# JSON-decision output (exit 0) — NOT exit 2. This is the public-installer-compatible
# blocking form (mirrors block-secret-in-note.py): a hooks.json `... || echo '{allow}'`
# crash-fallback then fails OPEN correctly, because a real block exits 0 (fallback never
# fires) while only a crash exits non-zero (fallback allows). Works identically for the
# personal registration (Claude Code honors permissionDecision=deny).
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": err}}))
sys.exit(0)
