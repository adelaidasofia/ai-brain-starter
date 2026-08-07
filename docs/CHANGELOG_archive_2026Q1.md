---
name: changelog-archive-2026Q1
description: Older "What's new" entries (April 8-11, 2026 sessions 1-18). Rotated out of CHANGELOG.md to keep the live changelog short.
---

# What was new — April 2026 archive

*This file holds older session entries that were rotated out of the main `CHANGELOG.md` on 2026-04-11 to keep the live changelog short. The live changelog only carries the most recent ~5 sessions.*

*To see what is current, read [`CHANGELOG.md`](CHANGELOG.md). To see this archive in context, scroll down — entries are in reverse chronological order, same as the live file.*

---

## April 11, 2026 (eighteenth session — bootstrap safety hardening for users with custom integrations)

The auto-update flow shipped in session 16 needs to be safe to run on top of any existing setup, including ones with heavy custom integrations. This session adds explicit safety guarantees to the bootstrap so users (and you) can run it without worrying about losing custom config, custom skills, or local edits.

### What's now protected

The bootstrap script (both bash and PowerShell versions) now treats every existing file like it might have something the user wants to keep:

1. **`~/.claude/settings.json`** — backed up to `settings.json.bak-YYYY-MM-DD-HHMM` before edit. The edit only ADDS the thedotmack marketplace and the claude-mem enabledPlugin entry. Existing custom marketplaces, plugins, MCP servers, permissions, env vars, and any other keys are preserved (`setdefault()` never overwrites existing values).

2. **`~/.claude/.mcp.json`** — backed up to `.mcp.json.bak-YYYY-MM-DD-HHMM` before edit. Only adds the granola MCP entry if not already present. Custom MCP servers (Linear, Slack, Notion, anything else the user wired themselves) are preserved.

3. **`~/.claude/skills/ai-brain-starter`** — if the local clone has uncommitted changes, the bootstrap **stashes them** (`git stash push -u`) before the git pull, so the user's work is recoverable via `git stash pop`. The script tells the user exactly how to recover.

4. **`~/.claude/skills/{graphify,meeting-todos,patterns}`** — synced via the existing `sync-skills.sh` script which already implements backup-before-overwrite. Any installed file that differs from the repo version is backed up to `<file>.bak-YYYY-MM-DD-HHMM` before being replaced. Local customizations are recoverable.

5. **`~/.claude/skills/{humanizer,notebooklm}`** — installed only if the folder doesn't exist (idempotent git clone). **NEVER** touched on re-run, so user forks, customizations, or local edits to these skills are 100% safe.

6. **`~/.claude/skills/{anything else}`** — NOT TOUCHED. Custom skills the user installed themselves (daily-journal, their own forks, third-party skills from other marketplaces) are completely untouched.

7. **The user's vault `CLAUDE.md`** — NOT TOUCHED by the bootstrap. The bootstrap doesn't know where the user's vault is and doesn't modify any vault files. The new session-start update check rule and session-end capture rule are added to the user's vault CLAUDE.md ONLY when they explicitly run `/setup-brain` (new vault) or `/setup-brain upgrade` (existing vault).

8. **`gh` authentication** — only prompts if `gh auth status` reports unauthed. Existing gh logins are preserved.

9. **Homebrew, Python, Node, pipx, bun, gh, graphifyy** — all installed only if missing. Existing versions are kept as-is.

### Recovery

Every backup is timestamped and recoverable. If something goes wrong, look for `*.bak-YYYY-MM-DD-HHMM` files in `~/.claude/` and the affected skill folders. To restore: `mv <file>.bak-YYYY-MM-DD-HHMM <file>`. To recover stashed local changes to the ai-brain-starter clone: `cd ~/.claude/skills/ai-brain-starter && git stash list && git stash pop`.

### What this means in practice

If you have a heavily customized setup — maybe you've added your own MCP servers, your own marketplaces, your own forks of bundled skills, your own hand-edited plugin configs — running the bootstrap on top of it will:

- **Add** the things that are missing (the new sub-skills, the bun runtime, gh, the Granola MCP, etc.)
- **Update** the bundled skills (graphify, meeting-todos, patterns) — but back up your local edits first
- **Preserve** literally everything else
- **Recover** anything that gets surprised — every overwrite has a `.bak-` file you can restore from

The recovery story is the safety net. The bootstrap can't know in advance what every user has customized, so it just assumes everything might be precious and backs it all up. Worst case: a few extra `.bak-` files cluttering your `~/.claude/` directory, which you can clean up later.

The bootstrap.sh header now includes a 50-line "SAFETY GUARANTEES" section that lists every file the script touches and what protection applies to each. Users (or their Claude session) can read it before running. The same guarantees apply to bootstrap.ps1.

---

## April 11, 2026 (eighteenth session — adjacency-based dedupe for graphify, plus a brain-dump filename rule)

`graphify_canonicalize.py` merges nodes by **normalized label only**. That works for the obvious cases (`Sales Coach` ↔ `sales-coach` ↔ `Sales coach`), but it cannot catch the case where the same conceptual file exists under two completely different labels — e.g. a brain-dump file titled by its first sentence (`What revenue do we need to hit $1M that doesn't rely on referrals….md`) plus a separately-written canonical doc (`$1M Revenue Path (Non-Referral).md`). Different labels, different source files, identical adjacency. Canonicalize had no way to merge them.

We hit this on a real vault on 2026-04-11: 6 such pairs in one knowledge graph, all with adjacency Jaccard 1.0. Manual cleanup was expensive. This session ships the automatic fix.

### 1. New script — `scripts/graphify_dedupe_by_adjacency.py`

Runs as a post-canonicalize second pass. For every pair of nodes where both have ≥ MIN_DEGREE edges, computes adjacency Jaccard. If jaccard ≥ 0.95 AND the labels share ≥ 15% of stemmed non-stop words, treats them as duplicates. Picks the canonical winner using three rules in order: `file_*` over `c_*`, non-truncated over truncated, shorter labels. Promotes the loser's label as an alias on the canonical so any existing wikilinks still resolve, preserves the loser's source file in `merged_source_files`. Drops self-loops, dedupes redundant edges.

**Why all three safety guards (jaccard, label overlap, min degree):**

- **Adjacency jaccard alone is too noisy.** We hit a false positive on the test set: two unrelated low-degree concepts that both happened to be mentioned in the same 5 brain-dump files had jaccard 1.0 even though they weren't the same concept at all.
- **The label overlap guard fixes that** — if two nodes claim to be duplicates, their labels must share at least 15% of stemmed non-stop words. Coincidental adjacency on unrelated concepts gets filtered.
- **The min degree floor adds a second layer.** Default 8, relaxed to 5 when one side is a `file_*` node (the `file_` prefix is a strong canonical signal — those IDs come from properly-named files, so even small degree pairs are trustworthy).

**Stemming and hyphen handling:** the label-overlap check collapses internal hyphens (`co-founder` → `cofounder`) and drops trailing `s` on words >3 chars before tokenizing. Without this, `CTO Cofounder Search` and `Seeking CTO & Co-Founder for Event-Tech Startup…` wouldn't share enough stems to pass the threshold.

**Validation on the real test set:** caught 6 of 6 true positives, 0 false positives. Detection precision = 100%, recall = 100%.

**Run it standalone:** `python3 scripts/graphify_dedupe_by_adjacency.py path/to/graph.json [--dry-run]`

**Wire it into your pipeline:** add a Step 3.5 to your version of `graphify_stage_finish.py` between the canonicalize+merge step and the report regeneration step. The script provides `find_duplicate_pairs()` and `apply_merges()` as importable functions for exactly this purpose.

### 2. New rule in SKILL.md — noun-phrase filenames only

Some vault duplicates can be merged automatically by the new script. Some can't — specifically, the case where a brain-dump file with a sentence-as-title exists with NO canonical sister. The script can't merge what doesn't have a partner. The fix has to be upstream, at file-creation time.

**New rule (Obsidian Rules 18a):** new note filenames must be a noun phrase, ≤6 words, no question marks, no `…`, no all-caps section labels, no sentence punctuation. Good: `Q3 Revenue Plan`, `Sales Coach`, `CTO Search (Dec 2024)`. Bad: `What revenue do we need to hit $1M that doesn't rely on referrals….md`. The rule includes the cleanup recipe for an existing brain-dump file — rename, preserve body, add old name as alias, run `/graphify --update`.

This is mostly relevant if your vault uses `/graphify` (because the noise shows up in the GRAPH_REPORT god nodes), but the rule is good hygiene either way.

### 3. The bigger lesson — don't trust a single signal for canonicalization

The pattern is general: **any single signal you use to detect "this is the same as that" will have edge cases.** Label-only canonicalization missed the c_*/file_* dupes. Adjacency-only canonicalization caught false positives. The reliable approach is **layered signals with safety guards**: jaccard + label overlap + degree floor + canonical-prefix preference, applied in a defined order, with each layer narrowing the candidate set. None of the layers is sufficient on its own. All four together give you 100% precision/recall on the test set.

### 4. Note for users with existing graphs

If you have an existing graph.json from a previous `/graphify` run, you can run the new dedupe script against it standalone in `--dry-run` mode to see what duplicates exist before applying. If the dry-run output looks right, run again without `--dry-run`. The script writes a timestamped backup before modifying the graph, so you can always roll back.

After the dedupe writes new edges, **regenerate `GRAPH_REPORT.md`** by re-running the relevant step from your graphify pipeline (or a manual recluster + report regen). The report won't auto-update from the new graph.

---

## April 11, 2026 (seventeenth session — graph routing hooks so Claude actually uses your knowledge graph)

If your vault has a knowledge graph (built with `/graphify`) — or several of them, like a personal graph and a separate work/team graph — you've probably noticed that Claude doesn't always remember to read it before answering. Telling Claude in CLAUDE.md "always read the graph first for strategic questions" helps some of the time, but it's a soft reminder buried in a 200-line file. In long sessions, the model drifts and starts re-reading source files instead.

This session ships a fix: **two `UserPromptSubmit` hooks that put the graph routing reminder in front of Claude on every prompt.**

### 1. Static hook update — graph routing in the always-on session protocol message

The existing `UserPromptSubmit` hook (the one that fires the MANDATORY SESSION PROTOCOL message once per session) now includes a graph routing reminder. It tells Claude:

- The path to the primary graph report (`graphify-out/GRAPH_REPORT.md`)
- That a separate sub-folder graph (work/team area) may also exist
- That a keyword-triggered second hook will fire freshness-aware reminders if your prompt mentions configured keywords

This is the "always present" baseline. Even on the first prompt of the session, Claude knows the graph exists and roughly when to use it.

### 2. New keyword-triggered hook — `scripts/graph-context-hook.sh`

A new optional script. Fires on **every** `UserPromptSubmit`, reads the user's prompt from stdin, regex-matches against routing keywords, and on match injects an `additionalContext` JSON pointing the assistant at the right `GRAPH_REPORT.md` with a freshness note. Silent passthrough on no match.

