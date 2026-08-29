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


# A heredoc opener: `<< EOF`, `<<'EOF'`, `<<"EOF"`, `<<-EOF`. `<<<` herestrings
# do not match (the third '<' is not a delimiter character).
_HEREDOC_RE = re.compile(r"""<<-?[ \t]*(["']?)([A-Za-z_][A-Za-z0-9_]*)\1""")


def _strip_heredocs(cmd):
    """The command LINES, with every heredoc BODY removed.

    The gate must open on what a command WRITES, not on whatever text it happens
    to carry. A journal entry's body, a test fixture, or a doc that quotes a
    journal path all travel inside a heredoc, and matching those opened the gate
    on writes that are not journal saves at all — measured 2026-08-28, when a
    write to `tests/integration/*.sh` was blocked because the test's own PROSE
    contained `Journals/August 2026/`. A guard that fires on unrelated writes
    teaches the operator to reach for the bypass, and a bypass reached for by
    habit is how the real block gets waved through.

    Only the GATE narrows. `creationDate:` is still read from the full text,
    because that lives in the body by construction."""
    if "<<" not in cmd:
        return cmd
    lines = cmd.split("\n")
    kept, i = [], 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for m in _HEREDOC_RE.finditer(line):
            delim = m.group(2)
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1  # drop the closing delimiter line too
    return "\n".join(kept)


def _vault_root(text):
    r"""Absolute dir before the '<optional emoji >Journals/<Month YYYY>/' segment,
    or None when the text carries no ABSOLUTE journal path.

    Anchored on the absolute path (starts at a real '/', or a `C:/` drive root),
    so a leading shell prefix like `cat > '/vault/.../x.md'` is NOT captured into
    the root (that was the 2026-07-07 Bash-path fail-open bug).

    The drive-letter alternative is guarded by `(?<![A-Za-z])` so a URL like
    `http://host/Journals/May 2026/` cannot have its `p:` read as a drive.

    The optional emoji-prefix segment is bounded on quotes and shell operators,
    not only on '/' and newline. Journals are written as

        cd "<vault>" && cat > "<emoji> Journals/August 2026/e.md" << 'EOF'

    where the journal path is RELATIVE. With the old `(?:[^/\n]*\s)?` the segment
    swallowed `vault" && cat > "<emoji> ` and matched `Journals/` anyway, so group 1
    stopped at the vault's PARENT and the marker was looked up one directory too
    high (measured 2026-08-28 on a real save). Returning None here is the honest
    answer for a relative path; `_resolve_root` recovers the real root from the
    `cd` target or the session cwd."""
    m = re.search(
        r"((?:(?<![A-Za-z])[A-Za-z]:)?/[^\n\"']*?)/(?:[^/\n\"'&|;<>]*\s)?"
        r"Journals/[A-Z][a-zA-Z]+\s+\d{4}/",
        text)
    return m.group(1) if m else None


# `cd <path>`, honouring quotes and skipping option flags (`cd -P /x`). Bounded on
# shell operators so it cannot run past the end of the cd word.
_CD_RE = re.compile(
    r"""(?:^|[;&|]|\s)cd\s+(?:-[A-Za-z]+\s+)*("[^"\n]+"|'[^'\n]+'|[^\s;&|<>]+)""")

# The journal folder as it is spelled in this command -- `Journals` or, in the
# default vault layout, `<emoji> Journals`. The class cannot cross '/', so an
# absolute path yields the last segment only.
_JOURNAL_DIR_RE = re.compile(r"([^/\n\"']*Journals)/[A-Z][a-zA-Z]+\s+\d{4}/")


def _is_vault(root, journal_dirname=None):
    """True only if `root` actually holds a journals folder.

    This is the check that would have caught the 2026-08-28 bug on its own: the
    stitched root `/Users/me` holds no Journals dir, so it can never be mistaken
    for a vault no matter what the regex hands over. A candidate that fails here
    is discarded rather than trusted, so a wrong guess degrades to fail-open
    instead of to a confident answer about the wrong directory.

    Metadata-only (isdir/listdir), never a file read, so it stays safe on a
    cloud-mirrored vault per the cloud-safe-filesystem-walk rule."""
    try:
        if not os.path.isdir(root):
            return False
        if journal_dirname and os.path.isdir(os.path.join(root, journal_dirname)):
            return True
        if os.path.isdir(os.path.join(root, "Journals")):
            return True
        for entry in os.listdir(root):
            if entry.endswith(" Journals") and os.path.isdir(os.path.join(root, entry)):
                return True
    except OSError:
        return False
    return False


def _resolve_root(text, cwd):
    """The vault root for this write, or None when it cannot be determined.

    Ordered by how directly each candidate names the write target:
      1. an ABSOLUTE journal path in the text  (strongest -- names the vault itself)
      2. the `cd` target in the same command   (the relative-path write form)
      3. the session cwd from the hook payload (relative path, no cd)

    Every candidate must pass `_is_vault`, so a wrong one is dropped instead of
    being used. None means fail-open, which is the correct posture for ambiguity:
    this guard blocks only on a POSITIVE determination that the marker is absent."""
    jm = _JOURNAL_DIR_RE.search(text)
    journal_dirname = jm.group(1) if jm else None

    candidates = []
    root = _vault_root(text)
    if root:
        candidates.append(root)
    for cm in _CD_RE.finditer(text):
        candidates.append(cm.group(1).strip("\"'"))
    if cwd:
        candidates.append(cwd)

    for cand in candidates:
        if cand and _is_vault(cand, journal_dirname):
            return cand
    return None


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
    # Gate on the command LINES only (heredoc bodies stripped), so a write whose
    # PAYLOAD merely mentions a journal path is not mistaken for a journal save.
    gate_text = _strip_heredocs(cmd_norm)
    if JOURNAL_PATH_RE.search(gate_text) and any(
        m in gate_text for m in ("cat >", "cat >>", "tee ", "tee -", " > ", " >> ", "mv ", "cp ", "rsync ")
    ):
        # Normalized, because _vault_root() below must see forward slashes too.
        # The FULL text (body included) is the blob: the gate narrows, the
        # creationDate scan must not.
        blob = cmd_norm

if not blob:
    sys.exit(0)  # not a journal save

vault = _resolve_root(_strip_heredocs(blob) if tool_name == "Bash" else blob,
                      payload.get("cwd") or None)
if not vault:
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
