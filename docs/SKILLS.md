# Skills Catalog

The first-party skills that ship with AI Brain Starter. Every one is a markdown file you can read in a few minutes; install puts each where Claude Code can find and trigger it. Most are invoked with a slash command (`/journal`, `/graphify`, `/coach`); a few auto-fire on the right cue.

This is the first-party catalog — the skills built for the substrate itself. For the third-party skills, MCP servers, and Obsidian plugins also bundled at install, see [`POWER_TOOLS.md`](POWER_TOOLS.md). To see what a skill's output actually looks like, [`EXAMPLES.md`](../EXAMPLES.md) shows a real journal entry and weekly insight report.

---

## Daily practice

The everyday rituals — the surface you touch most.

- **rise** — morning consciousness routine. Identifies your emotional Floor from a natural check-in, surfaces the day's top one to three priorities, sets an intention.
- **daily-journal** — daily journal interview with a live advisory panel that meets your draft and pushes back where the thinking is soft.
- **coaching** — multi-pass coaching session for a hard conversation, a decision you are second-guessing, or accumulated tension that will not fit in a daily entry.
- **insights** — weekly and monthly journal insight reports: Floor trends, avoidance flags, wins, life-coach and therapist observations.
- **sunday-review** — weekly meta-review that orchestrates the weekly insight, pattern scan, vault hygiene, backup restore drill, and decision retrospective in one flow.
- **patterns** — the Instinct Engine. Scans recent sessions, journals, and decisions for recurring patterns and turns them into concrete captures.
- **deconstruct** — first-principles analyst. Surfaces hidden assumptions, finds the foundational truths, rebuilds from scratch, names the high-leverage move.

## Health and longevity

- **coach** — longevity and fitness coach. Issues a daily workout prescription that reads recovery, sleep, and cycle phase from your wearable, paired with today's Floor.
- **health-setup** — interactive setup wizard for the health connector. Picks the right wearable (Apple Watch, Oura, Fitbit, Garmin, Whoop) and walks token setup end to end.
- **health-context** — auto-fires when the journaling, coaching, panel, or insight skills run, pulling the day's health context into them.
- **health-doctor** — observability surface for the health auto-trigger chain. Shows data-source freshness, the last prescription, missed days.
- **ingest-health** — imports Apple Health data into the local database the health skills read from.
- **longitudinal** — multi-year health pattern surface. Scans years of data and returns only the strongest correlations.
- **backfill-journal-body-context** — walks past journal entries and appends a body-data section, pairing each day with its health metrics.

## Knowledge graph and vault structure

- **graphify** — turns any input (code, docs, papers, images) into a knowledge graph with clustered communities, an HTML view, and an audit report.
- **second-brain-mapping** — unified vault-mapping pipeline. Extracts structured metadata from every typed file and surfaces cross-document insights.
- **setup-vault-types** — interactive wizard to configure which document types your vault uses and scaffold extractors for custom ones.
- **resolver-query** — reads your rule resolver and answers a natural-language question by surfacing the matching rule.
- **diagnose** — self-check against an installed vault. Verifies CLAUDE.md, the Meta folder, skills, hooks, and the journal index; prints a green/yellow/red report.

## Capture and to-dos

- **meeting-todos** — extracts action items from a meeting note and files them, separating your tasks from everyone else's.
- **note-todos** — the same extraction for any non-meeting note: class notes, book notes, podcast notes, transcripts, panel writeups.
- **cierre-de-llamada** — a SALES or client call just ended: turns its transcript into the whole follow-up (CRM summary, deal state, tickable tasks, a drafted follow-up email, the reminder, a note to the sales lead, and what worked / what did not). Invents nothing, and never sends anything to the client.

## Ingestion — pull external sources into the vault

- **ingest-github** — recent merged PRs, issues, and commits from a repository.
- **ingest-youtube** — a YouTube video transcript, or a channel's recent uploads.
- **ingest-health** is listed above under Health and longevity.

Each writes queryable markdown into the vault and is idempotent: re-running on the same day overwrites cleanly.

## Writing and content

- **repurpose-talk** — turns one speaking engagement into 10 to 30 content pieces: LinkedIn posts, Substack notes, a video-clip plan.
- **nano-banana** — image generation, editing, and composition via Google's Gemini 3 Pro Image model.

## Coaching and interviews

- **interview-me** — simulates a panel interview or podcast, then gives you a structured debrief.

## Self-improvement and instincts

- **evolve** — clusters hardened instincts and proposes a new skill scaffold from observed patterns.
- **instinct-export** — exports the Instinct Engine's learned patterns to a portable file.
- **instinct-import** — imports a portable instinct pack into a fresh vault.
- **doubt-driven-development** — adversarial self-reviewer. Applies structured doubt to a plan or decision before committing.

## Vault safety

- **secret-warn** — scans the vault and flags files that look like they contain secrets or credentials.

## Teams

- **for-my-team** — walks you through what changes when a personal vault becomes a team vault.

---

Thirty-plus skills covering the daily practice, health, knowledge graph, capture, writing, and team surfaces. Install registers each so Claude Code triggers it on the right cue; you do not memorize a command list. New to the system? Start with **rise** in the morning and **daily-journal** at night — the rest reveals itself as the work calls for it.
