#!/usr/bin/env python3
"""PreToolUse Bash hook: block `claude mcp add ... --env <NAME>=<value>` when
<value> looks like a secret.

Pattern this prevents: every Claude Code child spawn includes the full MCP
config as a `--mcp-config '<json>'` argv string. Any `ps aux` / `pgrep -fl`
/ `lsof` on the running `claude` process dumps the PAT in plaintext. Past
incident: 2026-05-19 github-mcp PAT leaked via `pgrep -fl chrome-devtools-mcp`.

Correct pattern: set the secret in `~/.zsh_secrets` (sourced from `~/.zshenv`),
add the MCP server WITHOUT an `--env` block. The MCP child process inherits
the shell env, so the secret stays out of argv.

Block applies to `claude mcp add` only. Other claude-mcp subcommands pass.
Bypass: MCP_INLINE_SECRET_BYPASS=1.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Inline-bypass consult: os.environ can't see a `MCP_INLINE_SECRET_BYPASS=1
# <cmd>` prefix that lives only in the command string, never the hook's own
# env (HOOK-READS-SESSION-ENV-NOT-COMMAND-ENV). Fail-open to env-only if the
# shared helper is unavailable.
sys.path.insert(0, str(Path(__file__).resolve().parent / "_lib"))
try:
    from cmd_env import inline_bypass
except Exception:
    def inline_bypass(command, var, value="1"):  # type: ignore
        return False

SECRET_PATTERNS = [
    r"sk-ant-[A-Za-z0-9_\-]+",
    r"sk-[A-Za-z0-9_\-]{20,}",
    r"github_pat_[A-Za-z0-9_]+",
    r"ghp_[A-Za-z0-9]+",
    r"ghs_[A-Za-z0-9]+",
    r"pat-[a-z0-9_\-]+",
    r"xoxb-[0-9A-Za-z\-]+",
    r"xoxp-[0-9A-Za-z\-]+",
    r"glpat-[A-Za-z0-9_\-]+",
    r"Bearer\s+[A-Za-z0-9_\-\.]+",
    r"AKIA[0-9A-Z]{16}",
    r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+",  # JWT
]

SECRET_RE = re.compile("|".join(SECRET_PATTERNS))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    tool = payload.get("tool_name") or payload.get("tool", "")
    if tool != "Bash":
        return 0

    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not cmd:
        return 0

    if os.environ.get("MCP_INLINE_SECRET_BYPASS") == "1" or inline_bypass(
        cmd, "MCP_INLINE_SECRET_BYPASS"
    ):
        return 0

    # Only flag `claude mcp add ...` commands
    if not re.search(r"\bclaude\s+mcp\s+add\b", cmd):
        return 0

    # Look for --env NAME=value or -e NAME=value where value matches a secret
    env_arg_re = re.compile(r"(?:--env|-e)\s+[A-Z_][A-Z0-9_]*=([^\s'\"]+)")
    for match in env_arg_re.finditer(cmd):
        value = match.group(1)
        if SECRET_RE.search(value):
            print(
                "[block-claude-mcp-inline-secret] BLOCKED:\n"
                "  Inlining a secret-looking value via `--env NAME=...` puts it\n"
                "  into the MCP config at ~/.claude.json, which every Claude\n"
                "  Code child spawn passes via --mcp-config '<json>' argv. Any\n"
                "  `ps aux` / `pgrep -fl` / `lsof` on the running claude process\n"
                "  will dump the secret in plaintext.\n\n"
                "  Correct pattern (env-injection):\n"
                "    1. echo 'export NAME=value' >> ~/.zsh_secrets\n"
                "    2. claude mcp add <server> --scope user -- <command>\n"
                "       (NO --env block; the child inherits shell env)\n"
                "    3. Restart Claude Code to pick up the new env\n\n"
                "  Past incident: 2026-05-19 github-mcp PAT leaked via\n"
                "  pgrep -fl chrome-devtools-mcp (the Bash output included\n"
                "  the claude process's full --mcp-config argv).\n\n"
                "  Bypass: MCP_INLINE_SECRET_BYPASS=1",
                file=sys.stderr,
            )
            return 2  # block

    return 0


if __name__ == "__main__":
    sys.exit(main())
