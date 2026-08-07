---
name: block-malformed-mcp-json
enabled: true
event: file
action: block
conditions:
  - field: file_path
    operator: regex_match
    pattern: \.mcp\.json$
  - field: new_text
    operator: regex_match
    pattern: '^(?!\s*\{[\s\S]*\}\s*$)'
---

> Maintainer note: the second condition is an ANCHORED NEGATIVE LOOKAHEAD, not the
> more readable `operator: regex_not_match`. That operator is not implemented by the
> official hookify engine (unknown operator -> False -> this rule would load fine and
> SILENTLY NEVER BLOCK). Do not "simplify" it back until upstream ships it
> (anthropics/claude-code#78715). The `^` anchor is load-bearing: `re.search` scans
> every position, so an unanchored lookahead would match later in the string and
> wrongly fire on valid JSON.

**`.mcp.json` content does not look like a complete JSON object.** Claude Code silently drops malformed MCP config files: every MCP server inside the file will stop loading with no warning. Common cause: orphaned blocks, stray braces, or content added outside the top-level `mcpServers` object. Fix the JSON structure (must open with `{` and close with matching `}`) before saving. Validate with `python3 -c "import json; json.load(open('<path>'))"`
