#!/usr/bin/env python3
"""
Lint Claude Code settings + MCP config files for silent breakage.

Detects:
  1. Duplicate keys at ANY depth (json.load tolerates them; last wins;
     allowlists/hooks/permissions get nuked when a second `permissions: {...}`
     block lands at the same depth)
  2. Unknown enum values for known fields (model, theme)
  3. Hooks/commands referencing files that don't exist on disk
  4. Permissions/allow entries with bare commands (must be wrapped Bash(...))

Why this hook exists: standard json.load() is silently tolerant of duplicate
top-level keys. A common failure: a user appends a second `"permissions": {...}`
block at the bottom of `~/.claude/settings.json` to grant a few launchctl perms,
and the original gh/git push allowlist at the top is wiped — last value wins.
The user keeps re-approving the same permissions every session, never realizing
the config has been silently corrupt for weeks.

Modes:
  (default)   Warn-only, exit 0. For SessionStart + FileChanged (drift detection).
  --strict    Exit 2 on any BLOCK-severity issue. For PreToolUse blocker.
  --test      Run self-test against in-process bad fixtures. Exits 1 if a guard
              fails its own test. Wire into SessionStart so guard rot fails loud.
  --paths X Y Override which files to lint.
  --content STR --label LABEL  Lint JSON content directly (used by the PreToolUse
              wrapper hook on tool_input.content for Write).

Coverage (default mode):
  ~/.claude/settings.json
  ~/.claude/settings.local.json
  cwd/.claude/settings.json
  cwd/.claude/settings.local.json
  cwd/.mcp.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

HOME = Path(os.path.expanduser("~"))

KNOWN_ENUMS = {
    "model": {"sonnet", "opus", "haiku", "opusplan", "default"},
    "theme": {"light", "dark", "dark-daltonized", "light-daltonized", "system"},
}

WARN: list[tuple[str, str]] = []  # (severity, msg) — severity in {"BLOCK", "WARN"}


def _emit(sev: str, msg: str) -> None:
    WARN.append((sev, msg))


def collect_dups(pairs, path="$"):
    keys = [k for k, _ in pairs]
    counts = Counter(keys)
    for k, c in counts.items():
        if c > 1:
            _emit("BLOCK", f"DUP-KEY at {path}: '{k}' appears {c}x — last value wins, earlier ones SILENTLY LOST")
    out = {}
    for k, v in pairs:
        out[k] = v
    return out


def check_enums(obj, path="$"):
    if not isinstance(obj, dict):
        return
    for field, allowed in KNOWN_ENUMS.items():
        if field in obj and isinstance(obj[field], str) and obj[field] not in allowed:
            _emit("WARN", f"UNKNOWN-ENUM at {path}.{field}: '{obj[field]}' not in {sorted(allowed)}")


def check_hook_paths(obj, path="$"):
    if not isinstance(obj, dict):
        return
    hooks = obj.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            for j, h in enumerate(entry.get("hooks", []) or []):
                cmd = h.get("command", "")
                if not cmd:
                    continue
                for tok in cmd.split():
                    if tok.startswith("/") and not tok.startswith("//"):
                        if not Path(tok).exists():
                            _emit("WARN", f"MISSING-HOOK-FILE at hooks.{event}[{i}].hooks[{j}]: {tok}")
                        break


def check_perms(obj, path="$"):
    perms = obj.get("permissions", {}) if isinstance(obj, dict) else {}
    allow = perms.get("allow", []) if isinstance(perms, dict) else []
    for i, rule in enumerate(allow if isinstance(allow, list) else []):
        if not isinstance(rule, str):
            continue
        if rule.startswith(("git ", "gh ", "npm ", "ls ", "cat ", "find ")) and not rule.startswith("Bash("):
            _emit("WARN", f"BAD-PERM at permissions.allow[{i}]: '{rule}' — bare command, must be wrapped Bash(...)")


def lint_file(path: Path, label: str) -> None:
    if not path.exists():
        return
    try:
        with path.open() as f:
            data = json.load(f, object_pairs_hook=lambda p: collect_dups(p, path=label))
    except json.JSONDecodeError as e:
        _emit("BLOCK", f"INVALID-JSON in {label}: {e}")
        return
    check_enums(data, path=label)
    check_hook_paths(data, path=label)
    check_perms(data, path=label)


def lint_content(content: str, label: str) -> None:
    """Lint a JSON string directly (used by the PreToolUse wrapper)."""
    try:
        data = json.loads(content, object_pairs_hook=lambda p: collect_dups(p, path=label))
    except json.JSONDecodeError as e:
        _emit("BLOCK", f"INVALID-JSON in {label}: {e}")
        return
    check_enums(data, path=label)
    check_perms(data, path=label)


def default_targets() -> list[tuple[Path, str]]:
    candidates = [
        (HOME / ".claude" / "settings.json", "~/.claude/settings.json"),
        (HOME / ".claude" / "settings.local.json", "~/.claude/settings.local.json"),
        (Path.cwd() / ".claude" / "settings.json", "cwd/.claude/settings.json"),
        (Path.cwd() / ".claude" / "settings.local.json", "cwd/.claude/settings.local.json"),
        (Path.cwd() / ".mcp.json", "cwd/.mcp.json"),
    ]
    seen = set()
    out = []
    for p, lbl in candidates:
        rp = p.resolve() if p.exists() else p
        if rp in seen:
            continue
        seen.add(rp)
        out.append((p, lbl))
    return out


# ─── Self-test (guards must fail loudly on known-bad input) ────────────────

SELF_TEST_FIXTURES = [
    {
        "name": "duplicate top-level key",
        "json": '{"a": 1, "a": 2}',
        "expects": "DUP-KEY",
    },
    {
        "name": "duplicate nested key",
        "json": '{"permissions": {"allow": [], "allow": []}}',
        "expects": "DUP-KEY",
    },
    {
        "name": "unknown model enum",
        "json": '{"model": "gpt-4"}',
        "expects": "UNKNOWN-ENUM",
    },
    {
        "name": "invalid json",
        "json": '{"a": ',
        "expects": "INVALID-JSON",
    },
    {
        "name": "valid config (must NOT trigger)",
        "json": '{"model": "sonnet", "theme": "light"}',
        "expects": None,
    },
]


def run_self_test() -> int:
    fails = []
    for fx in SELF_TEST_FIXTURES:
        WARN.clear()
        lint_content(fx["json"], label=f"<self-test:{fx['name']}>")
        msgs = " | ".join(m for _, m in WARN)
        expected = fx["expects"]
        if expected is None:
            if WARN:
                fails.append(f"FALSE POSITIVE on '{fx['name']}': got {msgs}")
        else:
            if not any(expected in m for _, m in WARN):
                fails.append(f"MISSED on '{fx['name']}': expected {expected}, got '{msgs}'")
    if fails:
        print("[lint-claude-settings:self-test] FAILED:", file=sys.stderr)
        for f in fails:
            print(f"  x {f}", file=sys.stderr)
        return 1
    return 0


# ─── Main ──────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="Exit 2 on BLOCK-severity issues")
    ap.add_argument("--test", action="store_true", help="Run self-test against fixtures")
    ap.add_argument("--paths", nargs="+", help="Specific files to lint")
    ap.add_argument("--content", help="Lint JSON content directly (with --label)")
    ap.add_argument("--label", default="<stdin>", help="Label for --content")
    args = ap.parse_args()

    if args.test:
        return run_self_test()

    if args.content is not None:
        lint_content(args.content, label=args.label)
    elif args.paths:
        for p in args.paths:
            lint_file(Path(p), label=p)
    else:
        for p, lbl in default_targets():
            lint_file(p, label=lbl)

    if WARN:
        blocking = [m for sev, m in WARN if sev == "BLOCK"]
        warning = [m for sev, m in WARN if sev == "WARN"]
        print("=" * 60, file=sys.stderr)
        print("[lint-claude-settings] issues detected:", file=sys.stderr)
        for m in blocking:
            print(f"  x BLOCK: {m}", file=sys.stderr)
        for m in warning:
            print(f"  ! WARN:  {m}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        if args.strict and blocking:
            return 2
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    # A hook that print()s non-ASCII raises UnicodeEncodeError on a cp1252
    # console: the gate then fails silently OPEN, or denies the tool call with
    # no legible cause. Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
