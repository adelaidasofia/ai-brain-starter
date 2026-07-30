#!/usr/bin/env python3
"""
PreToolUse hook: block Write/Edit of handoff files that lack a non-empty
`consumes_when:` frontmatter field.

Why: handoff files are cross-session context bridges. Without an explicit
completion signal recorded at create time, the next session has no way to
know whether the bridged work is done — they pile up indefinitely. Vault
audit on 2026-04-26 found three long-shipped handoffs still in
`⚙️ Meta/`, each silently outdated.

A file is a handoff if EITHER:
  - filename matches *Handoff*.md / *handoff*.md / *-handoff-*.md /
    next-session-*.md, OR
  - frontmatter `type:` is handoff / session-handoff / session-starter /
    prompt

This hook fires on Write, Edit and MultiEdit. It scopes to a VAULT so it never
fires on unrelated projects elsewhere on disk.

Vault detection (issue #375): the old code read
`os.environ.get("VAULT_ROOT", str(Path.home() / "vault"))`. Nothing sets
VAULT_ROOT, so it silently defaulted to ~/vault and in_vault() returned False
for every real file on any vault not literally named "vault" - the guard was
inert everywhere, with no signal that it was doing nothing.

Now: explicit $VAULT_ROOT wins; otherwise walk up from the target file for the
nearest ancestor containing a Meta-suffixed folder ("Meta" / "⚙️ Meta"), the
same signature scripts/_meta_resolver.py keys on. A CLAUDE.md marker is NOT
usable here: every code repo has one, so it would fire this guard on source
trees. Detection is deliberately self-contained (no import off scripts/) so a
hook that gates every Write cannot be broken by an import error.

FAIL OPEN: if no vault can be identified, the write is allowed.

Bypass: HANDOFF_FRONTMATTER_BYPASS=1 in the environment.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def find_vault_root(start: Path) -> Path | None:
    """Nearest ancestor of `start` that contains a Meta-suffixed folder, or None."""
    try:
        here = start.resolve()
    except (OSError, RuntimeError):
        return None
    for cand in (here, *here.parents):
        try:
            if any(c.is_dir() and c.name.endswith("Meta") for c in cand.iterdir()):
                return cand
        except (OSError, PermissionError):
            continue
    return None


def vault_root_for(path: Path) -> Path | None:
    """Vault root governing `path`: auto-detected from the file, else $VAULT_ROOT.

    Detection comes FIRST on purpose. $VAULT_ROOT names ONE vault, but a machine
    routinely has several (a personal vault plus one per project). If the env var
    won, every write into any OTHER vault would resolve to a root it does not sit
    under, in_vault() would be False, and the guard would silently skip exactly
    the files it exists to check. Per-file detection is correct for all of them;
    $VAULT_ROOT stays as the fallback for a vault with no Meta-suffixed folder.
    """
    found = find_vault_root(path.parent)
    if found is not None:
        return found
    env = (os.environ.get("VAULT_ROOT") or "").strip()
    if env:
        try:
            return Path(env).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
    return None

HANDOFF_FILENAME_RE = re.compile(
    r"(?:^|/)(?:[^/]*[Hh]andoff[^/]*|next-session-[^/]+)\.md$"
)

HANDOFF_TYPES = {"handoff", "session-handoff", "session-starter", "prompt"}

# A handoff is markdown by definition (the lifecycle rule puts them in
# `⚙️ Meta/Handoffs/*.md`, and HANDOFF_FILENAME_RE already requires .md). Gating
# on the extension lets the common case (writing code) exit before any I/O.
MARKDOWN_EXTS = {".md", ".markdown", ".mdx"}

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
TYPE_LINE_RE = re.compile(r"^type:\s*(\S+)\s*$", re.MULTILINE)
CONSUMES_LINE_RE = re.compile(r"^consumes_when:\s*(.*?)\s*$", re.MULTILINE)


def in_vault(path: Path) -> bool:
    """True only when `path` sits under an identifiable vault root. No vault
    found -> False, so the caller allows the write (fail open)."""
    root = vault_root_for(path)
    if root is None:
        return False
    try:
        path.resolve().relative_to(root)
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def filename_matches(path: Path) -> bool:
    return bool(HANDOFF_FILENAME_RE.search(str(path)))


def get_frontmatter_type(content: str) -> str | None:
    """Return the frontmatter `type:` value, or None if no type or no frontmatter."""
    fm_match = FRONTMATTER_RE.match(content)
    if not fm_match:
        return None
    type_match = TYPE_LINE_RE.search(fm_match.group(1))
    if not type_match:
        return None
    return type_match.group(1).strip().strip("\"'")


def is_handoff(file_path: Path, content: str) -> bool:
    """Frontmatter type is authoritative. Filename match is a fallback ONLY
    when the file has no frontmatter type at all (raw markdown). A file with
    `type: decision` whose filename happens to contain 'handoff' (e.g. a
    decision log about the handoff system) is NOT a handoff."""
    fm_type = get_frontmatter_type(content)
    if fm_type is not None:
        return fm_type in HANDOFF_TYPES
    return filename_matches(file_path)


def has_valid_consumes_when(content: str) -> bool:
    fm_match = FRONTMATTER_RE.match(content)
    if not fm_match:
        return False
    consumes_match = CONSUMES_LINE_RE.search(fm_match.group(1))
    if not consumes_match:
        return False
    value = consumes_match.group(1).strip().strip("\"'")
    return bool(value)


def _allow() -> int:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
    return 0


def _deny(reason: str) -> int:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))
    return 0


def simulate_edit(current: str, old_string: str, new_string: str,
                  replace_all: bool) -> str | None:
    if old_string not in current:
        return None
    if replace_all:
        return current.replace(old_string, new_string)
    return current.replace(old_string, new_string, 1)


def main() -> int:
    if os.environ.get("HANDOFF_FRONTMATTER_BYPASS") == "1":
        return _allow()

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return _allow()

    tool_name = payload.get("tool_name") or ""
    # MultiEdit is included deliberately: it can produce exactly the same final
    # content as Edit, so a guard that ignored it was bypassable by switching tool.
    if tool_name not in {"Write", "Edit", "MultiEdit"}:
        return _allow()

    tool_input = payload.get("tool_input") or {}
    file_path_str = tool_input.get("file_path") or ""
    if not file_path_str:
        return _allow()

    file_path = Path(file_path_str)
    # CHEAP GATES FIRST. This hook runs on every Write/Edit/MultiEdit, so the
    # ordering below is deliberate: the only filesystem WALK (in_vault) happens
    # last, after the string checks have already ruled out the overwhelming
    # majority of writes. A handoff is markdown by definition, so a non-markdown
    # path can exit before any I/O at all.
    if file_path.suffix.lower() not in MARKDOWN_EXTS:
        return _allow()

    if tool_name == "Write":
        proposed = tool_input.get("content") or ""
    else:
        if tool_name == "MultiEdit":
            edits = tool_input.get("edits") or []
        else:
            edits = [{
                "old_string": tool_input.get("old_string") or "",
                "new_string": tool_input.get("new_string") or "",
                "replace_all": bool(tool_input.get("replace_all")),
            }]
        if not edits or not file_path.exists():
            return _allow()
        try:
            current = file_path.read_text(encoding="utf-8")
        except Exception:
            return _allow()
        # Apply sequentially, exactly as the tool would. Any edit that does not
        # apply means we cannot model the result -> allow (fail open).
        for e in edits:
            result = simulate_edit(current, e.get("old_string") or "",
                                   e.get("new_string") or "",
                                   bool(e.get("replace_all")))
            if result is None:
                return _allow()
            current = result
        proposed = current

    if not is_handoff(file_path, proposed):
        return _allow()

    if has_valid_consumes_when(proposed):
        return _allow()

    # Only a file that WOULD be denied pays for the vault walk.
    if not in_vault(file_path):
        return _allow()

    # DENY on stdout as JSON, exit 0. NOT stderr+exit 2: the canonical wrapper in
    # hooks.json is `<hook> 2>/dev/null || echo '{...permissionDecision:allow}'`,
    # so a non-zero exit is swallowed and REWRITTEN into an allow. A guard that
    # blocked via exit 2 under that wrapper would be wired and still never block.
    return _deny(
        "HANDOFF FRONTMATTER MISSING `consumes_when:`\n"
        f"  file: {file_path}\n"
        "\n"
        "This file is a handoff (filename or `type:` frontmatter matches the "
        "handoff pattern). Every handoff MUST include a non-empty "
        "`consumes_when:` field naming the completion signal — without it, "
        "the next session-close scan cannot tell whether the bridged work "
        "shipped, and stale handoffs accumulate.\n"
        "\n"
        "Examples of valid `consumes_when:` values:\n"
        "  consumes_when: graph reaches >8000 nodes via Stage 5D Option B\n"
        "  consumes_when: claude-meeting-todos repo published on github.com/github-username\n"
        "  consumes_when: private-concierge.example.com production launch complete\n"
        "\n"
        "If you cannot name a concrete signal, the bridged work isn't "
        "concrete enough to ship — write a clearer plan first.\n"
        "\n"
        "Convention: handoff files live in `⚙️ Meta/Handoffs/`, not at "
        "`⚙️ Meta/` top-level. Consumed handoffs move to `Handoffs/Archive/`.\n"
        "\n"
        "Full lifecycle rule: ⚙️ Meta/rules/handoff-files.md\n"
        "Bypass (rare): HANDOFF_FRONTMATTER_BYPASS=1\n"
    )


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so the non-ASCII reason
    # text can't crash the hook on a Windows console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except Exception:
        # FAIL OPEN: a write-time guard must never block a legitimate write on a
        # bug of its own. Matches hooks/block-secret-in-note.py.
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
        sys.exit(0)