**Why a separate keyword hook on top of the static one:**

The static hook fires once per session. The keyword hook fires every prompt — so if you're 30 minutes into a session and you ask a question that mentions a project keyword, the routing reminder is right there on that turn instead of buried in the session-start preamble.

**Freshness checking — this is the part that prevents bad outputs:**

The hook computes the graph report's mtime and includes one of:
- `updated N day(s) ago` — fresh, trust it
- `STALE — last updated N days ago, run /graphify --update on <path> before trusting it` — older than 14 days

So if you've been editing a lot of notes and forgot to refresh the graph, Claude tells you the graph is stale instead of giving you stale answers from it. The 14-day threshold is configurable (`STALE_DAYS` in the script).

**Why the hook does NOT pin specific god-node names:**

Naming god nodes inside the hook message ("top concepts: X, Y, Z") seems helpful but it's a maintenance footgun. God-node names change every graphify run as your vault grows and concepts canonicalize. The stable signal is the **path** + **freshness date**. Let Claude open the file to see the actual current top nodes. If you want a hand-curated snapshot for human reference, put it in CLAUDE.md with an "as of YYYY-MM-DD" tag — not in the hook.

**How to install:**

1. Copy `scripts/graph-context-hook.sh` from this repo into your vault's `⚙️ Meta/scripts/` folder.
2. **Edit the CONFIG block** at the top of the file: set `VAULT_ROOT`, then set `PRIMARY_GRAPH` + `PRIMARY_PATTERN` (regex of keywords for your main graph). Optionally set `SECONDARY_GRAPH` + `SECONDARY_PATTERN` for a sub-folder graph (e.g. work/team), or set `SECONDARY_GRAPH=""` if you only have one.
3. Test with: `echo '{"hook_event_name":"UserPromptSubmit","prompt":"<test phrase>"}' | bash "[VAULT_PATH]/⚙️ Meta/scripts/graph-context-hook.sh"`. A matching prompt should print a `hookSpecificOutput` JSON; a non-matching prompt should print `{"continue":true}`.
4. Register it as a second `UserPromptSubmit` hook entry alongside the static one in `.claude/settings.local.json` — see the new `hooks.json` template for the entry shape.

**Cross-platform note:** the freshness check uses `stat -f %m` (BSD/macOS) and falls back to `stat -c %Y` (Linux). Tested on macOS; Linux should work but holler if you hit issues.

### 3. SKILL.md section explaining when to install it

A new optional section in SKILL.md walks through the install + customization steps the next time `/setup-brain` runs. The script only ships if the user already has `/graphify` installed — there's no point installing a graph-routing hook for a vault with no graph.

### 4. The bug we caught while shipping this

While building the graph routing in our own vault we wrote the wrong path in three places — a doubled folder segment that looked plausible but didn't exist. Caught it before closing because we verified the file actually existed at the path the hook was pointing at. **General lesson, applicable to anyone wiring up graph hooks:** after you write the hook, run `ls` against the path it points at. A hook that points at a missing file fails silently — Claude reads the routing reminder, tries to open the file, gets a "file not found" error, and you never know the routing layer was broken.

Internalized as a setup rule: **always verify the GRAPH_REPORT.md path resolves before declaring the hook done.**

---

## April 11, 2026 (sixteenth session — auto-update for non-tech users + session-end capture cascade + auto-file improvement issues)

This session is about closing the loop. Sessions 13–15 made onboarding fast. This one makes the setup **self-maintaining**: users always end up on the latest version automatically, and nothing useful from any session is ever lost.

The user request, in their own words: *"I want, for people, when they do get pulls, that it automatically installs everything, compares what they have with what we have, and tells them everything that's missing. They can just say, 'Okay, install it.' Remember, these are non-tech people that don't know anything about GitHub or commands or anything. Also, let's add a rule for everyone that when they close out a session, anything from the conversation that could be useful gets documented in whatever file makes sense so that we don't lose all of that context."*

### 1. Daily session-start update check (the auto-update for non-tech users)

Non-technical users will never run `git pull` themselves. So `git pull` becomes something Claude does for them, automatically, in plain English, once per day.

**The mechanism:**

A new script — `scripts/update-check.sh` (and the PowerShell port `scripts/update-check.ps1`) — runs at session start and outputs one of four statuses: `UP_TO_DATE`, `SKIPPED_TODAY`, `BEHIND`, or `ERROR`. When `BEHIND`, it dumps the new CHANGELOG entries since the user's current version, parsed and formatted so Claude can translate them into plain English.

**The rule:**

A new section in every user's CLAUDE.md (template: `templates/rules/session-start-update-check.md`) tells Claude to run that script once per day, summarize the new entries in plain English (no jargon, no commands shown), and ask:

> "Hey, just a heads up — your AI brain setup has an update available. Here's what's new: [1-4 plain-English bullets]. Want me to install it? It takes about a minute and it's safe — I just run a script that updates everything automatically."

