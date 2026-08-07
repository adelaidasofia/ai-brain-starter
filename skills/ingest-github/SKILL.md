---
name: ingest-github
description: Use when the user says /ingest-github <owner/repo> [--days N], or asks to ingest, capture, sync, pull, mirror, or import a GitHub repo's recent activity (merged PRs, issues, commits) into the vault, second brain, or knowledge graph, or wants repo history queryable by graphify or External Inputs refreshed. Not for opening PRs, creating issues, one-off PR diff reads, or non-GitHub sources (Slack, Notion, email have their own ingest-* connectors).
argument-hint: "<owner/repo> [--days N]"
---

# ingest-github: GitHub-to-vault connector

> **`{SKILL_DIR}`** = this skill's own folder (locally: the directory this SKILL.md lives in; a served brain substitutes the real absolute path before you read this). If a path does not resolve, name the missing file and stop — never guess another location.

Ingests recent GitHub repository activity into the vault as markdown the graphify pipeline can read and the rest of the AI Brain Starter substrate (decision log, session-close cascade, hooks) can act on.

This is the second connector in the ingest-* pattern. Adding the next external source means writing a new normalizer, not a new architecture.

## When to use

- User says `/ingest-github <owner/repo>` (with or without `--days N`)
- User asks to capture, sync, ingest, or pull a GitHub repository into the vault
- User mentions wanting recent PRs, issues, or commits available to the knowledge graph

Do NOT use for:
- Opening pull requests or creating issues (use the GitHub MCP write tools or `gh` CLI directly)
- One-off PR diff reads (use `gh pr view` or the GitHub MCP)
- Non-GitHub sources (Slack, Notion, email get their own connectors)

## How it works

1. Parse the `<owner>/<repo>` argument, normalize to a slug (`owner-repo`)
2. Fetch merged PRs from the last N days (default 7) via the GitHub MCP `list_pull_requests` tool (state=closed, filter to merged)
3. Fetch issues from the last N days via `list_issues` (any state, since the date window)
4. Fetch commits from the last N days on the default branch via `list_commits`
5. For each PR with a body, optionally pull review comments and inline conversation
6. Normalize all items chronologically into one markdown body, grouped by item type
7. Write to `External Inputs/GitHub/<owner-repo>/<YYYY-MM-DD>.md`
8. Print summary: file path, PR count, issue count, commit count

## Voice rules

- No em dashes (use commas, colons, periods, parentheses)
- No exclamation marks
- Direct, no fluff
- Author logins quoted verbatim (the GitHub login, not the display name)
- Body excerpts truncated at 800 characters per item to keep the vault file readable

## MCP requirement

This skill calls a GitHub MCP for read access (`list_pull_requests`, `list_issues`, `list_commits`, `get_pull_request`). If no GitHub MCP is connected to your Claude Code install, the skill prints a clear error naming the missing MCP and instructions for connecting one (the canonical reference is the official `@modelcontextprotocol/server-github` package or the GitHub Copilot MCP). The skill does not silently fall back to the public REST API; it surfaces the gap so you can wire the MCP once and run the skill cleanly.

If the MCP is connected but the token lacks repo read scope, the call returns 403 and the skill reports the scope issue.

## Invocation

The skill is a thin orchestrator. The actual normalization runs in Python at `{SKILL_DIR}/ingest.py`. The skill assembles the GitHub MCP tool calls, hands the raw payloads to `ingest.py` as JSON on stdin, and the script writes the file.

When invoked:

1. Parse arguments: `<owner>/<repo>` (required), `--days N` (optional, default 7)
2. Verify a GitHub MCP is available. If not, print the missing-MCP error and stop.
3. Call `list_pull_requests` for the repo with `state=closed`, filter to PRs merged within the last N days.
4. Call `list_issues` for the repo with `since=now - N days`. Filter out PRs (GitHub returns issues that are also PRs in this list; drop entries with `pull_request` set).
5. Call `list_commits` for the repo with `since=now - N days` on the default branch.
6. Optionally for each PR with non-empty body, call `get_pull_request` for full body + linked issues. Cheap to skip if you want a fast run.
7. Hand the assembled payload (repo metadata + PRs + issues + commits) to `ingest.py` as JSON on stdin.
8. `ingest.py` writes the vault file and prints a summary.
9. Surface the summary to the user.

## Output contract

The vault file at `External Inputs/GitHub/<owner-repo>/<YYYY-MM-DD>.md` has frontmatter:

```yaml
---
type: external-input
source: github
repo: <owner/repo>
date_range: <YYYY-MM-DD>..<YYYY-MM-DD>
item_count: <int>
ingested_at: <ISO 8601 timestamp>
entity_ids:
  github_repo: <owner/repo>
  github_pr: [<pr_number>, ...]
  github_issue: [<issue_number>, ...]
---
```

Body is grouped by item type, each group chronologically sorted:

- `## Merged PRs` then one `### #<number> <title>` per PR with author, merged_at, body excerpt, linked issues
- `## Issues` then one `### #<number> <title>` per issue with author, state, created_at, body excerpt
- `## Commits` then one `### <short_sha> <subject>` per commit with author, committed_at, body excerpt

The `entity_ids` block conforms to the cross-type frontmatter contract so downstream consumers (graph builders, fact aggregators, agents) can join GitHub items to their source records without re-parsing the body.

## Idempotency

Re-running `/ingest-github <owner/repo> --days N` on the same calendar day overwrites the same vault file. The file path is keyed by date and repo slug, so the same source produces the same path across re-runs. Re-runs do not duplicate; they refresh.

## Acceptance test

A successful run produces:
1. One new (or refreshed) file at `External Inputs/GitHub/<owner-repo>/<date>.md`
2. A stdout summary: `Wrote N item(s) (P prs, I issues, C commits) to <path>.`

If the repo resolves but contains no activity in the date range, write the file anyway with `item_count: 0` so re-runs are still idempotent and the absence is recorded.
