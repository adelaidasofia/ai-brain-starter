#!/usr/bin/env python3
"""
validate-skill-frontmatter.py - PreToolUse hook with two passes.

Pass 1 (write-time schema validation):
  Fires on Write|Edit|MultiEdit to skills/**/SKILL.md. Validates the projected
  post-edit frontmatter against templates/schemas/skill.json. Blocks malformed
  files at the write boundary so the skill catalog never goes structurally broken.
  Bypass: SKILL_VALIDATION_BYPASS=1.

Pass 2 (invocation-time capability sandboxing):
  Fires on any tool call when an active skill context is detected. Reads the
  active skill's tool_access whitelist and BLOCKS tool calls not on the list.
  This lifts skills from "documented contract" to "enforced sandbox".
  Bypass: SKILL_SANDBOX_BYPASS=1.

Skill-context detection (in order):
  1. CLAUDE_ACTIVE_SKILL env var. Value is the skill name (slug).
  2. CLAUDE_ACTIVE_SKILL_PATH env var. Value is the absolute path to SKILL.md.
  3. Most recent SKILL.md from session metadata (if available).
  4. Fall through: no active skill, validation skipped.

Hook contract (Claude Code PreToolUse):
  Input (stdin JSON): {"tool_name": "...", "tool_input": {...}}
  Output (stdout JSON):
    - {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}} -> allow
    - {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}} -> block

Pass 1 behavior:
  - Only fires on files matching skills/**/SKILL.md
  - Silent allow for any other file
  - Projects the post-edit content (current file + Edit substitution) before linting
  - Blocks with a clear stderr message naming the schema violation
  - Bypass: SKILL_VALIDATION_BYPASS=1 in env

Pass 2 behavior:
  - Fires on every tool call when CLAUDE_ACTIVE_SKILL is set
  - Reads the named skill's SKILL.md and resolves tool_access
  - Blocks tool_name not on the whitelist with a sandbox-specific message
  - Bypass: SKILL_SANDBOX_BYPASS=1 in env
  - Fail-open: any internal error returns allow (never blocks the user spuriously)

Both passes are fail-open on internal errors. Performance budget: <200ms.

Stdlib + PyYAML + jsonschema. PyYAML and jsonschema are dependencies of the
broader hook stack already; if missing, the hook fails open.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def log_debug(msg: str) -> None:
    if os.environ.get("SKILL_VALIDATION_DEBUG") == "1":
        print(f"[validate-skill-frontmatter] {msg}", file=sys.stderr)


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
    print(reason, file=sys.stderr)


def is_skill_md(file_path: str) -> bool:
    """True if file_path matches skills/**/SKILL.md (any depth under a skills/ dir)."""
    if not file_path:
        return False
    p = Path(file_path)
    if p.name != "SKILL.md":
        return False
    return "skills" in p.parts


def project_post_edit_content(tool_name: str, tool_input: dict) -> str | None:
    """Return what the file content WILL be after the operation completes."""
    file_path = tool_input.get("file_path")
    if not file_path:
        return None

    if tool_name == "Write":
        return tool_input.get("content") or ""

    if tool_name == "Edit":
        try:
            existing = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except (OSError, FileNotFoundError):
            existing = ""
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        if tool_input.get("replace_all"):
            return existing.replace(old, new)
        return existing.replace(old, new, 1)

    if tool_name == "MultiEdit":
        try:
            content = Path(file_path).read_text(encoding="utf-8", errors="replace")
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


def find_schema() -> Path | None:
    """Locate templates/schemas/skill.json relative to this hook."""
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "templates" / "schemas" / "skill.json",
        Path.home() / ".claude" / "skills" / "ai-brain-starter" / "templates" / "schemas" / "skill.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def extract_frontmatter(text: str) -> tuple[str | None, str | None]:
    """Extract frontmatter block. Returns (frontmatter_text, error)."""
    if not text.startswith("---"):
        return None, "missing frontmatter (file does not start with '---')"
    m = re.match(r"^---\n(.*?)\n---\s*", text, re.DOTALL)
    if not m:
        return None, "frontmatter delimiter '---' not properly closed"
    return m.group(1), None


def find_skill_md_for_name(skill_name: str) -> Path | None:
    """Locate SKILL.md for a named skill across known skill roots.

    Search order:
      1. Repo-local skills/<name>/SKILL.md (for development).
      2. ~/.claude/skills/<name>/SKILL.md (for installed user skills).
      3. ~/.claude/skills/ai-brain-starter/skills/<name>/SKILL.md (bundled).
    """
    if not skill_name:
        return None
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "skills" / skill_name / "SKILL.md",
        Path.home() / ".claude" / "skills" / skill_name / "SKILL.md",
        Path.home() / ".claude" / "skills" / "ai-brain-starter" / "skills" / skill_name / "SKILL.md",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def resolve_active_skill_md() -> Path | None:
    """Determine the active skill SKILL.md, if any.

    Detection order (first match wins):
      1. CLAUDE_ACTIVE_SKILL_PATH env var (absolute path to SKILL.md).
      2. CLAUDE_ACTIVE_SKILL env var (skill name; resolve to a path).
      3. None (no active skill, sandboxing pass is skipped).
    """
    explicit_path = os.environ.get("CLAUDE_ACTIVE_SKILL_PATH", "").strip()
    if explicit_path:
        p = Path(explicit_path)
        if p.is_file():
            return p
        log_debug(f"CLAUDE_ACTIVE_SKILL_PATH {explicit_path!r} not a file")

    skill_name = os.environ.get("CLAUDE_ACTIVE_SKILL", "").strip()
    if skill_name:
        resolved = find_skill_md_for_name(skill_name)
        if resolved is not None:
            return resolved
        log_debug(f"CLAUDE_ACTIVE_SKILL {skill_name!r} not resolvable")

    return None


def load_skill_tool_access(skill_md_path: Path) -> tuple[list[str] | None, str | None]:
    """Read SKILL.md frontmatter and return (tool_access, error).

    tool_access is a list of allowed tool ids. None means the skill does not
    declare a whitelist (sandboxing pass is then a no-op for that skill).
    """
    try:
        text = skill_md_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return None, f"could not read {skill_md_path}: {e}"

    fm_text, fm_err = extract_frontmatter(text)
    if fm_err:
        return None, fm_err

    try:
        import yaml  # type: ignore
    except ImportError:
        return None, "PyYAML not installed"

    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        return None, f"YAML parse error: {e}"

    if not isinstance(parsed, dict):
        return None, "frontmatter is not a mapping"

    if "tool_access" not in parsed:
        return None, None

    tool_access = parsed.get("tool_access")
    if not isinstance(tool_access, list):
        return None, "tool_access is not a list"

    cleaned = [str(t).strip() for t in tool_access if isinstance(t, (str, bytes)) and str(t).strip()]
    return cleaned, None


def run_schema_validation(tool_name: str, tool_input: dict) -> int:
    """Pass 1: schema validation on Write|Edit|MultiEdit to skills/**/SKILL.md.

    Returns the process exit code. Emits the hook decision JSON.
    """
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        emit_allow()
        return 0

    file_path = tool_input.get("file_path", "")
    if not is_skill_md(file_path):
        emit_allow()
        return 0

    projected = project_post_edit_content(tool_name, tool_input)
    if projected is None:
        emit_allow()
        return 0

    fm_text, fm_err = extract_frontmatter(projected)
    if fm_err:
        emit_deny(
            f"Skill validator: {fm_err} in {Path(file_path).name}. "
            f"Bypass with SKILL_VALIDATION_BYPASS=1 if intentional."
        )
        return 1

    try:
        import yaml  # type: ignore
    except ImportError:
        log_debug("PyYAML not installed; skipping validation")
        emit_allow()
        return 0

    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        emit_deny(
            f"Skill validator: YAML parse error in {Path(file_path).name}: {e}\n"
            f"Bypass with SKILL_VALIDATION_BYPASS=1 if intentional."
        )
        return 1

    if parsed is None or not isinstance(parsed, dict):
        emit_deny(
            f"Skill validator: frontmatter in {Path(file_path).name} must be a YAML mapping, "
            f"got {type(parsed).__name__ if parsed is not None else 'null'}."
        )
        return 1

    schema_path = find_schema()
    if not schema_path:
        log_debug("schema file not found; skipping validation")
        emit_allow()
        return 0

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as e:
        log_debug(f"schema load failed: {e}")
        emit_allow()
        return 0

    try:
        import jsonschema  # type: ignore
    except ImportError:
        log_debug("jsonschema not installed; skipping validation")
        emit_allow()
        return 0

    try:
        validator = jsonschema.Draft7Validator(schema)
        errors = sorted(validator.iter_errors(parsed), key=lambda e: list(e.absolute_path))
    except jsonschema.SchemaError as e:
        log_debug(f"schema is itself invalid: {e}")
        emit_allow()
        return 0

    if errors:
        details = []
        for err in errors[:5]:
            loc = ".".join(str(x) for x in err.absolute_path) or "<root>"
            details.append(f"  - {loc}: {err.message}")
        if len(errors) > 5:
            details.append(f"  ... and {len(errors) - 5} more")
        emit_deny(
            f"Skill validator blocked write to {Path(file_path).name}:\n"
            + "\n".join(details)
            + f"\nFix the frontmatter or bypass with SKILL_VALIDATION_BYPASS=1."
        )
        return 1

    emit_allow()
    return 0


def run_capability_sandbox(tool_name: str) -> int:
    """Pass 2: capability sandboxing.

    Reads the active skill (env var) and blocks tool calls outside its
    declared tool_access whitelist. No-op when no active skill is set or
    when the skill does not declare tool_access.

    Returns the process exit code. Emits the hook decision JSON only when
    blocking; allow is left to the caller so we do not emit twice.
    """
    if os.environ.get("SKILL_SANDBOX_BYPASS") == "1":
        return 0

    skill_md = resolve_active_skill_md()
    if skill_md is None:
        return 0

    tool_access, err = load_skill_tool_access(skill_md)
    if err is not None:
        log_debug(f"sandbox: skipping skill {skill_md} due to error: {err}")
        return 0
    if tool_access is None:
        return 0

    if tool_name in tool_access:
        return 0

    skill_name = skill_md.parent.name
    declared = ", ".join(tool_access) if tool_access else "<empty>"
    emit_deny(
        f"SKILL SANDBOX: skill {skill_name} declares tool_access=[{declared}]; "
        f"attempted to call tool {tool_name}. Blocked. "
        f"Bypass: SKILL_SANDBOX_BYPASS=1."
    )
    return 1


def main() -> int:
    if os.environ.get("SKILL_VALIDATION_BYPASS") == "1" and \
       os.environ.get("SKILL_SANDBOX_BYPASS") == "1":
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

    # Pass 2 runs first because it applies to all tool calls. If the
    # sandbox blocks, emit deny and stop. If sandbox passes (or is not
    # active), run pass 1 only when applicable.
    if os.environ.get("SKILL_SANDBOX_BYPASS") != "1":
        sandbox_rc = run_capability_sandbox(tool_name)
        if sandbox_rc != 0:
            return sandbox_rc

    if os.environ.get("SKILL_VALIDATION_BYPASS") == "1":
        emit_allow()
        return 0

    return run_schema_validation(tool_name, tool_input)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log_debug(f"unexpected error: {e!r}")
        emit_allow()
        sys.exit(0)