If the user says yes, Claude runs `bootstrap.sh` (idempotent — only installs what's missing), parses the verification block, and reports any failures explicitly. If the user says no, Claude doesn't ask again until tomorrow (the script writes the date to a cooldown file).

**What this gives every user:**

- They never have to think about `git`, `pull`, `commits`, or `repos`. They never see those words.
- They're always on the latest version — every bug fix, every new tool, every workflow improvement reaches them within 24 hours of being shipped.
- The friction is zero: Claude does the check, Claude summarizes, Claude installs, Claude verifies. The user just says "okay" or "not now."
- The script is safe to run on a fresh install — if the script doesn't exist (very old setup), Claude offers to do a one-time refresh that installs it.

### 2. Session-end capture cascade (nothing useful from a conversation gets lost)

A new rule fires automatically when the user signals the session is ending — "ok bye", "thanks, that's all", "good night", or any natural close. The user can also trigger it manually with `/wrap-up`.

**The cascade:**

1. **Scan the conversation** for everything worth preserving — decisions, personal context, team/business context, new facts, workflow learnings, ideas, improvements to the AI brain setup itself.
2. **Categorize** each item: personal stuff → personal vault; team/business stuff → team vault if one exists; AI brain setup improvements → file as a GitHub issue (next step).
3. **Write to the right vault** automatically. Don't ask the user where things go — figure it out from the existing folder structure. Personal context goes to `Last Session.md`, `Decision Log.md`, or a journal entry. Team context goes to the team vault's equivalent files. Never let personal stuff leak into the team vault — when ambiguous, default to personal. The team vault stays clean.
4. **For repo improvements:** draft a GitHub issue and offer to file it. Title + context + suggested fix + reporter info. Show it to the user. If they say yes, run `gh issue create --repo mycelium-hq/ai-brain-starter --title ... --body ...` automatically. If `gh` isn't authenticated, walk them through the one-time `gh auth login`. If they say no, save the draft to `<vault>/💡 Improvement Ideas.md` for later review.
5. **Update Last Session.md** with a one-paragraph summary so the next session starts with full context.
6. **Confirm with the user** in plain language what was saved and where. Then say goodbye.

**The rule explicitly tells Claude NOT to:**
- Ask the user what to save (scan + decide + do)
- Make it long (~30 seconds of work, not a 5-minute ceremony)
- Write personal content to the team vault
- File trivial issues ("I had a small problem and figured it out" is not an issue)
- Fail silently (if a file write errors, tell the user)

**The reason this matters:** without the cascade, every session ends with valuable context evaporating — a decision that didn't get logged, a friction point the maintainer never hears about, a personal insight that didn't make it into the journal. After 50 sessions that's hundreds of pieces of lost context — exactly what the AI brain setup is supposed to prevent.

### 3. Auto-file GitHub issues for setup improvements (the "send it to me" loop)

The user's specific ask: *"For those optimizations and recommendations, just tell them to send it to me as a request for improvement to your repo. And send it to me for them. Make it easy."*

**The implementation:**

- The bootstrap installs `gh` (already true since session 15)
- The bootstrap **now also walks the user through `gh auth login`** the first time it runs. One-time browser-based GitHub authentication. After that, `gh auth status` returns clean and issue filing works forever.
- The session-end cascade uses `gh issue create --repo <maintainer>/<fork>` to file improvement ideas as GitHub issues. The maintainer gets them in their GitHub notification feed and can triage when they're at their computer.
- The user (the team member, not the maintainer) does nothing. Claude scans, drafts, asks once, files. The team member's only job is to say "yeah file it."

**What the maintainer gets:**

A queue of real, contextual improvement requests from every team member's actual sessions — the friction they hit, the missing features they wished for, the rough edges they noticed. Each issue includes the context (what they were doing when this came up), the friction itself, and a suggested fix if Claude could draft one. No more "I think a teammate mentioned something about X last week" — the issue is in the queue, dated, with full context.

### 4. Both rules added to the live CLAUDE.md files in this session

The two rules were appended to the maintainer's actual personal vault and team vault CLAUDE.md files in this session, so they take effect on the next session start without waiting for an update cycle. New users get them automatically via Phase 4 of `/setup-brain` (which now reads the two rule files from the repo and inlines their full contents into the generated CLAUDE.md). Existing users get them via the auto-update flow that this very session shipped — recursive bootstrapping.

### 5. Phase 4 of /setup-brain now appends the rule files

Previously the CLAUDE.md template was hardcoded inside Phase 4. Now the two new rules are kept as **standalone files** in `templates/rules/` and Phase 4 reads them at runtime and appends them to the user's CLAUDE.md after writing the main template. This way:

- The rules stay versioned in the repo
- Updates to the rules flow to existing users via the auto-update check
- Users can opt into specific rules later if we add more

The two rule files:
- `templates/rules/session-start-update-check.md`
- `templates/rules/session-end-capture.md`

Both are written in plain prose with explicit "what to do for each case" sections, designed to be loaded as part of the user's CLAUDE.md and parsed by Claude at session start.

### Why this all matters

After session 14 the repo had everything you'd want. After session 15 the install was zero-friction. After **this** session, the setup maintains itself:

- Every user is always on the latest version, automatically, in plain English.
- Nothing useful from any session ever evaporates — it cascades into the right place.
- The maintainer gets a real improvement queue from real sessions, not a wishlist they have to chase down.
- The friction loop closes: a user hits friction → Claude files it → next update fixes it → next session-start check tells the user it's fixed → friction is gone for everyone.

The setup is now genuinely "set it and forget it" for non-technical users. They open Claude Code in their vault, type things, and the AI brain takes care of itself in the background.

---

## April 11, 2026 (fifteenth session — one-command bootstrap, team-vault join mode, adaptive meeting tool selection, Phase 0 hardening)

This session is about onboarding speed for entire teams. The previous session shipped the missing pieces. This one makes the install zero-friction for new users **and** for new team members joining an existing shared vault.

### 1. One-command bootstrap (`bootstrap.sh` + `bootstrap.ps1`)

Two new files at the repo root: a Mac/Linux bash bootstrap and a Windows PowerShell bootstrap. Both are designed to be `curl`/`irm`-piped from anywhere — you don't need to clone the repo first, you don't need to launch Claude Code first, you don't need to know which directory anything goes in.

**Mac and Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/mycelium-hq/ai-brain-starter/main/bootstrap.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/mycelium-hq/ai-brain-starter/main/bootstrap.ps1 | iex
```

Both scripts install **the entire stack** from scratch:
- Homebrew (Mac), Python 3.10+, Node.js, npm, pipx, **bun**, **gh**
- graphify CLI + the bundled Claude skill (with the optimization scripts)
- meeting-todos and patterns sub-skills (previously deferred to later phases — now installed in Phase 0 alongside graphify so the user has a working stack the moment bootstrap finishes)
- claude-mem registered via the marketplace AND via npx (belt-and-suspenders)
- humanizer cloned from the fork
- notebooklm cloned from upstream
- The Granola MCP wired into `~/.claude/.mcp.json` so the meeting workflow rule works on day 1
- The ai-brain-starter skill itself

Both scripts end with a **verification block** that checks every dependency is callable and reports failures explicitly with red `✗` markers. **Never fail silently** — if something didn't install, the user is told exactly what failed and how to retry. This rule exists because of a real incident (covered below) where a team member's Phase 0 left graphify partially installed and the broken state stayed invisible for days.

Both scripts are **idempotent and safe to re-run**. They skip anything already installed.

### 2. Phase 0 hardened (sub-skills, bun, gh, Granola MCP, marketplace registration, verification block)

The in-conversation Phase 0 of `/setup-brain` got the same treatment. Previously it installed graphify but only graphify; the other sub-skills (`meeting-todos`, `patterns`) were copied later in Phase 11/22, which meant users who stopped the conversation early ended up with broken slash commands. Now Phase 0 copies all three sub-skills together.

Other Phase 0 fixes:

- **`bun` runtime is now installed explicitly.** claude-mem's worker is a bun script, not a node script — without `bun`, the plugin's slash commands silently fail. This was missing from every previous Phase 0.
- **`gh` (GitHub CLI) is now installed.** Used by the session-close repo-update propagation rule and by anyone who wants to fork the repo and push improvements back.
- **claude-mem is registered via the marketplace AND via npx.** Previously Phase 0 only ran `npx claude-mem install`, which is a different code path from the marketplace plugin install. The repo now writes the marketplace registration directly into `~/.claude/settings.json` so `/plugin` commands work without manual setup.
- **Granola MCP is auto-wired into `~/.claude/.mcp.json`.** The meeting workflow rule in CLAUDE.md depends on this MCP — without it, the rule fires but can't fetch the transcript. Previously users had to set this up manually. Now it's there from session 1.
- **Verification block at the end of Phase 0.** Checks every CLI is callable, every skill folder exists, the marketplace registration is in place, and the Granola MCP is wired. Reports failures explicitly. **Never fail silently.**

All three platform sections (Mac, Windows, Linux) got the updates.

### 3. New mode — `/setup-brain join-team` for new team members

Until now, `/setup-brain` only handled two cases: brand-new personal vault, or upgrading your own existing vault. There was no path for "I'm a new team member joining a shared vault someone else already set up." This is now a first-class mode.

**Auto-detection** at the very top of Phase 1: before asking the language question, the skill walks the cwd (and parent directories, and one level deep) looking for an existing CLAUDE.md. Three outcomes:

- **No CLAUDE.md anywhere** → mode A, new personal vault, walk through every phase
- **CLAUDE.md in cwd** → mode C, upgrade your own vault, use the "Already Set Up?" branch
- **CLAUDE.md in a parent directory** → ask whether they're joining a team or starting fresh
- **CLAUDE.md in a SUBFOLDER of the cwd** (the cwd-mismatch case) → auto-fix it (next section), then run mode B

The user can also force the mode by typing `/setup-brain join-team` directly.

**In mode B, the skill skips Phases 2, 3, 4, 5, 14, 15, 16, 19** — all the structure-creation phases. The vault already exists; duplicating that work would just create conflicts. It runs only Phase 0 + the cwd auto-fix + the meeting tool selection + a verification block + a hand-off message.

### 4. Cwd-mismatch auto-fix for shared team vaults

This is the load-bearing fix for a class of bugs that affects every team using a shared folder service (Google Drive, OneDrive, Dropbox) that wraps the actual content in a single subfolder.

**The bug:** Claude Code only auto-loads `CLAUDE.md` from the current working directory and walks **upward** through parent directories. It does **not** walk **downward** into subfolders. So if a team member launches Claude from the wrapper folder (the natural choice — that's the folder Drive/OneDrive opens by default), the team's CLAUDE.md is never loaded. Their session has zero project context. The meeting workflow rule doesn't fire. The graph never gets read. Every answer is generic. **The user can go days without realizing their setup is broken** because Claude responds normally — it just doesn't know anything about their team.

**The fix:** when the join-team detection finds a CLAUDE.md in a subfolder (not in the cwd itself), it auto-writes a thin **pointer CLAUDE.md** at the cwd that says "the real CLAUDE.md is in `<subfolder>` — please load that file at session start." Claude Code reads this pointer, follows it, and loads the real one. The fix is invisible to the user from then on — every team member who runs Claude from either folder gets the same context.

The pointer file is documented (it explains why it exists) so future maintainers don't accidentally delete it thinking it's stale boilerplate.

This bug was the reason this session happened. A team member's session was answering strategy questions without the team's knowledge graph for days. The diagnosis took the rest of an audit session. The fix is permanent.

### 5. Adaptive meeting tool selection (Phase 11)

Previously the meeting workflow rule was hardcoded to assume Granola + Google Drive Gemini transcripts. Most teams don't use that exact stack. Phase 11 now asks:

> Do you record / transcribe your meetings? Which tool? Pick the closest:
>   1. Granola
>   2. Google Meet + Gemini
>   3. Otter.ai
>   4. Fireflies.ai
>   5. Zoom recordings + Zoom AI Companion
>   6. Microsoft Teams + Copilot
>   7. Notion AI Notetaker
>   8. Manual notes
>   9. Multiple tools
>   10. None

For each tool, the skill wires up the right discovery path:
- **Granola** → registers the Granola MCP (already done in bootstrap, verified here)
- **Gemini Docs** → uses the Google Drive MCP, asks which Drive folder they live in
- **Otter / Fireflies** → asks where they auto-export, points discovery there
- **Zoom / Teams** → asks for the local recording path, finds VTT files
- **Notion** → routes through the Notion MCP
- **Manual** → no wiring, just installs the meeting-todos skill
- **Multiple** → walks through each one separately, generates a parallel-discovery rule

After collecting the answer, the skill **generates a meeting workflow rule adapted to the user's actual tools** and appends it to their CLAUDE.md. The rule includes a "source hierarchy" step: when multiple sources exist for the same meeting (e.g. Granola summary + verbatim transcript), prefer the verbatim source and skip the summary to save tokens.

### 6. Public-repo personal-context rule (`CLAUDE.md`)

Added a new section to the repo's `CLAUDE.md` codifying the rule that future Claude sessions working on this repo must follow: **never hardcode personal context in new content.** No names, no company, no personal vault paths, no anecdotes that name real people. When you need to illustrate a pattern with an example, invent a fictional one. Existing files with personal references (the README's Background section, the LICENSE, the CHANGELOG narrative) are load-bearing context — don't strip them. The rule applies only to new content.

The rule also requires grepping the diff for personal terms before any commit. This was added after three references slipped through in this session before the maintainer caught them — the audit step prevents that recurrence.

### Why this all matters

After this session, here's what onboarding looks like for a brand-new team member joining an existing shared vault:

```bash
# 1. Install Claude Code (one-time, follow the prompts at https://claude.ai/code)
# 2. Open the shared vault folder (Google Drive, OneDrive, etc.) on your machine
# 3. Run the bootstrap (one command, ~5 minutes)
curl -fsSL https://raw.githubusercontent.com/mycelium-hq/ai-brain-starter/main/bootstrap.sh | bash

# 4. Open Claude Code in the shared folder
cd "<path to the shared vault>"
claude

# 5. Type one command
/setup-brain join-team
```

That's it. No CLAUDE.md editing, no manual MCP setup, no folder creation, no meeting tool guesswork, no "wait, why isn't Claude using the graph" debugging. The cwd-mismatch is auto-fixed. The meeting tool is detected and wired. The verification block tells you exactly what's working and what isn't. Five minutes from "I just joined the team" to "I have a working AI-powered vault session."

For a brand-new personal vault user, the same bootstrap + `/setup-brain` (without `join-team`) walks them through the full conversational setup. Same one-command start.

---

## April 11, 2026 (fourteenth session — full setup audit, graphify pipeline fix, memory system docs, power tools catalog, Dataview library)

This session was a full audit comparing my private Claude Code setup against this public repo, and shipping the gaps. The repo had been quietly missing pieces I use every day. Five priorities, all shipped:

### 1. Graphify pipeline was broken — now fixed

The repo shipped 3 of the 5 graphify pipeline scripts. The two missing ones (`graphify_stage_select.py` and `graphify_stage_finish.py`) meant anyone trying to do a real staged graphify run on a large vault hit a dead end. They're now in `skills/graphify/scripts/`, de-personalized, with a `--vault-root` arg so they don't assume any specific vault path.

Also shipped: the full **`skills/graphify/RUNBOOK.md`** — the production playbook with cost guardrails, the optimized 7-phase pipeline, and 36 lessons learned from running graphify on a 4,700-file vault across 5 sessions and ~5M LLM tokens. Every lesson is from a real failure or optimization that landed. Most important ones to know:

- **Run `graphify_prep.py --apply` first** — wikilink regex pre-extraction yields more edges than the LLM does, and is free
- **Use chunk size 50, not 20** — the per-chunk prompt overhead is the dominant cost
- **The Grep-first prompt cuts 46% off baseline tokens** — agents that read files one at a time waste enormous amounts of tokens
- **Cache hit detection must distinguish preflight stubs from real LLM extractions** — naive "does the cache file exist?" reports `0 tokens needed` even when the LLM layer is missing. The new `is_llm_extraction()` discriminator catches this.
- **The wrong-root cache miss is the costliest beginner mistake** — if you suddenly see 0% cache hits on a folder you've graphified before, you're 100% running against the wrong vault root. The new stage_select.py warns when this happens.

### 2. Power Tools catalog (`docs/POWER_TOOLS.md`)

A new doc catalogs every third-party skill, MCP server, and Obsidian plugin this setup wires together — with attribution, install commands, source links, and the *why* behind each one. Nothing in the catalog is built by this repo; it's all open source by other people. What this repo does is install them and make them work in concert.

Now properly cataloged with attribution:
- **graphify** — knowledge graph builder
- **humanizer** ([adelaidasofia/humanizer](https://github.com/adelaidasofia/humanizer), forked from blader/humanizer) — de-AI writing pass
- **nano-banana** ([devonjones/devon-claude-skills](https://github.com/devonjones/devon-claude-skills) by Devon Jones) — image generation via Gemini 3 Pro Image
- **notebooklm** ([PleasePrompto/notebooklm-skill](https://github.com/PleasePrompto/notebooklm-skill)) — query NotebookLM notebooks for source-grounded answers
- **claude-mem** ([thedotmack/claude-mem](https://github.com/thedotmack/claude-mem)) — automatic cross-session memory
- **Granola MCP** — meeting notes auto-sync (powers the meeting workflow rule)
- **Obsidian plugin stack** — Dataview, Templater, Calendar, Tasks
- **Recommended additional MCPs** — Linear, Slack, Gmail, Calendar, Drive, HubSpot, Apollo

The catalog ends with a "how they fit together" section showing a full day of real workflows: morning journal → meeting → cascade → strategy question → pitch deck.

### 3. Phase 0 now installs nano-banana and notebooklm too

Previously only humanizer was wired into Phase 0's silent install. Now nano-banana (via the devon-claude-skills marketplace) and notebooklm (via git clone) get installed alongside, so users have the full third-party skill stack from the first session. Updated for Mac, Windows, and Linux.

### 4. Memory system docs (`docs/MEMORY_SYSTEM.md`)

The most underrated pattern in this whole setup: the **typed memory directory** at `~/.claude/projects/<project>/memory/` that turns Claude from a stateless assistant into a colleague who actually accumulates context across sessions.

The doc explains:
- **The 5 memory types** — `user`, `feedback`, `project`, `reference`, `discovery` — when to use each
- **What NOT to put in memory** — not ephemeral tasks, not git history, not anything CLAUDE.md already covers
- **Why you should save validated approaches, not just corrections** — if you only save "don't do X", Claude grows overly cautious. Save the wins too.
- **How to structure feedback memories** — lead with the rule, then **Why:** (the reason from a past incident), then **How to apply:** (when this kicks in)
- **Always convert relative dates to absolute when saving project memories** — "Thursday" → "2026-03-05" or the memory becomes uninterpretable in 2 weeks
- **Quarterly memory hygiene** — open MEMORY.md, delete what's no longer true, update what's drifted
- **Memory vs CLAUDE.md vs Tasks vs Plans** — when to use which persistence layer

This pattern is what makes Claude stop making the same mistakes after ~20 sessions and start anticipating your nuances after ~50.

### 5. Reusable Dataview query library + standalone build-journal-index.py + Decision Log template

Three new files in `templates/` and `scripts/`:

- **`templates/dataview-queries.md`** — 17 ready-to-use Dataview queries for journals, CRM, AI chats, decision logs, drafts, and cross-source searches. Copy/paste any of them into a note and they render live. Replace the folder names with yours and they work immediately.

- **`scripts/build-journal-index.py`** — standalone, parameterized version of the script that builds `journal-index.json`. Powers the `/insights` and `/weekly`/`/monthly` skills. Accepts `--vault-root`, `--journal-dir`, and `--meta-dir` so it works for any vault layout. Run weekly via cron or manually.

- **`templates/Decision Log.md`** — template for tracking the *how* of your decisions (what / why / floor / stakes / speed / outcome / pattern), with the philosophy of why you fill in `outcome` and `pattern` later (you can't grade your own decision from inside the moment — you need distance). Includes a fictional example so it's clear how to use it.

### 6. README and CLAUDE.md updated

The README has a new "Deeper Documentation" section pointing to all the new docs. The repo CLAUDE.md is updated to list the new standalone sub-skills (`patterns`, `graphify`) and the reference docs (`docs/`, `templates/`, `scripts/`).

### Why this all matters

If you're a stranger who just cloned this repo to set up your own AI brain, you now get **everything I actually use** — not just the parts I happened to ship in earlier sessions. The graphify pipeline is complete instead of broken. The memory system is documented instead of hidden. Every third-party tool I depend on is cataloged with attribution and install commands. The Dataview library and Decision Log template are reusable patterns, not personal artifacts.

The audit found 5 high-value gaps. All five are now fixed.

---

## April 11, 2026 (thirteenth session — meeting workflow, CRM structure preservation, personal↔team to-do separation, session-close repo-update check)

Four new hard rules baked into the Obsidian Rules section + Session Protocol. All four came from a real-world failure cascade during Adelaida's pitch-feedback session with an advisor: Claude skimmed a Granola transcript summary instead of reading the full Gemini Google Doc verbatim, then replaced a standard CRM dataview block with a custom History section (breaking the user's normal flow), then nearly mixed personal and team to-dos. We shipped the fixes to Adelaida's vault that day. These rules now ship to every user who builds their vault from this starter so the same failures can't recur.

### 1. Meeting workflow — "I just had a meeting" trigger (new Rule 15)

When the user says any variation of "I just had a meeting" or "pull the transcript," Claude now runs a full automated workflow: discovery → source hierarchy → full cascade.

- **Discovery:** search Google Drive for a Gemini transcript (verbatim, timestamped) AND Glob the meeting-notes folder for recently-synced Granola notes AND any chat context — all in parallel, before reading anything.
- **Source hierarchy (the big one):** if a Gemini Google Doc exists, read ONLY the Gemini doc — it's the source of truth. Do NOT also read the Granola file; it's a post-processed summary and re-reading it wastes tokens. Still file and wikilink the Granola note so the user can reference it. If no Gemini exists, read Granola fully — it's the only source. Never skim either. Dispatch a subagent if it exceeds main-context.
- **Full cascade (8 steps, no asking):** enrich the meeting note in place → cascade to canonical strategy docs with a rule-consistency scan → log high-stakes decisions → update the CRM contact file (preserving the dataview structure) → split to-dos between team and personal → run /humanizer on any investor-facing prose → verify with backlinks → report every file changed.

Why it matters: skimming transcript summaries loses load-bearing context. The advisor's most important commitments — verbatim numbers, conviction-escalation moments, accountability statements — live in the full verbatim transcript, not the summary. This rule makes Gemini-first the default and the full cascade automatic.

### 2. CRM structure preservation (enhanced Rule 9)

Every CRM file in a user's vault should follow a standard shape: YAML frontmatter → short inline bio → `## Meeting Notes` section with explicit wikilinks → `## Mentions` section containing a dataview query.

Claude now has an explicit rule: **do NOT replace the `## Mentions` dataview block with a long-form "History" narrative.** The dataview query is how users find related content from the CRM page. If a contact needs more context than bullets allow, add a `## Notes` block, not a History section. New meeting notes must (a) include the contact as `[[Bare Filename]]` in the Attendees list so the dataview picks it up, AND (b) be listed explicitly under `## Meeting Notes` in the contact's CRM file. Both. Not one. Before editing any CRM file, Claude reads 2–3 adjacent CRM files first to confirm the pattern.

### 3. Personal ↔ team to-do separation (new Rule 16)

For users whose vault is connected to a shared team vault (symlink, sync, cloud folder), personal and team to-dos live in two different files that never mix content.

- Personal to-do file = the full personal list: writing, payments, emotional commitments, health, travel logistics, everything. Never syncs to the team vault.
- Team to-do file = business work only. Visible to teammates. No personal items.
- Only copy business-related items from personal to team. Never the reverse. When ambiguous, default to personal.
- **Single-pane view via block embed, not copy.** The personal file has a `![[Team To-dos]]` block embed at the bottom so the user sees everything in one view without duplicating items. One-way: team-to-personal view only.

### 4. Always read the full transcript (new Rule 17)

Explicit rule that transcripts and long-form sources must be read in full before any downstream summary, decision log, or action-item extraction. No inferring from the first N lines. Dispatch a subagent for files that exceed main-context with explicit "read 100% in chunks" instructions. If you have to skim, say so out loud.

### 5. Wikilinks — bare filenames only (enhanced Rule 14)

Added explicit guidance: `[[Colombia]]`, never `[[🌱 Curiosities/Colombia]]`. Path-form wikilinks break graph canonicalization and leak folder structure into shared docs.

### 6. Session close — always check for repo updates (new Session Protocol Step 4)

Before closing ANY session, Claude now scans for improvements that should propagate upstream: new rules, skills, scripts, prompt patterns, runbooks, workflow fixes. If anything qualifies, Claude asks: *"We improved [X] this session. Want me to push it to the ai-brain-starter repo so your team (and anyone else who builds their vault from your repo) benefits?"* Improvements that stay local by default are wasted — this rule closes the gap.

### 7. Auto-update hook now offers per-rule merge into vault CLAUDE.md (follow-up fix in `222f9df`)

The thirteenth-session commit shipped the rules to this repo, but the existing auto-update hook would only **notify** users that the CHANGELOG had new entries. It wouldn't diff the new rules against the user's vault CLAUDE.md or offer to merge them, so every existing vault would silently fall behind. The gap was caught the same afternoon and patched in a follow-up commit.

The auto-update hook's `additionalContext` in `hooks.json` now explicitly instructs Claude to:

1. Read the top CHANGELOG entry after the hook's `git pull` fetches commits.
2. **Check whether the update added new rules to the Obsidian Rules or Session Protocol sections of SKILL.md.** If it did, read the user's vault CLAUDE.md and compare.
3. For every new or enhanced rule that is not already in the user's vault CLAUDE.md, offer a per-rule merge: show a short diff (old rule vs. new rule, or just the new rule if it's a new addition), explain why the rule exists (citing the failure mode from the CHANGELOG), and ask one yes/no question. Wait for confirmation.
4. On yes: back up the user's current CLAUDE.md to `CLAUDE.md.bak-YYYY-MM-DD-HHMM` first, then apply the edit. On no: drop the rule and don't ask again this session.
5. If the sync output lists backed-up skill files, mention it casually so the user knows their customizations are recoverable.
6. Check whether `hooks.json` differs from the user's local `settings.local.json` — if so, offer to update `settings.local.json`.

