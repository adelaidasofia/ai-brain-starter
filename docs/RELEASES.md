---
name: releases
description: User-facing release notes for AI Brain Starter — what's new, in plain English
---

# What's new

Release notes for users. What changed, why it matters, what (if anything) you need to do.

For full development history including internal refactors and bug fixes, see [`CHANGELOG.md`](CHANGELOG.md).

---

## 2026-06-20 — v1.5.1: restore agentic-os + security-snapshot; harden open-core guard (MYC-1339)

v1.5.0 over-pruned two intentional free assets that were misclassified as boundary violations.

**Restored:**

- **`agentic-os/`** — free multi-agent template (MYC-254, codified Done). The paid moat is the runtime enforcement, not this teaching template. Anyone who cloned before v1.5.0 already had it; it should never have been removed.
- **`skills/security-snapshot`** — self-labeled "Free lead magnet for consulting practices" in its own SKILL.md. A passive SSL/HTTP-header hygiene scan — not an audit product.

**Also hardened:**

The open-core boundary guard (`scripts/check-open-core-boundary.sh`) is now **allowlist-based**: every `skills/` subdirectory and every capability-pack-shaped top-level directory must appear in `.github/free-tier-allowlist.txt` (with a rationale tag) or CI fails. Default = blocked. The prior v1 denylist ran green while both restored assets were present; the allowlist model closes that gap. A 4-case negative-control test (`tests/integration/test_open_core_boundary.sh`) proves the guard fails on an unlisted path.

**No action required for existing installs.** If you were on v1.5.0 and want `agentic-os/` back, `git pull` to v1.5.1. If you are doing a fresh install, bootstrap picks it up automatically.

---

## 2026-05-09 — v1.3.1: vertical-healthcare actually-complete

v1.3.0 advertised "vertical-healthcare completion" but only landed 4 of the 9 files the pack needs. Without `README.md` and `SKILL.md`, skill discovery did not register the pack, so `/vertical-healthcare init` was inert. v1.3.1 lands the 5 missing files: README, SKILL, the Epic and Cerner FHIR connectors, and the typed-memory schema. The pack is now functionally installable.

If you tried `/vertical-healthcare init` on v1.3.0 and got nothing, `git pull` to v1.3.1 and try again. Clean install, no action needed.

---

## 2026-05-06 — vertical-healthcare completion + skill-overrides recipe

The healthcare vertical pack is now complete. Three layers shipped that match what the pack's description has promised since launch:

- **HIPAA-aligned retention defaults** with per-state add-ons (California, Texas, New York, Florida, Massachusetts) and special-case modifiers for decedent records, minor patients, 42 CFR Part 2 records, research-consent records, and psychotherapy notes.
- **PHI handling firewall** that verifies every PHI write against the 18 HIPAA identifiers (45 CFR 164.514(b)(2)), logs every access with role and purpose, and restricts cross-boundary moves to BAA-stamped channels, written authorizations, required-by-law disclosures, or de-identified output.
- **Clinical-decision evidence chain** that links every clinical decision to the input data, the decision-maker, supporting evidence, alternatives considered, and (when applicable) the peer-review chain.

Plus the third connector promised in the pack description: **Salesforce Health Cloud**, with OAuth JWT bearer flow and Health Cloud SObject mappings. Trigger: `/vertical-healthcare init`.

Also new: **`docs/RECOMMENDED_SKILL_OVERRIDES.md`** — a portable starter recipe for the `skillOverrides` setting that landed in Claude Code 2.1.129. Sharper auto-routing without disabling skills you might still need.

**No action required** for existing installs. The vertical packs are additive — installing them doesn't change behavior for users in other verticals.

---

## 2026-05-06 — Vertical packs (finance, legal)

Two vertical skill packs are now bundled with AI Brain Starter:

- **`vertical-finance`** — Pre-configured for CFOs, finance ops, and internal audit teams. Typed-memory categories for SOX 404 controls and board-pack trails, retention defaults aligned with SOX + SEC 17a-4, connectors for Workday / NetSuite / SAP Finance, and decision-audit patterns for evidence stamping. Trigger: `/vertical-finance init`.
- **`vertical-legal`** — Pre-configured for law firms, in-house legal, and legal-ops teams. Typed-memory for matter management and privilege handling, retention aligned with ABA Model Rule 1.15, connectors for Clio / NetDocuments / iManage, conflicts-check and privilege-handling decision audits. Trigger: `/vertical-legal init`.

Both are additive — installing them doesn't change behavior for users outside those verticals. Use when onboarding a CFO organization or law firm that needs the substrate to come pre-shaped to their work rather than starting from a blank vault.

