#!/usr/bin/env python3
"""
lint-vault-frontmatter.py — PreToolUse Write|Edit hook.

Validates frontmatter on every Write or Edit operation against vault schemas.
Blocks the write if the post-edit content has malformed YAML or violates the
required schema for that file's type (decision, session, journal).

Same permanent-fix pattern that saved settings.json. Catches malformed YAML
at the write boundary instead of silently corrupting aggregator output.

Hook contract (Claude Code PreToolUse):
  Input (stdin JSON): {"tool_name": "Write|Edit", "tool_input": {...}}
  Output (stdout JSON):
    - {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}} → allow
    - {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}} → block

Behavior:
  - Only fires on files in Sessions/, Decisions/, or journal folders
  - Silent allow for any other file
  - Auto-detects type by path
  - Projects the post-edit content (current file + Edit substitution) before linting
  - Blocks with a clear stderr message naming the schema violation
  - Bypass: VAULT_LINT_BYPASS=1 in env
  - Fail-open: any internal error returns allow (never blocks the user spuriously)

Performance budget: <200ms.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def log_debug(msg: str) -> None:
    if os.environ.get("VAULT_LINT_DEBUG") == "1":
        print(f"[lint-vault-frontmatter] {msg}", file=sys.stderr)


def emit_allow() -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }))


def emit_deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def detect_type(file_path: str) -> str | None:
    parts = Path(file_path).parts
    for p in parts:
        if p == "Decisions":
            return "decision"
        if p == "Sessions":
            return "session"
        if "Journal" in p or "Daily Logs" in p:
            return "journal"
    return None


def project_post_edit_content(tool_name: str, tool_input: dict) -> str | None:
    """Return what the file content WILL be after the operation completes."""
    file_path = tool_input.get("file_path")
    if not file_path:
        return None

    if tool_name == "Write":
        return tool_input.get("content") or ""

    if tool_name == "Edit":
        try:
            existing = Path(file_path).read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            existing = ""
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        if tool_input.get("replace_all"):
            return existing.replace(old, new)
        return existing.replace(old, new, 1)

    if tool_name == "MultiEdit":
        try:
            content = Path(file_path).read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            content = ""
        for edit in tool_input.get("edits", []):
            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            if edit.get("replace_all"):
                content = content.replace(old, new)
            else:
                content = content.replace(old, new, 1)
        return content

    return None


def find_validator() -> Path | None:
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "scripts" / "vault-schema-validator.py",
        Path.home() / ".claude" / "skills" / "ai-brain-starter" / "scripts" / "vault-schema-validator.py",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def main() -> int:
    if os.environ.get("VAULT_LINT_BYPASS") == "1":
        emit_allow()
        return 0

    try:
        raw = sys.stdin.read()
        if not raw.strip():
            emit_allow()
            return 0
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        log_debug(f"hook input read failed: {e}")
        emit_allow()
        return 0

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}

    if tool_name not in ("Write", "Edit", "MultiEdit"):
        emit_allow()
        return 0

    file_path = tool_input.get("file_path", "")
    if not file_path or not file_path.endswith(".md"):
        emit_allow()
        return 0

    type_name = detect_type(file_path)
    if not type_name:
        emit_allow()
        return 0

    projected = project_post_edit_content(tool_name, tool_input)
    if projected is None:
        emit_allow()
        return 0

    # Extract frontmatter from projected content
    if not projected.startswith("---"):
        # No frontmatter; many session/decision files require frontmatter though.
        # Be permissive on Write of partial files; only warn via stderr.
        log_debug(f"no frontmatter in projected {file_path}")
        emit_allow()
        return 0

    # \r?\n, for the same reason scripts/vault-schema-validator.py carries it
    # (#409, locked by #431): projected content can reach here with CRLF intact.
    # On the Write path `projected` IS tool_input["content"] verbatim -- nothing
    # on that path applies universal-newline translation -- so a client that
    # emits CRLF meets an LF-only delimiter pattern and gets "not properly
    # closed", denying a perfectly valid vault write. This gate runs BEFORE the
    # validator subprocess, so the validator's own \r?\n fix is never reached to
    # cover it. (The Edit/MultiEdit path reads through Path.read_text(), which
    # does translate, so this is specifically the Write hole.)
    m = re.match(r"^---\r?\n(.*?)\r?\n---\s*", projected, re.DOTALL)
    if not m:
        emit_deny(
            f"Vault frontmatter linter: '---' delimiter not properly closed in {Path(file_path).name}. "
            f"Expected frontmatter to end with a line containing only '---'. "
            f"Bypass with VAULT_LINT_BYPASS=1 if this is intentional."
        )
        return 0

    fm_text = m.group(1)

    # Quick YAML parse check
    try:
        import yaml  # type: ignore
    except ImportError:
        log_debug("PyYAML not installed; skipping lint")
        emit_allow()
        return 0

    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        emit_deny(
            f"Vault frontmatter linter: YAML parse error in {Path(file_path).name}: {e}\n"
            f"This would silently break the aggregator. Fix the YAML or bypass with VAULT_LINT_BYPASS=1."
        )
        return 0

    if parsed is not None and not isinstance(parsed, dict):
        emit_deny(
            f"Vault frontmatter linter: frontmatter in {Path(file_path).name} must be a YAML mapping, "
            f"got {type(parsed).__name__}."
        )
        return 0

    # Schema validation via the standalone validator
    validator = find_validator()
    if not validator:
        log_debug("validator script not found; skipping schema check")
        emit_allow()
        return 0

    # Write projected content to a temp file and call validator
    import subprocess
    import tempfile
    # newline="" is REQUIRED, not cosmetic. Without it, Python text mode on
    # Windows rewrites every "\n" to "\r\n" as it writes. The validator then
    # reads this file through safe_read_text, which decodes BYTES directly
    # (no universal-newline translation), so it sees the injected "\r\n" and
    # its LF-only frontmatter regex fails — denying every Write/Edit of a
    # vault .md file on Windows regardless of content. Preserve bytes exactly.
    with tempfile.NamedTemporaryFile(
        suffix=".md", mode="w", delete=False, encoding="utf-8", newline=""
    ) as tf:
        # Adjust path so detect_type in validator finds the right schema
        tf.write(projected)
        tmp_path = tf.name

    try:
        # Re-create the directory structure hint via a symlink-style env var; the
        # validator reads --type explicitly, so we just pass it.
        result = subprocess.run(
            [sys.executable, str(validator), "--file", tmp_path, "--type", type_name, "--strict", "--quiet"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        log_debug(f"validator subprocess failed: {e}")
        emit_allow()
        return 0
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if result.returncode == 0:
        emit_allow()
        return 0

    # Strict mode = exit 2 means schema violation
    msg = (result.stdout + result.stderr).strip()
    if not msg:
        msg = f"unknown schema violation (validator exited {result.returncode})"
    emit_deny(
        f"Vault frontmatter linter blocked write to {Path(file_path).name}:\n{msg}\n"
        f"Fix the frontmatter or bypass with VAULT_LINT_BYPASS=1."
    )
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never block on unexpected errors
        log_debug(f"unexpected error: {e!r}")
        emit_allow()