Without this fix, rules shipped to the repo but silently failed to reach anyone who already had a vault built from an earlier version. With it, the auto-update flow closes the full loop: pull → diff → offer merge → apply → backup. The more elaborate session 16 rule cascade builds on this same foundation but via a dedicated `update-check.sh` script instead of inline hook logic — the two mechanisms stack rather than replace each other.

**Maintainer-side note (not user-facing but worth recording):** the same fix surfaced that the maintainer's own personal vault `settings.local.json` was missing the auto-update hook entry entirely. The hook had shipped in the repo's `hooks.json` template and in new-install `/setup-brain` flows, but never propagated to the maintainer's pre-existing vault config. Atomic `jq --slurpfile` merge fixed it with a timestamped backup. Worth remembering: the repo's hooks template is not the source of truth for an already-configured vault. The two can drift, and the drift is invisible until someone runs the comparison.

---

## April 10, 2026 (twelfth session — full skill-folder sync + backup-before-overwrite + Phase 11 pointing at the right meeting-todos)

Fixes a structural bug where **skill optimizations lived in the repo but never reached installed users**, and gives the auto-update path a safe backup mechanism.

### 1. Full skill-folder sync (fresh install + auto-update)

Previously, Phase 0 installed graphify with `cp SKILL.md` — only the one file. The `scripts/` folder (with `graphify_prep.py`, `graphify_chunk.py`, `graphify_canonicalize.py`, the 80–92% cost-cutting wrappers) and `OPTIMIZATIONS.md` were **never copied**. Result: every new user got a graphify skill whose SKILL.md referenced scripts they didn't have. The optimizations were silent no-ops for everyone except developers who ran `/graphify` from inside the starter repo clone.