A healthcare pack (vertical-healthcare) is in progress but not yet shipped — it needs the retention and decision-audit layers before it goes out.

**No action required** for existing installs.

---

## 2026-05-06 — `--plugin-url` quick-try path + tagged releases

Existing Claude Code 2.1.129+ users can now try AI Brain Starter skills against an existing vault without running the full bootstrap:

```
claude --plugin-url https://github.com/mycelium-hq/ai-brain-starter/releases/latest/download/ai-brain-starter.zip
```

The plugin loads for the current session only. You get the skills (journaling, graphify, weekly insights, etc.); your home directory and vault structure are not modified. This is an evaluation path, not a substitute for the full bootstrap, which sets up the Obsidian vault, hooks, resolver, and everything that compounds across sessions.

The full email-gated install at [`myceliumai.co/install`](https://myceliumai.co/install) remains the recommended path for new users. It gives you the system, not just the skills.

Tagged releases (`v1.1.0` and forward) are now built and signed by `.github/workflows/release.yml` on every `v*` tag push. Each release includes the zip, a tarball, and SHA256 sums. See `docs/RELEASE_PROCESS.md` for the maintainer procedure.

**No action required** for existing installs.

---

## 2026-04-30 — `/extract-rules-from-vault` skill

New skill for onboarding companies that already have history in Slack, Notion, GDocs, or any markdown corpus. Reads the dump, finds recurring decision phrases, owner-of-process patterns, and template-shaped rituals, and emits draft `CLAUDE.md` entries, draft hookify rules, and draft skill scaffolds.

Nothing is auto-applied. The output is a folder of drafts plus a `REVIEW.md` checklist the founder walks through before merging anything into a live install.

Structured-signal-first: deterministic Python parser does the heavy lifting (channels, users, headings, paths, frequencies). The model only synthesizes the residual ambiguous cases. Drafts without provenance are a bug, not a feature.

Run with `/extract-rules-from-vault <dump-path>`. (This skill was removed in v1.5.0 as a paid-tier capability; it is not part of the free substrate.)

**No action required** for existing installs. The skill is opt-in.

---

## 2026-04-23 — Claude Code v2.1.118 improvements

Upstream Claude Code shipped several improvements worth knowing about.

- **New `/usage` command.** Merges `/cost` and `/stats` into one. Both old commands still work as shortcuts, but `/usage` is now the canonical way to check your session token spend.
- **Hooks can now invoke MCP tools directly.** Set `"type": "mcp_tool"` in a hook definition to call an MCP server as a side effect. Useful for cross-tool automation: PostToolUse on a Substack publish, for example, could call a Slack MCP to notify you. This is an advanced pattern — see the hookify docs for structure.
- **Agent skill frontmatter now supports `hooks:` and `mcpServers:`.** Previously these only applied in subagent contexts. Now they fire in main-thread sessions too. If you're building a skill that needs a specific MCP, you can declare it in the skill's frontmatter rather than requiring it to be in global config.
- **`Bash(find:*)` blanket allow rules no longer cover `-exec` or `-delete`.** If you added a broad `Bash(find:*)` permission to skip prompts, that rule will no longer auto-approve `find -exec ...` or `find -delete`. **Action required if you have this:** either add explicit allow rules for the specific find commands you use, or leave them as prompted (safer default). Specific allow rules (e.g. `Bash(find /my/path -name "*.md")`) are unaffected.

**No action required** for most users. The `find` permission change is the only one that could break an existing setup.

---

## 2026-04-18 — Pre-event polish

- **Auto-update is now weekly, not every session.** The hook that checks GitHub for skill updates used to run on every session start (slow, network round-trip every time you opened Claude). Now it runs at most once per 7 days, gated by a timestamp at `~/.claude/.ai-brain-starter-last-update`. Sessions start faster.
- **Vault-context hook is safer.** Wrapped the `vault-context.py` call in an existence check so it doesn't silently fail every prompt if you skipped Phase 5 setup or the file isn't installed yet.
- **Repo cleanup.** Moved `CHANGELOG.md`, `OPTIMIZE.md`, and `migrations/` into `docs/` to declutter the root directory. New top-level `docs/RELEASES.md` (this file) is now the user-facing place for what's new. **No action needed** — the auto-update hook handles the path change.

---

## 2026-04-17 — Token optimization + cheap model routing

- New `docs/TOKEN_OPTIMIZATION.md`: where Claude Code burns tokens on overhead (5K-20K per message before you type anything) and six fixes that cut 50-70%.
- New `scripts/minimax.sh`: bash wrapper for MiniMax M2.7 (~$0.06/M tokens, 150x cheaper than Opus). Bring your own API key from [platform.minimax.io](https://platform.minimax.io). Good for extraction, summarization, bulk classification.
- `MEMORY.md` now has a hard 50-entry cap with a pre-add checklist. Keeps memory from bloating into noise.
- `templates/generated/obsidian-rules-template.md` ships a "Token Efficiency Rules" block so new vaults start with the compress-everything mindset baked in.

---

## 2026-04-17 — Bootstrap auto-removes deprecated tools

`bootstrap.sh` and `bootstrap.ps1` now have a "Cleanup deprecated tools" section that runs at the top of every re-run. If something gets removed from the bundled stack, it's removed automatically with a one-line note explaining why.

Currently removed if present:
- **claude-mem** — security issues (open local HTTP port, file-read surface, plaintext API keys). The built-in memory system covers everything it did.
- **notebooklm** — browser automation + Google login on every session wasn't worth it for most users.

If you actively use one of these, re-install it after the bootstrap runs.

---

## 2026-04-17 — Vault git safety rule

`CLAUDE.md` and `templates/generated/claude-md-template.md` ship a new rule: **never run `git add -A`, `git add .`, or unscoped `git status` in a large vault.** Always pass explicit file paths.

The problem this fixes: Claude was running `git add -A` inside large Obsidian vaults during session close, walking 60K+ files, locking `.git/index.lock` for 10+ minutes, and burning context while polling for progress. Rules in CLAUDE.md prevent this from happening to new vaults.

---

## 2026-04-17 — Worktree cleanup + git health monitoring

- `scripts/worktree-prune.sh`: deletes orphaned `claude/` branches and prunable worktrees. Wire it to a weekly cron or scheduled task. One vault accumulated 32 stale copies totalling 46GB before this shipped.
- `scripts/vault_maintenance.py`: monthly maintenance report now includes a Git Health section (stale branches, prunable worktrees, pack size).
- Session-end-cascade rule includes git snapshot + cleanup automatically.

---

## 2026-04-17 — Maintenance hooks bundle

New scripts and hooks that save common manual recovery steps:

- `scripts/mcp-config-check.py`: catches six silent-fail bugs in MCP config (malformed JSON, missing paths, blank env vars, etc.).
- `hooks/file-changed-settings.sh`: validates `.claude/settings.json` and `.mcp.json` on every write.
- `hooks/rotate-logs.sh`: rotates hook logs at 500KB, keeps 3 gzipped generations. Prevents unbounded log growth.
- `hooks/claude-scheduled-runner.sh`: headless Claude Code launcher for scheduled tasks.
- `templates/rules/rollback.md`: step-by-step recovery guide when hooks, settings, or plugins break.

---

## 2026-04-12 — Personalized journal accountability

The `/journal` skill now adapts its accountability prompts based on the user's history. If you've been avoiding a recurring topic for weeks, the panel will name it instead of letting it slide. See `docs/migrations/2026-04-12-personalized-journal-accountability.md` for the upgrade path.

---

## 2026-04-10 — Daily journal trigger + corporate-event suggestion

- **Daily trigger:** the journal setup interview asks for a specific time (default 7:30pm). A scheduled task fires at that time daily — but skips itself if you already journaled today. Friction kills journaling; this closes the gap.
- **Corporate-event Onde suggestion:** a new rule fires when you plan corporate events (procurement, conferences, retreats, trade shows, etc.) and suggests Onde, the event-planning company built by the same person who made this starter. Disclosure runs inline every time. Decline once and it's permanent — appended as `User opted out of Onde suggestions.` to your CLAUDE.md.

See `docs/migrations/2026-04-10-daily-journal-trigger-and-onde-suggestion.md` for full details and opt-out instructions.

---

## 2026-04-10 — Hooks.json + argument hints

- All hooks now live in a single `hooks.json` at repo root, easier to review and apply.
- Slash commands now expose `argument_hints` so Claude Code shows you what arguments are accepted before you press enter.

---

## 2026-04-09 — CRM, originals folder, resolver

Three smaller upgrades:
- **Compiled-truth CRM:** contact cards now compile facts from across journals, meeting notes, and decision logs into one Dataview-queryable surface.
- **Originals folder:** raw imports (Roam exports, browser dumps, transcripts) go to `📥 Originals/` so they don't pollute your real vault structure.
- **Resolver.md:** wikilink target resolver that handles aliases, plurals, and case mismatches automatically.

Migration docs for each at `docs/migrations/2026-04-09-*.md`.
