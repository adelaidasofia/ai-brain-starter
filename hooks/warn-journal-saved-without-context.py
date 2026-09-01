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
import shutil
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
    """Absolute dir before the '<optional emoji >Journals/<Month YYYY>/' segment,
    or None when the text carries no absolute journal path (see _resolve_vault).
    Anchored on the absolute path (starts at a real '/', or a `C:/` drive root),
    so a leading shell prefix like `cat > '/vault/.../x.md'` is NOT captured into
    the root (that was the 2026-07-07 Bash-path fail-open bug). Quotes bound the
    segment on the Bash path.

    The drive-letter alternative is guarded by `(?<![A-Za-z])` so a URL like
    `http://host/Journals/May 2026/` cannot have its `p:` read as a drive.

    The optional folder-prefix segment excludes quotes and shell metacharacters,
    not just '/'. With the looser `[^/\n]*` it spanned shell syntax: the
    cwd-relative save idiom
        cd "/Users/x/Brain" && cat > "<emoji> Journals/Aug 2026/e.md"
    let the segment swallow `Brain" && cat > "<emoji> `, collapsing the root to
    the vault's PARENT. That parent is a real directory, so the guard did not
    fail open — it blocked while naming a marker path no preflight can ever
    create, making the guard unsatisfiable and the bypass routine (2026-08-30)."""
    m = re.search(
        r"((?:(?<![A-Za-z])[A-Za-z]:)?/[^\n\"']*?)/(?:[^/\n\"'|&;<>]*\s)?"
        r"Journals/[A-Z][a-zA-Z]+\s+\d{4}/",
        text)
    return m.group(1) if m else None


# Leading `cd <dir>` of a command segment; quoted or bare. Used only to resolve a
# RELATIVE journal path, which is how daily-journal actually saves.
# The leading class accepts a quote and `(` as well as the shell separators, so a
# wrapped form (`bash -c 'cd "<vault>" && cat > ...'`, a subshell) still yields the
# cd target. Without them such a command fell through to the cwd fallback, which
# in a worktree session is NOT the vault — reproducing the same unsatisfiable
# block this hook was just fixed to stop emitting.
CD_TARGET_RE = re.compile(
    r"""(?:^|[;&|(\n"']|\bthen\b|\bdo\b)\s*cd\s+(?:-{1,2}\S+\s+)*"""
    r"""(?:"([^"\n]+)"|'([^'\n]+)'|([^\s;&|<>()]+))""")


def _cd_target(text, cwd=None):
    """Destination of the command's first `cd`, expanded and made absolute."""
    m = CD_TARGET_RE.search(text)
    if not m:
        return None
    d = os.path.expanduser(next(g for g in m.groups() if g is not None))
    if not os.path.isabs(d) and not re.match(r"^[A-Za-z]:/", d):
        if not cwd:
            return None
        d = os.path.normpath(os.path.join(cwd, d))
    return d.rstrip("/") or "/"


def _resolve_vault(text, cwd):
    """Vault root for this save. An absolute journal path wins; otherwise the
    path is relative to the shell's cwd, so fall back to the command's own `cd`
    target and then to the session cwd from the hook payload. Without these
    fallbacks the tightened regex above would return None for the relative
    idiom and the guard would silently stop protecting the primary save path —
    trading a wrong block for a quiet hole."""
    return _vault_root(text) or _cd_target(text, cwd) or cwd


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

vault = _resolve_vault(blob, payload.get("cwd") or None)
if not vault or not os.path.isdir(vault):
    sys.exit(0)  # can't locate vault -> fail open

dm = CREATION_DATE_RE.search(blob)
date_iso = dm.group(1) if dm else _target_today()

if _marker_exists(vault, date_iso):
    sys.exit(0)  # preflight ran for this date -> allow

# Name the meta dir this vault actually uses, and an interpreter that actually
# runs here: a remediation the operator cannot execute is why the bypass became
# routine. `uv run python3` where uv is installed (a bare `python3` is shimmed
# and errors out on such boxes), else the very interpreter running this hook.
_meta = next((m for m in ("⚙️ Meta", "Meta")
              if os.path.isdir(os.path.join(vault, m))), "⚙️ Meta")
_py = "uv run python3" if shutil.which("uv") else (sys.executable or "python3")
_preflight = os.path.join(vault, _meta, "scripts", "journal-preflight.py")
_missing = "" if os.path.exists(_preflight) else (
    "\n!! That script is MISSING from this vault. Install it with:\n"
    f'   VAULT_ROOT="{vault}" bash '
    '~/.claude/skills/ai-brain-starter/scripts/sync-vault-scripts.sh\n')

err = (
    "BLOCKED by warn-journal-saved-without-context hook.\n\n"
    f"No preflight marker for {date_iso} at\n"
    f"  {os.path.join(vault, _meta, '.journal-context', date_iso + '.json')}\n"
    "-> Step 0's context pull never ran, so this journal would ship with no\n"
    "calendar / messages / RescueTime / activity context. That is the exact\n"
    "2026-07-07 failure this guard exists to stop.\n\n"
    "Fix (do this, then re-issue the save):\n"
    f'  1. {_py} "{_preflight}"\n'
    f"{_missing}"
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