**Fixed** for all three platforms:
- **Mac:** `cp -R ~/.claude/skills/ai-brain-starter/skills/graphify/. ~/.claude/skills/graphify/`
- **Linux:** same pattern (Linux Phase 0 was *also* missing graphify skill install entirely — now fixed)
- **Windows:** `xcopy /E /I /Y ...` to handle recursive folder copy on cmd/PowerShell

Phase 9's "if graphify skill is missing" retry command was also updated with the `-R` pattern and a comment explaining why the `/.` suffix is critical.

### 2. Phase 11 meeting-todos now points at `skills/meeting-todos/`

The repo has two copies of `meeting-todos/SKILL.md`:
- `meeting-todos/SKILL.md` at the repo root (110 lines, Apr 9 — legacy)
- `skills/meeting-todos/SKILL.md` under the skills folder (116 lines, Apr 10 — canonical, added in `6a0edc0 add missing graphify and meeting-todos skills to repo`)

Phase 11 was installing from the stale root-level copy. Fixed to install from `skills/meeting-todos/` using the same `cp -R ... /.` pattern. The legacy root-level `meeting-todos/` folder is now unused — can be removed in a future cleanup commit.

### 3. New: `scripts/sync-skills.sh`

Added a helper script at `scripts/sync-skills.sh` that handles the repo → install sync with backup-before-overwrite semantics. Called by the auto-update hook on session start whenever `git pull` fetched new commits.

**What it does:**
1. For every skill in `~/.claude/skills/ai-brain-starter/skills/*/`, walks every file recursively.
2. If the corresponding file at `~/.claude/skills/<skill-name>/` doesn't exist → creates it.
3. If it exists and matches byte-for-byte (`cmp -s`) → no-op, no noise.
4. If it exists and differs → **backs up the current version to `<file>.bak-YYYY-MM-DD-HHMM` before overwriting with the repo version**. User's customizations are always recoverable.
5. Writes a summary to `~/.claude/skills/ai-brain-starter/.sync.log` and prints it to stdout.
6. Exits non-zero if any write failed, so the hook can surface it (NEVER fail silently).

**Why backup + overwrite (instead of skip-on-customization):** Adelaida's call. Safer default — users never silently lose updates, but they also never silently lose their customizations. Recovery is a one-line `mv SKILL.md.bak-2026-04-10-HHMM SKILL.md`. No diff logic to get wrong.

### 4. Auto-update hook now calls `sync-skills.sh`

The Phase 5 auto-update hook (in `.claude/settings.local.json`) previously told Claude to "Copy any updated skills from ~/.claude/skills/ai-brain-starter/skills/ to ~/.claude/skills/ (overwrite existing)" — a vague natural-language instruction that different Claude sessions interpreted differently and that didn't preserve customizations.

Now the hook runs `bash ~/.claude/skills/ai-brain-starter/scripts/sync-skills.sh` deterministically after `git pull`. Output is captured into `$SYNC_OUTPUT` and passed into the hook's additionalContext so Claude can tell the user exactly what was created, updated, or backed up.

### 5. Session protocol hook path fix

The same Phase 5 hook block had the session-start protocol pointing at `Meta/Last Session.md` and `Meta/Current Priorities.md` without the `⚙️` emoji. Since Phase 3 creates `⚙️ Meta/` with the emoji, the hook was sending Claude to read files that didn't exist — silent violation of the protocol. Fixed to `⚙️ Meta/Last Session.md` and `⚙️ Meta/Current Priorities.md`. Matches the fix from the eleventh session which got everywhere *except* this one hook string.

### What existing users should do

Re-run `/setup-brain` and say "just resync my skills." Or manually:
```bash
cd ~/.claude/skills/ai-brain-starter && git pull
bash scripts/sync-skills.sh
```

If the sync output shows any `.bak-YYYY-MM-DD-HHMM` files, your local customizations are preserved there. Diff the backup against the new version and merge anything you want to keep back.

Also run the same re-sync on `.claude/settings.local.json` — copy the updated hook block from `hooks.json` in the repo to pick up the new sync-skills.sh call and the ⚙️ Meta path fix.

---

## April 10, 2026 (eleventh session — path fixes + honoring the NEVER-fail-silently rule)

Pure bug-fix pass. No new features. Fresh pulls now install cleanly without silent path mismatches.

### Path fixes — `⚙️ Meta/` and `📓 Journals/` emoji consistency

Phase 3 creates the vault with emoji-prefixed folders (`⚙️ Meta/`, `📓 Journals/`), but multiple downstream templates referenced them without the emojis. On a fresh install these would silently write to non-existent folders:

- **Phase 5 hook scripts** — `session-end-hook.sh`, `write-hook.sh`, and the `.claude/settings.local.json` hook command strings were all referencing `Meta/scripts/` (no emoji). Fixed to `⚙️ Meta/scripts/` in the save paths, the internal `$VAULT/Meta/` variable expansions, and the JSON hook command lines.
- **Phase 10 generated daily-journal skill** — Step 7 save path was `[VAULT_PATH]/Journals/` instead of `[VAULT_PATH]/📓 Journals/`. Claude would have saved entries to a phantom folder or created a parallel one (same class of bug as the `~/Desktop/Desktop/` phantom folder issue).
- **Phase 10 floor concept note Dataview queries** — four instances of `FROM "Journals"` that would have returned zero results because the actual folder is `📓 Journals`. Changed to `FROM "📓 Journals"`.
- **Phase 18 build-journal-index.py** — the script path reference was `Meta/scripts/build-journal-index.py` in three places. Fixed.
- **Phase 18 insights save paths** — `Journals/Weekly Insights/` and `Journals/Monthly Insights/` without the emoji. Fixed.
- **Phase 16 Wikilink Reference path** — `[VAULT_PATH]/Meta/Wikilink Reference.md` without the emoji. Fixed.
- **Phase 0 broken-install diagnostic** — said `Meta/journal-index.json` exists and pointed at "Phase 11" to regenerate it. Actual location is `⚙️ Meta/journal-index.json` and the script is installed in Phase 18. Both fixed.

### Duplicate rule number in the Efficiency Rules template

The `## Efficiency Rules` block that gets written to every new user's CLAUDE.md had two rules numbered `5.` — "Don't do things without confirming first" and "Route to the right tool." Renumbered the second one to `6.` so every new vault doesn't ship with a broken numbered list.

### Honoring the NEVER-fail-silently rule

Phase 16 rule 12 says `NEVER fail silently` but the hook scripts had zero error handling — which is how the `Meta/` path bugs survived so many commits. Hardened both scripts so they can't fail quietly:

**`session-end-hook.sh`:**
- Checks that `⚙️ Meta/` exists before doing anything. If missing, writes to `hook-errors.log` at the vault root AND emits a `HOOK ERROR` message into the Stop hook context so Claude tells the user immediately.
- Every file-write operation now `2>>` redirects stderr into `⚙️ Meta/hook-errors.log`. If any write fails, the next session start surfaces it.

**`write-hook.sh`:**
- Guards on `python3` being on PATH. Emits a `HOOK ERROR` if missing instead of silently producing empty JSON.
- Captures Python parse errors via `$?` and surfaces them in the hook context. Replaced the bare `except` with an explicit stderr write.

**`build-journal-index.py`:**
- Guards on `📓 Journals/` and `⚙️ Meta/` folders existing. `sys.exit(1)` with a clear message if either is missing.
- Replaced the bare `except: pass` (which was swallowing per-file parse errors) with a proper `except Exception as e:` that collects errors into a list.
- If any errors occurred, writes them to `⚙️ Meta/journal-index-errors.log` and exits with code `2` so cron/wrappers can detect partial success.

### Placeholder syntax normalization

My own Phase 10 scheduled-task section from yesterday used `{JOURNAL_TRIGGER_TIME}`, `{JOURNAL_TRIGGER_TZ}`, `{today}` with curly braces. Rest of the file uses `[VAR]` with square brackets. Normalized to brackets so Claude doesn't have to guess which substitution style is in play.

### What you should do if you already ran setup before this commit

Re-run `/setup-brain` and tell it to repair paths. Or manually:
1. Move any files currently under `Meta/` (without emoji) into `⚙️ Meta/`. Same for `Journals/` → `📓 Journals/`.
2. Update your `.claude/settings.local.json` hook paths to use `⚙️ Meta/scripts/` instead of `Meta/scripts/`.
3. Re-run `python3 "⚙️ Meta/scripts/build-journal-index.py"` to rebuild the index at the correct path.
4. If your CLAUDE.md has duplicate rule 5 in the Efficiency Rules block, renumber the second one to 6.

---

## April 10, 2026 (tenth session — daily journal trigger + corporate-event Onde suggestion)

Two new features, both touching Phase 10 and Phase 16 of `SKILL.md`. Existing users: see `migrations/2026-04-10-daily-journal-trigger-and-onde-suggestion.md` for how to apply.

### 1. Daily journal trigger (Phase 10)

The "what time of day would you usually journal?" question used to accept vague answers like "morning" or "evening." Now setup asks for a **specific time** (default `7:30pm`), stores it as `JOURNAL_TRIGGER_TIME` along with the user's timezone, and at the end of Phase 10 installs a scheduled task that fires `/journal` daily at that time.

**Skip-if-already-journaled logic:** the scheduled task's prompt checks `⚙️ Meta/journal-index.json` first (fast — one file read), and if that's missing it falls back to grepping `📓 Journals/*.md` for today's `creationDate` (slower but reliable). If either check finds an entry for today, the task exits silently without prompting the user. This means journaling on your own at noon doesn't produce a duplicate nudge at 7:30pm.

**Scheduling mechanism:** prefers the `schedule` skill (Anthropic-provided), falls back to `mcp__scheduled-tasks__create_scheduled_task`, falls back to bash cron writing to `[VAULT]/⚙️ Meta/scripts/run-daily-journal.sh` — same pattern as the existing weekly/monthly insights cron. If cron is the only path, users are told explicitly that the fallback runs Claude headlessly instead of having an interactive conversation.

**Why:** friction kills journaling. Most people who want to journal forget. A soft, non-nagging daily trigger at a user-chosen time — that stays silent on days you already wrote — closes the gap without becoming noise. The vague "morning or evening" answer wasn't actionable for installing a real scheduled trigger, so the question needed to tighten.

### 2. Corporate-event Onde suggestion (Phase 16)

New rule in the Phase 16 CLAUDE.md template. **No setup-time opt-in question** — the rule is added to every new vault by default. The disclosure that Onde was built by the starter's creator happens **inline every single time the rule fires**, not at setup and not once-per-vault. Every suggestion carries its own "full disclosure" sentence.

