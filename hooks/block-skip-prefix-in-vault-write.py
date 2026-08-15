#!/usr/bin/env python3
"""Block life-record vault writes that still contain a literal `__SKIP` line.

The convention: a line whose first non-whitespace token is `__SKIP` is
something the user said to the assistant but does NOT want persisted. The
assistant must strip those lines before writing, and say so. This hook is the
hard guard: if such a line survives to Write/Edit/Bash on a life-record path,
block and force a strip-and-retry.

Why a literal token: it is cheap to type, distinctive enough not to collide
with real markdown, and it borrows a convention people already know from the
shell (a leading space keeps a command out of history). Defense in depth --
the block fires even when the assistant forgets step 1.

WHY THIS IS A BLOCK AND NOT A WARNING
A warning that is ignored still persists the line, and a persisted line cannot
be un-persisted: it is in the file, in git history, and in whatever index reads
the vault. Privacy primitives fail closed.

SCOPE -- life-record surfaces only
Journals, coaching records, processing notes, and the panel aggregator: the
places a person narrates their own life. Deliberately NOT rule files, runbooks,
code, or imported third-party transcripts, where the user never had a `__SKIP`
opportunity in the first place and the token would only false-positive.

Defaults match the folder layout this repo's own skills write to
(skills/daily-journal, skills/coaching). A vault with a different taxonomy
extends coverage WITHOUT editing this file:

    export SKIP_PREFIX_EXTRA_PATHS='Diario/:Therapy Notes/'

Colon-separated Python regexes, matched against the path. An unparseable
pattern is reported on stderr and skipped rather than silently dropped, so a
typo cannot quietly shrink the guard's coverage.

Bypass: SKIP_PREFIX_BYPASS=1, honored from the session env AND as an inline
`SKIP_PREFIX_BYPASS=1 <cmd>` prefix on the Bash path. Legitimate uses: editing
this hook, its docs, or a skill file that quotes the token. Never for real
journal or coaching content.

Bug class: SKIP-PREFIX-LEAKED-TO-VAULT-PERSISTENCE (family:
ARTIFACT-WITHOUT-USER-CONSENT -- content the user did not authorize for
persistence reaches a persisted file).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

# --- inline-bypass handling (self-contained ON PURPOSE) ----------------------
# A PreToolUse(Bash) gate runs in the hook process, whose env is the SESSION
# env. An inline `SKIP_PREFIX_BYPASS=1 <cmd>` prefix lives only in the command
# STRING, so a bypass this hook advertises must be read from the command too
# (bug class HOOK-READS-SESSION-ENV-NOT-COMMAND-ENV).
#
# These helpers are LOCAL rather than imported from a shared hooks/_lib module,
# matching check-cd-outside-worktree.py: a shared module is co-owned by multiple
# installers (last-writer-wins) and can be clobbered by a version missing one of
# these functions, silently making the guard inert.
SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;\n|]")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPER_PREFIXES = {"env", "command", "exec", "builtin", "nohup", "sudo", "time"}


def _inline_bypass(command: str, var: str) -> bool:
    """True iff a `<var>=1` assignment leads any shell segment of `command`.
    A token inside quotes is not a leading assignment (`echo 'X=1'` -> no)."""
    for seg in SEGMENT_SPLIT_RE.split(command):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        i = 0
        while i < len(tokens) and (_ENV_ASSIGN_RE.match(tokens[i])
                                   or tokens[i] in _WRAPPER_PREFIXES):
            if _ENV_ASSIGN_RE.match(tokens[i]):
                k, _, v = tokens[i].partition("=")
                if k == var and v == "1":
                    return True
            i += 1
    return False


# --- scope -------------------------------------------------------------------
# Emoji-free regexes so they match whether the path carries the vault's emoji
# folder prefixes or not, and whether it is main-vault or worktree-prefixed.
# These mirror the paths this repo's own skills write to.
_DEFAULT_PATH_RES = (
    re.compile(r"Journals/[A-Z][a-zA-Z]+\s+\d{4}/"),  # Journals/May 2026/
    re.compile(r"Coaching Sessions/"),                # Home/ + Strategy/ variants
    re.compile(r"Co-founder Syncs/"),
    re.compile(r"Decision Reviews/"),
    re.compile(r"Personal Coaching/"),
    re.compile(r"Processing Notes - "),               # verbatim capture, any subdir
    re.compile(r"Panel Feedback Log\.md"),
)


def _extra_path_res() -> tuple:
    """User-supplied regexes from SKIP_PREFIX_EXTRA_PATHS (colon-separated).

    A bad pattern is REPORTED and skipped, never silently dropped: silently
    shrinking coverage is the failure mode this guard exists to prevent.
    """
    raw = os.environ.get("SKIP_PREFIX_EXTRA_PATHS", "").strip()
    if not raw:
        return ()
    out = []
    for pat in raw.split(":"):
        pat = pat.strip()
        if not pat:
            continue
        try:
            out.append(re.compile(pat))
        except re.error as exc:
            sys.stderr.write(
                f"[skip-prefix-convention] WARNING: ignoring unparseable "
                f"SKIP_PREFIX_EXTRA_PATHS entry {pat!r}: {exc}\n"
            )
    return tuple(out)


# Surfaces that legitimately contain the literal token: this hook, its docs,
# and skill/rule files that quote it. Substring match.
SELF_REF_EXEMPT_MARKERS = (
    "block-skip-prefix-in-vault-write.py",
    "skip-prefix-convention.md",
    "SKILL.md",
    "CLAUDE.md",
    "AGENTS.md",
)

# `__SKIP ` / `__SKIP\t` / a bare `__SKIP` at end of line all count. The
# lookahead is what stops `__SKIPPED` from matching -- requiring a trailing
# SPACE would miss a tab-separated line and leak it.
SKIP_LINE_RE = re.compile(r"(?m)^[ \t]*__SKIP(?=[ \t]|$)")

# Shell constructs that can write a file. Kept broad: a false positive here
# only costs a bypass flag, a false negative persists private content.
BASH_REDIRECT_MARKERS = ("cat >", "cat >>", "tee ", "tee -", " > ", " >> ")


def _norm(text: str) -> str:
    """Backslashes -> forward slashes before ANY path matching.

    On Windows a life-record path arrives as `C:\\vault\\Journals\\May 2026\\x.md`.
    The folder patterns are written with `/`, so without this every Windows write
    misses every pattern and the guard fails OPEN -- silently, with no error, on
    a privacy control. Matching only; nothing here is executed as a path.
    """
    return text.replace("\\", "/")


def _matches_life_record(text: str) -> bool:
    text = _norm(text)
    return any(rx.search(text) for rx in (_DEFAULT_PATH_RES + _extra_path_res()))


def _is_self_ref_exempt(text: str) -> bool:
    text = _norm(text)
    return any(marker in text for marker in SELF_REF_EXEMPT_MARKERS)


def _in_scope(path: str) -> bool:
    if not path or _is_self_ref_exempt(path):
        return False
    return _matches_life_record(path)


def _block(reason: str) -> None:
    sys.stderr.write(
        "[skip-prefix-convention] BLOCKED: " + reason + "\n"
        "A `__SKIP` line is content the user asked you NOT to persist.\n"
        "Strip every line whose first token is `__SKIP`, then emit one line per "
        "stripped item so the drop is never silent:\n"
        "    Dropped __SKIP line N (token preview: <first 6 words>)\n"
        "Do not paraphrase the dropped content back into the entry, and do not "
        "log it anywhere else. Then retry the write.\n"
        "Bypass (self-referential docs only): SKIP_PREFIX_BYPASS=1\n"
    )
    sys.exit(2)


def main() -> None:
    if os.environ.get("SKIP_PREFIX_BYPASS") == "1":
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_name = payload.get("tool_name", "") or ""
    tool_input = payload.get("tool_input", {}) or {}

    if tool_name == "Write":
        fp = tool_input.get("file_path", "") or ""
        if not _in_scope(fp):
            sys.exit(0)
        if SKIP_LINE_RE.search(tool_input.get("content", "") or ""):
            _block(f"Write to `{fp}` still contains a literal `__SKIP` line.")

    elif tool_name in ("Edit", "MultiEdit"):
        fp = tool_input.get("file_path", "") or ""
        if not _in_scope(fp):
            sys.exit(0)
        # Only NEW content is the persistence surface. A `__SKIP` in old_string
        # is the assistant REMOVING the marker -- that path must never block.
        new_blobs = []
        if "new_string" in tool_input:
            new_blobs.append(tool_input.get("new_string", "") or "")
        for edit in tool_input.get("edits", []) or []:
            new_blobs.append((edit or {}).get("new_string", "") or "")
        if any(SKIP_LINE_RE.search(blob) for blob in new_blobs):
            _block(f"Edit on `{fp}` would write a literal `__SKIP` line.")

    elif tool_name == "Bash":
        cmd = tool_input.get("command", "") or ""
        if _inline_bypass(cmd, "SKIP_PREFIX_BYPASS"):
            sys.exit(0)
        if not _matches_life_record(cmd) or _is_self_ref_exempt(cmd):
            sys.exit(0)
        if not any(marker in cmd for marker in BASH_REDIRECT_MARKERS):
            sys.exit(0)
        # Heredoc body / echo string. The token is distinctive enough that a
        # substring check is the right precision here.
        if SKIP_LINE_RE.search(cmd) or "__SKIP " in cmd:
            _block(
                "Bash command writes to a life-record path AND carries a "
                "literal `__SKIP` token in the command body."
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
