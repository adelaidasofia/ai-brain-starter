#!/usr/bin/env python3
"""MCP config health check. Run at session start or as a standalone check.

Catches the six silent-fail bugs most commonly encountered:
  1. Malformed .mcp.json (silently drops entire file)
  2. Registered stdio server path doesn't exist
  3. Blank env values (crashes server at startup)
  4. Ghost ~/.claude/.mcp.json present (never honored by Claude Code)
  5. Orphan MCP directories with server.py but no registration
  6. Top-level mcpServers key in ~/.claude.json (ghost location, Claude Code ignores it;
     use projects[HOME_PATH].mcpServers via `claude mcp add -s user` instead)

Output contract:
  STATUS: OK | ISSUES | ERROR
  ISSUE_COUNT: <int>
  ---ISSUES---
  <severity>|<category>|<path or name>|<one-line description>
  ...
  ---END---

Severity: CRITICAL | WARN | INFO
Categories: parse | path | env | ghost | orphan

Exits 0 on OK, 1 on ISSUES, 2 on ERROR.

Configuration (via environment variables):
  VAULT_ROOT        — directory containing .mcp.json (default: current working dir)
  MCP_SCAN_DIRS     — colon-separated list of dirs to scan for orphan MCP servers
                      (default: ~/Desktop)
  MCP_DIR_SUFFIX    — directory name suffix that marks an MCP server dir (default: -mcp)
"""

from __future__ import annotations
import json
import os
import sys
from pathlib import Path

# ---- Config ----

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", os.getcwd()))
USER_CLAUDE_JSON = Path.home() / ".claude.json"
GHOST_PATH = Path.home() / ".claude" / ".mcp.json"
MCP_DIR_SUFFIX = os.environ.get("MCP_DIR_SUFFIX", "-mcp")

# Dirs to scan for orphan MCP servers (colon-separated env var)
_scan_env = os.environ.get("MCP_SCAN_DIRS", str(Path.home() / "Desktop"))
MCP_DIRS_TO_SCAN = [Path(p) for p in _scan_env.split(":") if p.strip()]

# ---- Helpers ----

def load_project_mcp() -> tuple[dict, str | None]:
    """Return (parsed, error). parsed is {} on error; error is None on success."""
    path = VAULT_ROOT / ".mcp.json"
    if not path.exists():
        return {}, None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return {}, f"JSONDecodeError: {e.msg} at line {e.lineno} col {e.colno}"
    except Exception as e:
        return {}, f"{type(e).__name__}: {e}"


def load_user_mcps() -> dict:
    """Return mcpServers from ~/.claude.json project-scoped entry for this vault.

    Claude Code honors user-scoped MCPs ONLY under projects[HOME_PATH].mcpServers,
    not under top-level mcpServers. Top-level is a ghost location.
    """
    if not USER_CLAUDE_JSON.exists():
        return {}
    try:
        with USER_CLAUDE_JSON.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    home_proj = data.get("projects", {}).get(str(Path.home()), {})
    return home_proj.get("mcpServers", {})


def check_claude_json_top_level_ghost() -> list[tuple[str, str, str, str]]:
    """Flag top-level mcpServers in ~/.claude.json (ghost location, silently ignored)."""
    if not USER_CLAUDE_JSON.exists():
        return []
    try:
        with USER_CLAUDE_JSON.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if "mcpServers" not in data:
        return []
    names = list(data["mcpServers"].keys())
    return [(
        "CRITICAL", "ghost", str(USER_CLAUDE_JSON),
        f"Top-level mcpServers in ~/.claude.json is silently ignored by Claude Code. "
        f"Move {len(names)} server(s) ({', '.join(names)}) to "
        f"projects[HOME].mcpServers via `claude mcp add -s user` or delete them.",
    )]


def server_path(cfg: dict) -> Path | None:
    """Extract the server file path from a stdio server config, if any."""
    if cfg.get("type") not in (None, "stdio"):
        return None
    args = cfg.get("args") or []
    for arg in args:
        if isinstance(arg, str) and arg.endswith(".py"):
            return Path(arg)
    for arg in args:
        if isinstance(arg, str) and "/" in arg:
            return Path(arg)
    return None


def find_orphan_dirs(registered_paths: set[Path]) -> list[Path]:
    """Find MCP directories that have server.py but aren't referenced by any registration."""
    registered_parents = {p.parent.resolve() for p in registered_paths if p}
    orphans = []
    for root in MCP_DIRS_TO_SCAN:
        if not root.exists():
            continue
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                if not child.name.endswith(MCP_DIR_SUFFIX):
                    continue
                server_py = child / "server.py"
                if server_py.exists() and child.resolve() not in registered_parents:
                    orphans.append(server_py)
        except PermissionError:
            continue
    return orphans


# ---- Checks ----

def main() -> int:
    issues: list[tuple[str, str, str, str]] = []

    # 1. Ghost files
    if GHOST_PATH.exists():
        issues.append((
            "CRITICAL", "ghost", str(GHOST_PATH),
            "Ghost MCP config exists at path Claude Code does not honor. Delete it.",
        ))
    issues.extend(check_claude_json_top_level_ghost())

    # 2. Project .mcp.json parse
    project_cfg, project_err = load_project_mcp()
    if project_err:
        issues.append((
            "CRITICAL", "parse", str(VAULT_ROOT / ".mcp.json"),
            f"Project .mcp.json does not parse: {project_err}. Every MCP in this file is silently dropped.",
        ))

    project_servers = project_cfg.get("mcpServers", {}) if isinstance(project_cfg, dict) else {}
    user_servers = load_user_mcps()

    all_servers = {}
    for name, cfg in project_servers.items():
        all_servers[f"project:{name}"] = cfg
    for name, cfg in user_servers.items():
        all_servers[f"user:{name}"] = cfg

    registered_paths: set[Path] = set()

    for scoped_name, cfg in all_servers.items():
        if not isinstance(cfg, dict):
            continue

        # 3. Path existence (stdio only)
        sp = server_path(cfg)
        if sp is not None:
            registered_paths.add(sp)
            if not sp.exists():
                issues.append((
                    "CRITICAL", "path", scoped_name,
                    f"Registered server path does not exist: {sp}",
                ))

        # 4. Blank env values
        env = cfg.get("env") or {}
        for k, v in env.items():
            if v == "":
                issues.append((
                    "WARN", "env", scoped_name,
                    f"Env var {k} is blank string. Server will crash at init. Set a value or remove the key.",
                ))

    # 5. Orphans
    for orphan in find_orphan_dirs(registered_paths):
        issues.append((
            "INFO", "orphan", str(orphan.parent),
            f"MCP directory has server.py but no registration: {orphan.parent.name}",
        ))

    # ---- Output ----
    if issues:
        print("STATUS: ISSUES")
        print(f"ISSUE_COUNT: {len(issues)}")
        print("---ISSUES---")
        for sev, cat, subj, desc in issues:
            print(f"{sev}|{cat}|{subj}|{desc}")
        print("---END---")
        return 1
    else:
        print("STATUS: OK")
        print("ISSUE_COUNT: 0")
        print("---ISSUES---")
        print("---END---")
        return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except Exception as e:
        print("STATUS: ERROR")
        print("ISSUE_COUNT: 0")
        print(f"REASON: {type(e).__name__}: {e}")
        sys.exit(2)