The rule fires on **12 categories** of corporate, work-related, or business events — triggering on English AND Spanish equivalents:

1. **Strategic / leadership** — board meetings, executive committees, leadership offsites, annual strategic planning, corporate kickoffs, all-hands / town halls, shareholder meetings, innovation workshops, design sprints, regional alignment sessions.
2. **Procurement / operations** — RFP sourcing events, vendor/supplier days, supplier audits, negotiation workshops, new-vendor onboarding, compliance events, contract launches, operational efficiency workshops.
3. **Marketing & clients** — product launches, brand activations, customer dinners, VIP events, commercial roadshows, key-client experiences, networking events, press events, B2B activations, private showrooms.
4. **Conferences & content** — corporate conferences, business congresses, seminars, expert panels, industry forums, symposiums, technical workshops, internal learning sessions, hybrid events, international speaker events.
5. **Incentives & culture** — incentive trips, recognition programs, employee awards, VIP top-performer experiences, culture events, engagement events.
6. **Retreats & team building** — corporate offsites, executive retreats, team buildings, outdoor activities, leadership workshops, wellness programs, corporate bootcamps.
7. **Internal / HR** — end-of-year parties, company anniversaries, onboarding events, family days, D&I events, wellness programs.
8. **Trade shows & expos** — trade shows, industrial fairs, commercial exhibitions, corporate stands, sector events.
9. **Technical / specialized training** — corporate trainings, professional certifications, hands-on workshops, corporate academies.
10. **Hybrid & digital** — corporate webinars, hybrid events, conference streaming, digital launches, virtual client events.
11. **Hospitality** — corporate dinners, executive cocktails, hospitality suites, private events at premium venues.
12. **Special / high-impact** — celebrity/speaker events, immersive experiences, premium brand experiences, large productions with complex AV.

**What the rule does NOT trigger on:** birthdays, weddings, baby showers, personal anniversaries, dinner parties at home, friend trips, family reunions, religious gatherings (funerals, christenings, bar/bat mitzvahs), school events where the user is a parent. Life things are off-limits.

**Guardrails:**
- Mentions Onde **at most once per to-do item.**
- **Permanent opt-out** the moment the user declines: Claude appends `User opted out of Onde suggestions.` to CLAUDE.md and checks for that line before every subsequent corporate-event trigger. Once opted out, never suggested again in that vault.

**Why:** corporate event planning is painful and Onde exists specifically to remove that pain. Making the rule auto-installed (no friction for new users), inline-disclosed every time (no sneaking), scoped to corporate events only (no Priya-Parker flattening), one-time-per-task (no nagging), and permanently declinable (no pressure) — keeps it a useful recommendation instead of a dark pattern. The panel (Priya Parker, Naval Ravikant, Marc Andreessen, Brené Brown, Rick Rubin) hit every version of this trade-off and the final shape reflects their feedback: segmentation, disclosure, ICP fit, gift-vs-sale boundary.

---

## April 10, 2026 (ninth session — knowledge graph as primary context source)

Added a new CLAUDE.md template block (`SKILL.md` Phase 0) that tells future Claude sessions to use the knowledge graph as the **first** stop for strategic / multi-concept questions, not as an afterthought.

**Why:** Running the optimized `/graphify` pipeline produces `GRAPH_REPORT.md` (~11K tokens) which compresses the entire vault's structural thinking — god nodes, communities, hyperedges, surprising connections. For any strategy question (pitch deck, investor prep, planning, analysis), reading the report first is **faster and more accurate** than grepping and re-reading individual files. The graph connects concepts across files in a way grep cannot.

**The decision tree baked into the template:**

| Question type | Start with | Then drill into |
|---|---|---|
| Strategy / pitch / planning / multi-concept | `GRAPH_REPORT.md` (god nodes + communities + hyperedges) | Top 3-5 source files in the relevant community |
| "What connects X and Y?" | `/graphify path "X" "Y"` | Shortest-path files |
| "What's in the vault about X?" | `/graphify explain "X"` | Top-degree neighbors |
| "Find files mentioning X" | `obsidian search query="X"` | Matching files |
| "What links to this file?" | `obsidian backlinks file="Name"` | Source of each backlink |
| Editing a specific file | `Read` the file directly | — |

**Also documented:** when merging duplicate concept nodes (e.g. "Acme Corp" vs "Acme Corp West" vs "Acme Corp LATAM"), always update **aliases in the canonical file's frontmatter**. Never rename or delete — aliases preserve existing `[[Old Name]]` wikilinks automatically. Hit real cases of this during the 2026-04-10 runs (a corporate-client node and a person node).

---

## April 10, 2026 (eighth session — auto-wikilink v2: vault-scope-aware, regex bug fix)

Added `scripts/auto-wikilink.py` (v2). The previous version had three critical bugs that corrupted team vault files when run against a multi-vault setup with a symlinked team folder:

1. **Wrote path-form wikilinks** (`[[🌱 Curiosities/Colombia]]`) instead of bare filenames (`[[Colombia]]`). Source: it loaded canonical names from a `Wikilink Reference.md` index that had path-form entries, then wrote those literally into target files. The visible result in Obsidian was leaked personal-vault folder names showing up inside team-shared documents.

2. **Reached across the team-vault symlink** when building its term list, linking team-vault files to personal-vault concept notes. This violated the team-vault firewall: team members opening the synced docs would see references to private personal-vault folders they don't have access to.

3. **Substitution regex over-matched** when emoji or special characters were near a match. "A VP at Acme Corp" became "Curiosities/Colombiacme Corp]]" because the negative lookbehind `(?<!\[\[)` only checked 2 characters back, and the lookahead `(?!\]\]|[^\[]*\]\])` was fragile around unicode.

**v2 fixes all three:**

- **Vault-scope-aware:** files inside the team vault only link to terms whose source files are also inside the team vault. Personal context is invisible to team files. ONE-WAY FIREWALL — personal can reach into team if you opt in, team can never reach into personal.
- **Bare filenames + alias syntax only.** Hard guard refuses to write any wikilink containing `/` in the target. Path-form is impossible.
- **Region-tracking substitution.** Builds a list of `[[...]]` regions in the body first, then only modifies text strictly outside those regions. Recalculates after every change. Kills the character-eating bug.
- **Hard-blocks team-vault files** unless `--allow-team` is passed explicitly. Default is "personal vault only." You have to opt in to touch team-vault content.

Additional belt-and-suspenders fix: disabled `alwaysUpdateLinks` in the team vault `.obsidian/app.json` so Obsidian's own link-rewriter stops trying to "fix" cross-vault wikilinks during file watches.

If your setup has a similar multi-vault structure (personal Obsidian vault + symlinked team / shared / Google Drive folder), pull this version. The v1 bugs only manifested in multi-vault setups so single-vault users may not have noticed.

---

## April 10, 2026 (seventh session — graphify exclude flags + team vault support)

Ran the optimized graphify pipeline on Adelaida's Onde Team vault (a separate Google Drive-synced team vault with Archive folders). This surfaced four new issues that needed fixes:

1. **`--exclude` flag** on `graphify_prep.py` and `graphify_chunk.py`. Team vaults have `Archive/` folders that should not be indexed (per vault convention, archived content is historical only). Also useful for `.obsidian/`, `_review_alternate_drafts/`, etc. Multi-value flag: `--exclude Archive --exclude .obsidian`.

2. **`--no-dedupe` flag** on `graphify_prep.py`. Team vaults are curated and read-only — you should NEVER run dedupe on a cloud-synced team vault because it would delete real files. `--no-dedupe` skips the dedupe pass entirely but still writes the preflight JSON so the pipeline can proceed. Use this anytime the input is a read-only source (team drives, upstream repos, etc.).

3. **Label length cap of 60 chars** on file-stem labels and wikilink targets. Some journal / Substack draft filenames are long content snippets ("post to inspire others to redefine what wealth really means—it's not..."). When these become god nodes the graph is unreadable. Long labels are now truncated with ellipsis for display; full path stays in `source_file`.

4. **Cache root path gotcha (documented).** When running graphify on a source outside the CWD (e.g. team vault from a local temp working dir), the `save_semantic_cache` API only stores per-file entries for files that `Path(root) / fpath` can resolve. If the extraction has relative `source_file` paths and the CWD doesn't match the vault root, cache writes silently return 0. Fix: normalize `source_file` to absolute paths before calling the cache API. See OPTIMIZATIONS.md for the snippet.

Team vault run results (177 files, 4 chunks of ~30K words each, 3/4 succeeded):
- Regex preflight: 413 nodes, 1,040 EXTRACTED edges (free)
- LLM extraction: 342 nodes, 294 edges, 9 hyperedges (3 chunks)
- After canonicalize: 669 nodes, 1,308 edges, 113 communities
- Cache populated: 163 per-file entries
- Top god nodes: Onde (61), Pitch deck (46), Colombia (30), Pitch Narrative (27), Sales Coach (27), Strategy Index (27), Raise Sprint (26), Onde Summary (26) — exactly the right central business docs

---

## April 10, 2026 (sixth session — graphify cost optimization)

### Big: graphify wrapper scripts (cut LLM cost by 80–92%)
Running `/graphify` on a notes vault used to be expensive. A naive run on a ~1,500-file vault would burn ~10M LLM tokens because the upstream graphify skill re-extracts every wikilink the LLM finds, and many vaults have hidden duplicates from prior incomplete runs. A 566-file optimized run on Adelaida's vault produced **3,734 nodes / 13,699 edges** (vs the previous 812 nodes / 206 edges) at roughly one-eighth the naive cost.

What's new in `skills/graphify/`:
- **`OPTIMIZATIONS.md`** — read this BEFORE running graphify on anything bigger than 50 files. Step-by-step optimized pipeline.
- **`scripts/graphify_prep.py`** — runs BEFORE the LLM extraction. Two-pass dedupe (` 2.md` siblings + cross-directory copies) + regex preflight that pulls every `[[wikilink]]` and floor frontmatter tag as a free EXTRACTED edge. Typical: 30–60% file reduction.
- **`scripts/graphify_chunk.py`** — word-balanced bin-packing chunker (vs alphabetical). Skips files <500 words and `[AI Extract]` files (low inference yield). Targets 12 chunks of ~50 files each instead of 25-30 small chunks; cuts prompt-instruction overhead by ~60%.
- **`scripts/graphify_canonicalize.py`** — runs AFTER the LLM extraction. Collapses per-file scoped IDs into canonical labels (62% node reduction in testing), strips invalid `file_type` values agents invent, and **`--cache` writes results to `graphify-out/cache/` via `save_semantic_cache`** so the next `--update` run is FREE for unchanged files.
- **SKILL.md updated** with a TL;DR pointer at the top of the file.

The cache integration is the single most important addition. Without it, every weekly `/graphify` run repays the entire LLM cost. With it, subsequent runs only pay for genuinely new files.

