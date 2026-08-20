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


def _norm(text):
    """Backslashes -> forward slashes before ANY path matching.

    On Windows the journal path arrives as `C:\\vault\\Journals\\May 2026\\x.md`,
    which matches none of the '/'-written patterns here. Without this the gate
    never opens on Windows: no warning, no error, no signal — a silent fail-open
    on the platform rather than a visible break. Matching only; nothing here is
    executed as a path (and Python opens `C:/x/y` fine on Windows)."""
    return text.replace("\\", "/")


def _vault_root(text):
    r"""Absolute dir before the '<optional emoji >Journals/<Month YYYY>/' segment.
    Anchored on the absolute path (starts at a real '/', or a `C:/` drive root),
    so a leading shell prefix like `cat > '/vault/.../x.md'` is NOT captured into
    the root (that was the 2026-07-07 Bash-path fail-open bug). Quotes bound the
    segment on the Bash path.

    The drive-letter alternative is guarded by `(?<![A-Za-z])` so a URL like
    `http://host/Journals/May 2026/` cannot have its `p:` read as a drive.

    The optional group before 'Journals/' exists ONLY to skip a folder-name emoji
    prefix ('📓 '), so it must reject quotes, whitespace and shell metacharacters.
    With the older `[^/\n]*\s` it also matched ACROSS a command boundary: in
    `cd "/x/Brain" && cat > "📓 Journals/Aug 2026/e.md"` (relative save path) it
    swallowed `Brain" && cat > "📓 ` and resolved the root to `/x` — a directory
    holding no marker, so EVERY journal save was blocked regardless of whether the
    preflight had run. Codified 2026-08-18 after that false block. With the
    tightened class a relative save path finds no absolute root and fails OPEN per
    this module's contract, while absolute paths (the form SKILL.md mandates)
    resolve exactly as before — the guard keeps its teeth where they count."""
    m = re.search(
        r"((?:(?<![A-Za-z])[A-Za-z]:)?/[^\n\"']*?)/(?:[^/\n\"'>&|;\s]*\s)?"
        r"Journals/[A-Z][a-zA-Z]+\s+\d{4}/",
        text)
    return m.group(1) if m else None


def _marker_exists(vault, date_iso):
    """True when a preflight run covered date_iso.

    The preflight auto-spans since the last entry and names its marker after the
    day it RAN, not the day being journaled. Backfills (journaling Friday on
    Monday) therefore have no `<entry-date>.json` even though the run's
    since->until window covered the entry. Accept either: the exact-name marker,
    or any marker whose window contains date_iso."""
    for meta in ("⚙️ Meta", "Meta"):
        d = os.path.join(vault, meta, ".journal-context")
        if os.path.exists(os.path.join(d, f"{date_iso}.json")):
            return True
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    m = json.load(fh)
                since, until = m.get("since"), m.get("until")
                if since and until and since <= date_iso <= until:
                    return True
            except Exception:
                continue  # unreadable marker -> ignore, don't open a hole
    return False


try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool_name = payload.get("tool_name", "")
tool_input = payload.get("tool_input", {}) or {}

blob = ""          # text to scan for path + date + vault root
if tool_name == "Write":
    fp = _norm(tool_input.get("file_path", "") or "")
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
    cmd_norm = _norm(cmd)
    if JOURNAL_PATH_RE.search(cmd_norm) and any(
        m in cmd for m in ("cat >", "cat >>", "tee ", "tee -", " > ", " >> ", "mv ", "cp ", "rsync ")
    ):
        # Normalized, because _vault_root() below must see forward slashes too.
        blob = cmd_norm

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
