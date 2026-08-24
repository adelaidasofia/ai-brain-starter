# MCP stack

Companion MCP servers built alongside ai-brain-starter. All public, MIT licensed, sharing the same architecture pattern.

Every server in this list:

- Uses FastMCP for the stdio transport (Python).
- Runs every write through draft + confirm (one-time confirm, 1-hour TTL, thread-safe).
- Writes a JSONL audit log with token redaction.
- Passes user-supplied text through a prompt-injection scrubber.
- Mirrors reads to an Obsidian vault folder where it makes sense.

You install whichever subset you need. They work independently of each other and independently of ai-brain-starter — the substrate just makes them more useful by giving them a vault to write into.

## The thirteen

### Communication

- [slack-mcp](https://github.com/adelaidasofia/slack-mcp) — multi-workspace Slack. xoxc / xoxp / xoxb auth. 18 tools spanning read, write, edit, delete, reactions, mark-read, vault export.
- [imessage-mcp](https://github.com/adelaidasofia/imessage-mcp) — macOS-only. Direct read of `~/Library/Messages/chat.db`. Whisper transcription on voice notes. FTS5 search. AppleScript send.
- [whatsapp-mcp](https://github.com/adelaidasofia/whatsapp-mcp) — multi-account WhatsApp via whatsmeow. Voice-note transcription. Send confirmation.
- [google-workspace-mcp](https://github.com/adelaidasofia/google-workspace-mcp) — Gmail / Calendar / Drive / Docs / Sheets. OS-native token storage via `keyring`: macOS Keychain, Windows Credential Manager, Linux Secret Service. 61 tools.

### Sales, writing, events

- [apollo-mcp](https://github.com/adelaidasofia/apollo-mcp) — Apollo.io. Sequences, campaign health, mailbox warmup, enrichment, CRM, credits. 22 tools.
- [substack-mcp](https://github.com/adelaidasofia/substack-mcp) — publish Notes and posts, pull analytics, manage drafts, bridge Obsidian vault drafts to Substack.
- [luma-mcp](https://github.com/adelaidasofia/luma-mcp) — lu.ma events. Create, update, ticket types, coupons, RSVPs, attendee email blasts. 14 tools.

### Documents, signal, graphs

- [parse-mcp](https://github.com/adelaidasofia/parse-mcp) — markitdown / Docling / LlamaParse router. Plus an `interpret` tool that pipes parsed markdown into Claude with prompt caching.
- [rescuetime-mcp](https://github.com/adelaidasofia/rescuetime-mcp) — RescueTime productivity data (daily summary, top apps, categories, trends).
- [graph-query-mcp](https://github.com/adelaidasofia/graph-query-mcp) — surgical queries against a NetworkX node-link graph.json. Pairs with `graphify` in this repo.
- [graph-autotagger-mcp](https://github.com/adelaidasofia/graph-autotagger-mcp) — wikilink suggestions from the same graph format. Drives the Obsidian autotagger pattern.

### Founder ops

- [investor-relations-mcp](https://github.com/adelaidasofia/investor-relations-mcp) — seed-raise pipeline tracker. Syncs from Obsidian CRM, generates meeting prep, tracks follow-up compliance.
- [vault-sync-mcp](https://github.com/adelaidasofia/vault-sync-mcp) — bidirectional sync between a personal vault and a team vault.

## How they fit with ai-brain-starter

The substrate (ai-brain-starter) teaches the pattern. Each MCP wraps a service that lives outside the vault. The vault stays the single source of truth; the MCPs are how it stays in sync.

A common install graph:

1. Vault first. Follow the install guide. Get the directory structure + skills + CLAUDE.md scaffolding in place.
2. Add MCPs by use case. If you live in Slack, add `slack-mcp`. If you take notes from voice notes, add `whatsapp-mcp` or `imessage-mcp`. If you fundraise, add `investor-relations-mcp`.
3. Wire each MCP into `<vault>/.mcp.json`. Restart Claude Code. Verify with `claude mcp list`.

Each MCP's `SETUP.md` carries the per-service auth flow. None of them require a hosted backend or paid plan unless explicitly noted.

## The publishing rule

This stack is governed by a four-gate rule:

A new MCP server goes public IFF:

1. Zero personal data in code and git history.
2. Zero coupling to any paid runtime (no hardcoded tenant IDs, no proprietary workflow content).
3. Generic utility — useful to anyone running Claude Code, not just one author's stack.
4. No TOS violation against the upstream service.

The rule plus a personal-data scrub script means new MCPs ship same-day. The thirteenth would take a fraction of the time of the first.

## What the public stack does not include

These 13 repos give you the wiring. The team install at [diazroa.com](https://diazroa.com) adds what the wiring depends on:

- **The runtime.** Memory across sessions with per-tenant isolation, hash-chain audit, and the orchestration layer the public skills hook into. Private. Hosted.
- **Per-vertical workflow content.** Legal review packs, fundraise pipelines, board-pack generators, event-ops playbooks. Built for each client.
- **Personal + team vault wired together** without leaking either way, with RBAC, contractor routing, and the vault-hygiene work most installs stall on in week three.
- **Office hours.** The systems compound when someone who has run them under load is the one tuning them with you.

Free 20-minute diagnostic on [diazroa.com](https://diazroa.com) if you want to see if the team install fits your shape.

## License

Each repo is MIT. See the `LICENSE` file in the individual repo.

---

Team install or hosted version at [diazroa.com](https://diazroa.com).