Lessons baked in (read OPTIMIZATIONS.md for the full list):
- Always run prep before extraction
- Always run canonicalize + cache after extraction
- Always use word-balanced chunks of ~50 files (not alphabetical chunks of 20)
- The LLM should not re-extract wikilinks — only do INFERRED / semantic / rationale work
- Schema violations (invented `file_type` values) are common; canonicalize auto-fixes them
- Parallel subagent cap is ~10-12; for >12 chunks, dispatch in waves
- First Python `detect()` call sometimes hangs; wrap with a 90s timeout

---

## April 10, 2026 (fifth session)

### Breaking: Originals/ folder removed
Originals/ was solving a problem that doesn't exist. Most of your content is already original — journals, writing drafts, concept notes. Having a separate folder for "extra original" ideas was just adding a sorting decision that helped no one. The /patterns skill already surfaces recurring ideas automatically. Writing/Drafts/ is where ideas get developed.

What changed:
- Originals/ removed from folder structure (Phase 3)
- RESOLVER for Originals/ removed
- Originals/ → Writing pipeline docs removed (was just added last session — killed immediately)
- write-hook.sh no longer watches for Originals/ saves
- /patterns skill now proposes writing seeds in Writing/Drafts/ instead of Originals/
- CLAUDE.md rule changed from "protect Originals/" to "original ideas live where they happen"

If you have an existing Originals/ folder, you can keep it — nothing will break. But new setups won't create one. Consider moving any files from Originals/ into Notes/ or Writing/Drafts/ where they fit better.

### Fix: Auto-update now actually applies changes
Previously the auto-update hook would detect a new version and ask "Want me to update?" — requiring the user to understand what that means. Now: the hook runs `git pull` automatically, copies any updated skill files, syncs hooks.json to settings.local.json, and tells the user in plain English what changed. Zero manual steps.

---

## April 10, 2026 (fourth session)

### Fix: /weekly + /patterns now run automatically together
Previously the cron script only ran `/weekly` and Phase 22 told users to manually run `/patterns` after. Now the weekly cron script chains patterns automatically — one schedule, both run. Patterns in headless/cron mode auto-captures all findings without asking for confirmation (adds "(auto-captured — review and edit)" to new Originals/ files). Mac cron + Windows Task Scheduler scripts both updated.

### Fix: meeting-todos now fires automatically on meeting note save
Previously `/meeting-todos` required manual triggering. Now: when any file is saved to a Meeting Notes folder, the PostToolUse hook fires and Claude automatically runs the todo extraction, shows a preview, and waits for confirmation before writing. No manual step needed.

To upgrade from a previous install: replace `originals-hook.sh` with `write-hook.sh` in your `Meta/scripts/` folder (copy the new script from Phase 5 of setup) and update the path in `.claude/settings.local.json`. The `originals-hook.sh` behavior is preserved — `write-hook.sh` does everything it did, plus the meeting notes trigger.

### Fix: Originals/ → Writing/ → Substack pipeline now documented
The relationship between these three stages was missing. Added to Phase 3 (folder structure): Originals/ is the raw seed (verbatim, protected), Writing/Substack Drafts/ is the developed draft (links back to the seed), publishing is the final stage. A CLAUDE.md rule is now added automatically for users who have a Writing folder.

---

## April 10, 2026 (third session)

### New: /graphify skill bundled in repo (`skills/graphify/SKILL.md`)
The graphify CLI was already installed in Phase 0, but the skill file — which tells Claude *how* to run the full pipeline (parallel subagents, cache, clustering, HTML export, incremental updates) — was missing from the repo. Without it, `/graphify` does nothing. Now bundled at `skills/graphify/SKILL.md` and copied to `~/.claude/skills/graphify/` during Phase 0 and verified in Phase 9. Routing block added to CLAUDE.md in Phase 9.

Also documents a critical gotcha: `graph.json` stores edges under the key `"links"` (networkx format), not `"edges"`. Any custom script loading graph.json must use `graph.get('links', graph.get('edges', []))` or it silently gets zero edges.

### New: meeting-todos skill bundled in repo (`skills/meeting-todos/SKILL.md`)
Phase 12 already told setup to `cp -r ~/.claude/skills/ai-brain-starter/meeting-todos ~/.claude/skills/meeting-todos` — but the source folder didn't exist. That cp would fail silently for everyone. Now the skill is in the repo. Generic template with `[VAULT]` path placeholder.

---

## April 10, 2026 (second session)

### New: /patterns — Instinct Engine
The biggest gap in most second brains: patterns form during sessions, then evaporate. The Instinct Engine fixes this. Type `/patterns` after `/weekly` or any heavy session and it scans your recent journals, decisions, and drafts for recurring themes, frameworks, metaphors, and behavioral loops. It surfaces up to 5 proposals — Originals/ captures, new CLAUDE.md rules, concept notes — and executes the ones you confirm. Added as Phase 22 in setup. Skill template lives at `skills/patterns/SKILL.md` — setup copies and configures it for your vault path.

### New: Hardened Stop hook + session-end-hook.sh
The Stop hook now runs a bash script instead of just echoing a prompt. The script writes a guaranteed timestamp to `Meta/Session Log.md` every session (no Claude involvement) and appends a stub to `Last Session.md` so the date is always saved even if Claude doesn't complete the full update. Phase 5 now includes instructions to create and chmod this script.

### New: PostToolUse hook for Originals/ protection
A new PostToolUse hook fires after every Write tool call. If the written file is inside `Originals/`, it immediately prompts Claude to update Wikilink Reference.md before doing anything else. Closes the gap where Originals/ captures were saved but not linked. The hook script (`originals-hook.sh`) is created during Phase 5 setup. Also added to `hooks.json`.

---

## April 10, 2026

### New: hooks.json — updatable hook templates
Hooks (the session protocol, stop context save, pre-compact safety net) now live in `hooks.json` at the repo root. Previously they were embedded only in SKILL.md's Phase 5 setup, so existing users couldn't update them after a `git pull`. Now: pull the repo, compare `hooks.json` to your `.claude/settings.local.json`, update if anything changed. See `migrations/2026-04-10-hooks-json-and-argument-hints.md`.

### New: argument-hints on slash commands
Skills now show inline hints in Claude Code when you type a slash command. `/meeting-todos`, `/weekly`, `/monthly`, and `/graphify` now tell you what arguments they accept before you have to look it up.

---

## April 9, 2026 (meeting tools + repo infrastructure)

### New: /meeting-todos skill
After any meeting, type `/meeting-todos` and Claude reads the transcript, separates your action items from everyone else's, and shows you a preview before writing anything to your to-do. Time-sensitive commitments get flagged with ⚠️. Multilingual transcripts work fine. See `meeting-todos/SKILL.md`.

### New: GitHub issue templates + PR template
Added `.github/ISSUE_TEMPLATE/bug_report.md`, `feature_request.md`, and `PULL_REQUEST_TEMPLATE.md`. When someone opens a bug or feature request on GitHub, they now get a structured form instead of a blank box. PRs get a checklist that catches "forgot to update CHANGELOG" or "included personal data" mistakes before they merge.

---

## April 9, 2026 (final session — patterns borrowed from gbrain)

### New: Compiled truth + timeline for CRM entries
CRM entries now use a two-layer pattern. Above the `---` separator: 2-3 sentences synthesizing who this person is RIGHT NOW — rewritten whenever something significant changes. Below: an append-only timeline of events, never edited. This means clicking a contact gives you their current state instantly instead of scrolling through months of notes. Existing entries are unaffected until you migrate them — see `migrations/2026-04-09-compiled-truth-crm.md` for how.

### New: 💡 Originals/ folder — for your own thinking
New core folder added to every setup. Protected for the user's own frameworks, theses, metaphors, and original ideas — captured verbatim in their exact phrasing. Never paraphrased, never merged into a generic concept note. File names = the idea itself. Claude now has a rule to capture original thinking here immediately whenever it surfaces in conversation or journals. See `migrations/2026-04-09-originals-folder.md`.

### New: RESOLVER.md files in key directories
After creating folders, the setup now creates a RESOLVER.md in each key directory — a short decision tree answering "does X live here?" before any file is created. Covers CRM/, Notes/, and Originals/ by default. Prevents the slow vault decay where the same type of content ends up scattered across multiple folders because the rule was never written down. See `migrations/2026-04-09-resolver-md.md`.

### New: migrations/ folder for existing users
A `migrations/` directory now lives in the repo. Each file is a dated, plain-English guide for applying new patterns to an existing vault — what changed, why, and exactly how to apply it without re-running the full setup. The "Already Set Up?" section at the top of the skill now references these files.

---

## April 9, 2026 (late session)

### Floor notes now link to the Internal Design Substack series
All 16 floor notes and 3 tier notes (Low/Middle/High Floors) now link to the full Internal Design series page instead of a single article. Plain-text URLs throughout the skill were converted to proper markdown links. The series page gives readers access to all High-Rise writing, not just one entry.

### Claude won't recreate files you've manually moved
New rule added everywhere it counts: before creating any folder or file, Claude must check the vault map and search first. If something already exists — even if you moved it — Claude uses that location instead of creating a duplicate. This fixes the most common complaint from existing users. The rule is now baked into every generated CLAUDE.md so new users get it automatically.

### Four setup improvements
- **Already Set Up?** — new section at the top for users who want to add a feature, fix something, or upgrade their CLAUDE.md without re-running the full 21-phase setup
- **Existing vault detection** — Claude now asks "starting fresh or have existing notes?" before installing anything. Existing vaults import first, structure gets built around what's already there
- **Vault map verification gate** — hard stop between Phase 4 and Phase 5 forces Claude to confirm the vault map is actually filled in before continuing. A blank map was causing duplicate folders in every future session
- **Tier note templates** — Low/Middle/High Floors notes now have complete markdown templates matching the quality of individual floor notes (YAML, descriptions, floor lists, Substack link, Dataview queries)

---

## April 10, 2026 (evening)

### Journal skill completely rewritten — the biggest update yet
The journal skill template was a skeleton — it told Claude "include habit tracking" but didn't specify HOW. Now it's fully prescriptive with 8 explicit steps: opening question, deep follow-up logic, abundance/gratitude check, accountability check with pushback loops, idea quarantine for entrepreneurs, floor identification with all 16 floors defined, 3-4 advisory panel reactions (up from 1-2) with the full panel built into the skill, and post-save verification so entries never get lost silently.

### New: Accountability check in journal
During setup, you're now asked: "Do you want me to hold you accountable on anything?" with examples like gym consistency, sleep time, scrolling habits, and spending patterns. Whatever you choose gets built into your journal skill with specific pushback logic — not just "did you work out?" but "You're at 2/4 this week. When are you going tomorrow?"

### New: 19 floor concept notes created during setup
When you opt into floor tagging, the setup now creates a concept note for each of the 16 floors plus 3 tier notes (Low, Middle, High Floors). Each floor note explains what it feels like, lists signals, suggests how to move up, and links back to the Substack article "The Internal High-Rise — Peace Is a Place You Can Live." Click [[Fear]] in a journal entry and you land on a page that shows you every entry you've ever written from that floor.

