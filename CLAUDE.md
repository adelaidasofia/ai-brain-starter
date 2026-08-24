# ai-brain-starter — Claude Instructions

## Public repo rules — NEVER HARDCODE PERSONAL CONTEXT

This is a **public repo** that strangers fork to build their own vaults. The whole point is that it must work for people who are NOT the maintainer and are NOT on the maintainer's team.

**Before writing or editing ANY file in this repo, scan for and remove:**
- Personal names (the maintainer, their co-founders, their team, their advisors, their family)
- Company names the maintainer is affiliated with
- Personal vault and folder paths — the maintainer's own vault name, their team-folder name, any emoji-prefixed folder specific to one person's structure. **Every folder in a user's vault is named by that user at install time.** Never hardcode one, not even as a fallback: read it from config, detect it structurally, or take it as an argument. A hardcoded folder name is dead code for everyone except the person who wrote it.
- Personal frameworks the maintainer invented (a bespoke rating scale, a private roster of named advisors)
- Specific cities, countries, or geographies that aren't universal
- Substack URLs and personal blog links other than what already exists in the README/SKILL.md welcome message
- Real meeting names, real decision content, real strategic context
- Anecdotes that name real people or real situations

**When you need to illustrate a pattern with an example, invent a fictional one.** Generic placeholders ("a Google Drive shared folder", "a team member", "a co-founder") are always preferred over real names. If a fictional example would be confusing without context, use a clearly-marked one (`# Example only — replace with your own`).

**Existing files that pre-date this rule may have personal references** (the README's Background section names the maintainer as legitimate authorship attribution; the LICENSE carries their copyright; the CHANGELOG narrates the actual history of the repo's development). Don't touch those — they're load-bearing context. The rule applies to **new** content you add.

**The narrative CHANGELOG is the one place narrative context is OK** because it's a historical record of what shipped when. But even there, prefer "the maintainer" or "a team member" over names when possible.

If you catch yourself writing "this is what [name] does," stop. Rewrite it as a generic pattern. The repo is for strangers, not for the maintainer's team.

## Git in large Obsidian vaults (users' vaults, not this repo)

Many users of this skill will have their Obsidian vault under git for local snapshots (the Phase 5 setup suggests it). Obsidian vaults commonly grow to 10,000+ files between journal entries, attachments, book notes, and plugin caches (`.smart-env/`, `.obsidian/workspace*`, etc.). Walking that full tree with `git add -A` or unscoped `git status` takes minutes of CPU, locks `.git/index.lock`, and wastes a lot of assistant context polling for progress.

**Rules when operating in a user's vault:**
1. Never run `git add -A`, `git add .`, or unscoped `git status`. Always pass explicit file paths you intend to stage.
2. If a vault has no git remote (`git remote -v` empty), never attempt `git push`. Vaults are commonly local-only snapshot repos.
3. When `git` is slow, check `wc -l <(git ls-files)` — if >10k files, you are in a vault repo; switch to targeted paths immediately.
4. The session-close template (`templates/rules/session-close.md` or the user's equivalent) must include the "targeted-paths only" pattern for any git snapshot step. If a new setup phase is introducing git automation, ship the rule with it.

These rules ship to every user's vault CLAUDE.md via Phase 4 / `templates/generated/claude-md-template.md`. Keep them generic (no personal paths) per the public-repo rule at the top of this file.

## Repo structure rules

**Before creating any new file or folder, always check what already exists:**

1. Run `ls` on the root to see top-level structure
2. Check `SKILL.md` (thin orchestrator) and `phases/` (one file per phase group)
3. Check `templates/generated/` for embedded templates extracted from phases

**The pattern:** `SKILL.md` is a thin router that tells Claude which phase file to read next. Each phase file lives in `phases/` and is under 10k tokens (one Read call). Large embedded templates (CLAUDE.md template, insights skill, obsidian rules, team-weekly skill) live in `templates/generated/` and are referenced by pointer from the phase files. Adding a new phase means creating a file in `phases/` and adding a row to the routing table in `SKILL.md`.

## Setup phase files (phases/)
- `phase-00-install.md` — install efficiency tools (brew, python, node, graphify, MCPs)
- `phase-01-welcome.md` — language detection, mode detection, welcome interview
- `phase-02-03-plugins-folders.md` — Obsidian plugins + folder structure
- `phase-04-claude-md.md` — build user's CLAUDE.md
- `phase-05-context-layer.md` — context notes, hooks, aggregator scripts
- `phase-06-09-tools-templates.md` — tool routing, imports, templates, skill verification
- `phase-10a-journaling.md` — daily journaling setup (interview, floor framework, skill)
- `phase-10b-panel-roster.md` — advisory panel roster + voice routing
- `phase-11-external-tools.md` — email/calendar/Slack/CRM, meeting tool wiring
- `phase-12-17-imports-rules.md` — book notes, health, taxonomy, backup, obsidian rules
- `phase-18-insights.md` — weekly/monthly insights + cron
- `phase-19-23-finish.md` — test drive, team vault, patterns, theme, second-brain-mapping install (23.5), first-week handoff with recommended uses (24)

## Generated templates (templates/generated/)
- `claude-md-template.md` — CLAUDE.md skeleton used by Phase 4
- `obsidian-rules-template.md` — Obsidian + efficiency rules appended by Phase 16
- `insights-skill-template.md` — insights SKILL.md generated by Phase 18
- `team-weekly-skill-template.md` — team-weekly SKILL.md generated by Phase 20

## Current standalone sub-skills (skills/)
- `skills/meeting-todos/SKILL.md` — extract action items from meeting notes
- `skills/patterns/SKILL.md` — Instinct Engine for pattern extraction
- `skills/graphify/SKILL.md` (+ scripts + RUNBOOK.md) — knowledge graph builder
- `skills/insights/SKILL.md` — weekly/monthly journal insights
- `skills/deconstruct/SKILL.md` — first-principles analysis
- `skills/daily-journal/SKILL.md` — daily journal interview
- `skills/repurpose-talk/SKILL.md` — turn talks into content pieces
- `skills/nano-banana/SKILL.md` — image generation via Gemini

## Reference docs (for users, not embedded in SKILL.md)
- `docs/POWER_TOOLS.md` — catalog of every third-party skill, MCP server, Obsidian plugin this setup uses, with attribution and install commands
- `docs/MEMORY_SYSTEM.md` — typed-memory pattern for persistent cross-session knowledge
- `templates/dataview-queries.md` — reusable Dataview query library
- `templates/Decision Log.md` — template for tracking decisions over time
- `scripts/build-journal-index.py` — index builder for `/insights`-style skills
- `skills/graphify/RUNBOOK.md` — production playbook for big graphify runs
