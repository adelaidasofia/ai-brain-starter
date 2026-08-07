---
name: vault-system
description: 'Use when maintaining the vault ITSELF as a system, not its content: knowledge-graph build/rebuild/query (graphify), second-brain mapping, vault diagnose/audit/hygiene, drift detection, extract rules from vault, backfill journals, setup vault types, consolidate memory (index over cap), Obsidian tooling (CLI/Bases/Markdown/JSON Canvas), vault bloat or Obsidian slow. Answering questions FROM the graph, use graph-query tools; daily journal/coaching/pattern detection, use those skills.'
trigger: /vault-system
---

# Vault system

Routes **vault meta-maintenance** — the vault AS a system (its markdown corpus, knowledge graph, drift monitors, rule extraction, substrate sync). Not the content; the substrate that makes the content queryable and auditable.

**Distinct from:**
- Querying the vault to answer a question (use the knowledge-graph query tools)
- Bringing external data INTO the vault (use the ingest skills)
- Daily journal + coaching + pattern detection (those operate inside the substrate)

This is the SUBSTRATE layer. It maintains what every other skill uses.

## Step 1. Identify the maintenance moment

| Trigger | Skill / tool |
|---|---|
| Knowledge graph stale or never built for a corpus | `graphify` |
| Existing graph query (find concept / path / cluster) | your graph-query tool, or Obsidian graph view |
| Typed-file metadata extraction (books, meetings, people, articles, goals) | `second-brain-mapping` |
| Vault self-check (CLAUDE.md / Meta folder / skills / hooks / MCPs / journal index) | `diagnose` |
| Files edited many times in a short window (drift hotspot) | a drift-detection pass → a Drift Audit note |
| Recurring corruption pattern needs codification | codify it into a new rule file in your rules dir |
| Journals missing body / frontmatter context | `backfill-journal-body-context` |
| Vault types not yet set up (new vault or refresh) | `setup-vault-types` |
| Memory index getting long (truncation cliff) | `consolidate-memory` |
| Obsidian Bases / JSON Canvas / Markdown / CLI work | the matching `obsidian:*` skill |

If unclear → ask ONE question. Don't guess.

## Step 2. Pre-flight

1. **Vault location:** resolve the vault root (a `VAULT_PATH` / `VAULT_ROOT` env var, or the current directory).
2. **Graph state:** check whether a graph report exists and how fresh it is before answering graph questions.
3. **Drift state:** check the latest drift-audit output.
4. **Rules state:** list the rules directory.
5. **Memory state:** check the memory index length (keep it under the truncation cliff).
6. **Git in a large vault:** NEVER `git add -A` / `.` / unscoped `git status` on a big vault — full-tree walks are slow and lock-contending. Use explicit paths.

## Step 3. Route by work-type

### Knowledge graph
- Build a new graph from a corpus (code / docs / papers / images) → `graphify`
- Query an existing graph (search nodes / neighbors / path) → your graph-query tool
- Coverage audit (which corpus chunks were processed) → graphify's coverage audit

### Second-brain mapping
- Extract structured metadata from every typed file → `second-brain-mapping`
- Apply wikilinks from detected entities → `second-brain-mapping` (wikilink phase)
- Cross-type insights (patterns no single file shows) → `second-brain-mapping` (insights phase)

### Diagnostics + drift
- Vault self-check → `diagnose`
- Drift detection (files edited many times in 30d) → a drift-detection pass → a Drift Audit note

### Rule extraction + memory
- Extract recurring patterns into a CLAUDE.md rule / rules file → codify manually into a new rule file
- Consolidate memory files (merge duplicates, prune the index) → `consolidate-memory`

### Journal hygiene
- Backfill journal body / frontmatter context → `backfill-journal-body-context`
- Tag / taxonomy consistency check → manual against your canonical tag list

### Vault setup + onboarding
- Setup vault types (frontmatter taxonomy) → `setup-vault-types`
- Setup the substrate on a new vault → the bootstrap flow

### Obsidian tooling
- CLI (backlinks, search, unresolved, orphans, properties) → `obsidian:obsidian-cli`
- Bases queries (typed views) → `obsidian:obsidian-bases`
- JSON Canvas creation / edit → `obsidian:json-canvas`
- Markdown specifics (callouts, wikilinks, embeds) → `obsidian:obsidian-markdown`

## Step 4. Post-flight (gates "done")

1. Graph rebuilt + report regenerated if graphify ran.
2. Memory index under the truncation cliff post-consolidation.
3. Drift hotspots resolved if the Drift Audit surfaced any.
4. New rules indexed in the rules directory.
5. Vault committed with explicit paths (never raw `git add -A` in a large vault).

## Common mistakes

- **`git add -A` in a large vault.** Long walks + lock contention. Use explicit paths.
- **Editing aggregator outputs directly** (e.g. a "Last Session" or "Decision Log" roll-up). Edit the per-entry source files; let the aggregator rebuild.
- **Hand-editing symlinked skill copies.** Edit the source repo + re-sync.
- **Re-reading the memory index when it is already in context.**
- **Routing daily journal / coaching / pattern detection here.** Those are their own skills; this umbrella maintains the substrate, not the content.