### New: Abundance/gratitude check in every journal entry
Counters the natural bias toward only journaling when things are hard. One quick question: "What's one thing you have right now that you're grateful for?" The answer gets woven into the entry naturally.

### New: Idea quarantine for entrepreneurs
If you're building something, the journal skill now catches side ideas mid-conversation and parks them in Business/Idea Quarantine.md instead of letting them derail your focus. Also flags escape patterns: "Is this real inspiration or escape from the hard thing?"

### New: /team-weekly — operational digest for team vaults
If you set up a team vault (Phase 20), the setup now creates a /team-weekly skill that generates a weekly operational digest: meetings, pipeline, sales, product updates, decisions, and open loops. Scans all files modified in the past 7 days across the team vault. Saves to both team and personal vault. Business only — no journals or personal content.

### Auto-update check on every session start
The skill now checks for updates automatically via a session hook — not just when you run /setup-brain. If a newer version exists on GitHub, Claude tells you and offers to update with one command. No manual checking needed.

### Vault Changelog, Content Drafts, and Idea Quarantine created during setup
Phase 5 now creates Vault Changelog.md (tracks what you build), Content Drafts.md (auto-captures sharp insights from conversations), and Idea Quarantine.md (parks business ideas so they don't derail your main focus). Previously these were referenced in the rules but never actually created.

### "Never fail silently" rule added to generated CLAUDE.md
Rule 11 in the Obsidian Rules section. If anything fails — file save, install, path issue — Claude must tell you immediately and fix it.

### Journal index for fast date lookups
New Python script at Meta/scripts/build-journal-index.py creates a JSON index of all journal entries by date. The insights skill reads this index instead of grepping hundreds of files — fixes the bug where /weekly and /monthly found wrong entry counts or timed out on large vaults.

### New: Living floor notes — updated by weekly/monthly insights
Floor concept notes now grow over time. After each /weekly or /monthly insight report, the insights skill checks if any floor that appeared 2+ times has a new personal pattern worth capturing — triggers, movement strategies, person-floor correlations, surprises. Appends them under a `## Personal Patterns` section on the floor note. Monthly insights do a deeper review and can update, merge, or retire stale patterns. Over time, clicking [[Fear]] shows YOUR fear patterns, not a textbook definition.

### Setup no longer stops mid-flow
Previously, Claude might stop after the journal phase and wait for you to ask "what's next?" Now it automatically continues through all 21 phases unless you explicitly say to pause.

### Phase 13 streamlined
Health & Habit Tracking was redundant with the journal skill. Now Phase 13 only covers importing external health data (Apple Health, Fitbit, etc.). Basic habit tracking is handled in Phase 10.

---

### Advisory panel reactions in daily journal
Your daily journal entries now get 1-2 advisor reactions after saving — short, in-character sentences from the same 50+ voice panel used in weekly/monthly insights. Instead of just saving and moving on, Claude picks the 1-2 advisors most relevant to what came up and gives you a quick outside perspective. It's like having Naval or Brene Brown read your journal entry and give you one sentence back.

### Example outputs added
New file: EXAMPLES.md. Shows exactly what a daily journal entry and a weekly insight report look like — full frontmatter, raw first-person journaling, floor tags, advisor reactions, life coach flags, therapist observations, and the closing question. Fictional but realistic. If you're wondering "what does this actually produce?" — now you can see it before committing to the setup.

### /journal routing in CLAUDE.md
The setup now adds `/journal` routing to your CLAUDE.md so it works as a slash command, just like `/weekly` and `/monthly` already do. Previously you had to remember to type the full skill name or hope Claude figured it out.

### Skill & routing health check in /optimize-brain
New Phase 10 in the optimization skill: verifies all your skills exist, all file paths resolve to real folders (catches the common double-Desktop bug), all slash commands are routed in CLAUDE.md, the session protocol hook is installed, and the advisory panel is present in both the journal and insights skills. Also fixed duplicate numbering in the phase list.

---

## April 9, 2026 (late night update)

### Session protocol hook — Claude reads your files BEFORE responding
The biggest reliability problem with the vault was that Claude sometimes greeted you before reading your CLAUDE.md, Last Session, and Current Priorities files — meaning it started the conversation without context. The fix: a `UserPromptSubmit` hook that fires on your very first message each session and forces Claude to read those files before saying anything. It's automatic — you don't have to ask. The hook fires once per session and self-removes. Setup-brain now installs this hook during Phase 5 (context layer).

### Calendar-based weekly & monthly periods
Weekly and monthly insights now use calendar periods instead of rolling windows. /weekly covers Monday–Sunday of the calendar week. /monthly covers the 1st through last day of the month. If it's early in the period (Monday/Tuesday for weekly, 1st–3rd for monthly), it defaults to the previous period so you have enough data. You can say "this week" or "this month" to override.

### /weekly and /monthly routing fix
The setup now adds routing to your CLAUDE.md so `/weekly` and `/monthly` work as direct slash commands. Previously only `/insights` was recognized.

### Full advisory panel with voice descriptions
Every advisor on the panel now has a description of who they are, what they're known for, and how they speak — so Claude actually sounds like them instead of giving generic advice. 50+ voices across 8 categories: wealth & strategy (Naval, Buffett, Dalio, Hormozi, Andreessen, and Colombian founders like Vélez, Borrero, Moreno), leadership (Sandberg, Rabois, Collison), psychology (Brené Brown, Gabor Maté, Jungian/CBT/existential/inner child voices), relationships (Perel, Gottmans, Terry Real, Sue Johnson), health (Attia, van der Kolk, Stacy Sims), wisdom (Thich Nhat Hanh, Marcus Aurelius, Maya Angelou), and creativity (Rick Rubin, Elizabeth Gilbert, Twyla Tharp). Each one challenges you differently.

### Richer insight reports
The weekly/monthly insight skill now includes: a "Wins to Celebrate" section so good days don't get overlooked, habit tracking in the frontmatter (gym count, average bedtime), and a "never fail silently" rule — if the report fails to save, Claude tells you immediately instead of losing it.

### Automatic insight generation (cron / Task Scheduler)
The setup now offers to schedule your weekly and monthly insights to run automatically — no typing required. On Mac/Linux it sets up a cron job; on Windows it creates a Task Scheduler entry. Weekly runs every Monday morning, monthly on the 2nd. Logs to `⚙️ Meta/scripts/.insights-cron.log` so you can verify it ran. You can still run /weekly or /monthly manually anytime.

---

## April 9, 2026 (night update)

### Weekly & Monthly Insight Reports
Type /weekly or /monthly anytime. Claude reads all your journal entries for that calendar period and gives you: floor trends (are you moving up or down?), patterns a life coach would flag ("you mentioned this person 4 times and each time your floor dropped"), observations a therapist would explore ("there's a thread of guilt running through this week you haven't named"), advisory panel thoughts on your week, and one question to sit with. Saves as a note so you can look back over months. It's like a therapist session, a life coach check-in, and a board meeting — on demand, from your own data.

### Team Vault Setup
If you have a team, Claude now walks you through creating a separate shared vault (synced through Google Drive or similar) that stays connected to your personal vault. Business files sync automatically. Personal stuff stays private. Team members get their own First Time Setup instructions. You work from your personal vault (which knows your whole life), they work from the team vault (which knows the business). No double-entry, no drift.

---

## April 9, 2026 (evening update)

### Efficiency tools install FIRST (Phase 0)
The setup now installs Graphify and Claude-Mem before the conversation starts, not after. This means the entire setup process uses fewer tokens — saving you money and making everything faster. Previously these were installed at Phase 9, after the vault was already built. Now they're running from the start.

### Windows + Linux support
Phase 0 now detects your operating system and gives the right install commands. Previously it was Mac-only. If you're on Windows, it walks you through downloading Python and Node.js. If you're on Linux, it uses your package manager.

### Obsidian CLI setup
If you're on Mac or Linux with Obsidian 1.12.7+, the setup now tries to enable the Obsidian CLI. This lets Claude search your vault, check backlinks, and find broken links much faster. If it's not available (Windows or older Obsidian), it skips silently — everything still works.

### Emotional floor tagging in the journal
Your daily journal entries now get tagged with an emotional "floor" — a level from 1 (Shame) to 16 (Peace). Over time this builds a map of your emotional patterns: which people, activities, and decisions put you on which floors. If you don't want it, just tell Claude "turn off floor tagging." Learn more about the framework: [Internal Design — The High-Rise Model on Substack](https://adelaidadiazroa.substack.com/s/internal-design)

### Journal saves now include floor in YAML frontmatter
Each journal entry saves the floor name and level (low/middle/high) in the file metadata, so you can query across entries: "show me all my Love entries" or "what was my average floor this month."

---

## April 9, 2026

### AI chat export & cleanup
You probably have months or years of conversations in ChatGPT, Claude, or Gemini that contain real thinking — business ideas, personal processing, decisions, brainstorming. The setup and optimize skills now walk you through exporting those conversations and importing them into your vault. They also help you clean up the noise (like "how do I resize an image" or "what's the weather") so you keep the gold and delete the junk.

### External tools connection (email, calendar, Slack, CRM, meetings, design)
The setup skill now walks you through connecting Claude to your actual tools — Gmail, Google Calendar, Slack, HubSpot, meeting recorders, Canva, Figma, and more. This means Claude can search your email, check your schedule, draft messages, and pull context from your CRM — all with your vault as the brain behind it.

### Book notes & highlights import
If you highlight books on Kindle, Apple Books, Readwise, or even physical books — those highlights are now part of the setup. The skill walks you through exporting and importing them so your reading connects to your thinking.

### Health & habit tracking
The journal skill can now track habits like gym, sleep, mood, or anything else you care about. It asks at the end of each journal entry and includes a quick summary line. Over time, you can see patterns — like whether your best weeks have something in common.

### Concept taxonomy
The skill now scans your notes for recurring themes and offers to create a "concept note" for each one. These become hubs that everything else links through. It's what turns a folder of files into a thinking system.

### Backup & sync setup
Your vault is just a folder. If it disappears, everything is gone. The skill now walks you through setting up backup — Google Drive, iCloud, Dropbox, or git.

### Obsidian power rules added to CLAUDE.md
The setup now adds rules to your memory file that make Claude smarter in every session: always wikilink, never duplicate titles, capture content ideas and decisions automatically, quarantine shiny new ideas so they don't distract from your main work.

### Auto-update check
When you run /setup-brain or /optimize-brain, the skill now checks if there's a newer version available and offers to update first. No more running an outdated version without knowing.

---

## April 8, 2026 — Launch

### Initial release
- `/setup-brain` — interactive setup that builds your vault through conversation (13 phases)
- `/optimize-brain` — deep optimization for existing vaults (9 phases)
- Accountability rules, daily journaling, power tools, CRM, templates
- Free, open source, MIT licensed
