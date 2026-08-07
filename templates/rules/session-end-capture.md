# Session end — capture cascade (don't lose context)

When the user signals the session is ending, run this capture cascade **before** saying goodbye. The point: **nothing useful from the conversation gets lost.** Every personal insight, every team decision, every workflow improvement gets written to the right place in the right vault.

## Closing signals to listen for

Fire the cascade automatically when the user says any natural close:

- "ok bye" / "thanks, that's all" / "we're done" / "I'm done"
- "good night" / "talk later" / "catch you tomorrow"
- "let's stop here" / "wrapping up" / "that's enough for today"
- "ttyl" / "later" / "cya"
- Or any equivalent phrase in their primary language

The user can also trigger it manually with `/wrap-up`.

**Skip the cascade if the session was tiny** — fewer than 5 user messages, or under ~1000 tokens of substantive conversation, or "just chatted" with no decisions, no information, no learnings. Don't make a 5-minute closing ceremony out of a 30-second hello-goodbye.

## The cascade — run all of it, in order

### Step 1 — Scan the conversation

Look back through the session and identify everything worth preserving. Categorize each item into one of these buckets:

- **Decisions** — anything they decided during the session, even small ones, especially if it has implications later
- **Personal context** — feelings, goals, life events, relationships, things about themselves that update what you know about who they are
- **Team / business context** — strategic decisions, customer info, sales pipeline updates, roadmap changes, fundraising context, anything about their team or company
- **New facts** — things they told you about people, projects, tools, places that you didn't know going in
- **Workflow learnings** — patterns that worked well, friction points, edge cases, things that surprised you about how they work
- **Ideas** — things to write about, things to build, things to explore later
- **Improvements to the AI brain setup itself** — anything about the skills, the rules, the tools, the workflow that could be better. Friction they hit. Things that should be automatic but aren't. Bugs in the setup. Missing features.

### Step 2 — Decide where each item goes

The destination depends on the type of context AND which vault is loaded:

| Type | Destination |
|---|---|
| **Personal** (feelings, life, relationships, goals) | Personal vault: a new file in `⚙️ Meta/Sessions/YYYY-MM-DDTHH-MM-{worktree}.md` (always); a new file in `⚙️ Meta/Decisions/YYYY-MM-DDTHH-MM-{slug}.md` if a decision; a journal entry if reflective/emotional |
| **Team / business** (work, strategy, customers, fundraising) | Team vault if one exists: same per-worktree pattern — `⚙️ Meta/Sessions/` and `⚙️ Meta/Decisions/` in the team vault |
| **Improvements to the AI brain setup** | File a GitHub issue (see Step 4) — do NOT clutter their vault with this |
| **Anything ambiguous** | Personal vault session file with a note flagging it for review |

**Per-worktree writes (race-safety).** Write to files in `⚙️ Meta/Sessions/` and `⚙️ Meta/Decisions/`, **never** to the shared `Last Session.md` or `Decision Log.md`. Those two files are auto-generated aggregator views rebuilt from the per-worktree source files. The session-end hook creates a stub for you at `⚙️ Meta/Sessions/YYYY-MM-DDTHH-MM-{worktree}.md` — replace the stub body with the full session summary, keep the frontmatter fields valid (`creationDate`, `type: session`, `worktree`, `session_date`). For decisions, create new files at `⚙️ Meta/Decisions/YYYY-MM-DDTHH-MM-{slug}.md` with frontmatter `type: decision, worktree, decision_date, floor, stakes, speed, outcome, pattern`. After writing, run `VAULT_ROOT='...' python3 '⚙️ Meta/scripts/aggregate-sessions.py'` and the same for `aggregate-decisions.py` — or let the session-end hook run them for you. This pattern is race-safe against concurrent worktrees: unique filenames in the source folders eliminate write contention, aggregator output is deterministic from sorted input so concurrent aggregator runs can clobber each other without data loss. See [mycelium-hq/ai-brain-starter#5](https://github.com/mycelium-hq/ai-brain-starter/issues/5).

**For users with both a personal vault AND a team vault** (joined via the team-vault join flow): always cascade to BOTH. Personal stuff goes to the personal vault, team stuff goes to the team vault. Never let personal stuff leak into the team vault. When ambiguous, default to personal — the team vault stays clean.

**For users with only a personal vault:** the team-vault destination collapses into the personal vault's work folder (whatever they named it during setup).

### Step 3 — Write to the vault(s)

Use the file paths in this CLAUDE.md's vault map. Don't ask the user where things go — figure it out from the existing structure. If the destination file doesn't exist, create it (but first confirm with the user what to name it).

For each write:
- **Don't overwrite**, append. Use markdown headings to keep entries chronological.
- **Use the date format** the rest of the file uses (look at the existing entries for reference).
- **Include enough context** that the entry makes sense in 6 months without re-reading the whole conversation. A one-line title + 2-3 sentence summary + any key quotes or numbers.
- **Wikilink** people, projects, and concepts so they connect to the rest of the vault.

### Step 4 — File improvement ideas as GitHub issues

For anything in the "Improvements to the AI brain setup" bucket, **draft a GitHub issue and offer to file it for them.**

**Draft format:**

```markdown
**What happened:** [one-line description of the friction or missing feature]

**Context:** [what we were doing when this came up — 2-3 sentences]

**Suggested fix:** [if you have one — otherwise leave this blank and let the maintainer figure it out]

**Reported by:** [user's name from CLAUDE.md, if available; otherwise "an ai-brain-starter user"]
**Session:** [today's date]
```

**Show the draft to the user** and ask: *"I want to send this to the maintainer of the AI brain setup so they can fix it for everyone — yourself included on the next update. Want me to file it?"*

**If yes:**

Run:
```bash
gh issue create --repo <MAINTAINER_REPO> \
  --title "[short title from the draft]" \
  --body "[the full draft body]"
```

Where `<MAINTAINER_REPO>` is the GitHub repo that maintains the AI brain setup. Check the user's CLAUDE.md for a `MAINTAINER_REPO` field. If none exists, default to `mycelium-hq/ai-brain-starter`.

**If `gh` isn't authenticated** (first time), tell the user: *"Quick one-time setup: run `gh auth login` in a terminal and follow the prompts (pick GitHub.com, then HTTPS, then login with a web browser). Once that's done, I can file issues automatically forever after. Want to do that now or later?"*

**If they say no / not now:**

Save the draft to `<vault>/💡 Improvement Ideas.md` (create the file if it doesn't exist) so they can review later. Tell them: *"Saved to your Improvement Ideas file — you can review and file them anytime."*

### Step 5 — Run the aggregators

After all the per-worktree source files are written (session file + any decision files), run both aggregator scripts to refresh the shared `Last Session.md` and `Decision Log.md` views:

```bash
VAULT_ROOT="<absolute vault path>" python3 "<vault>/⚙️ Meta/scripts/aggregate-sessions.py"
VAULT_ROOT="<absolute vault path>" python3 "<vault>/⚙️ Meta/scripts/aggregate-decisions.py"
```

The session-end hook also runs `aggregate-sessions.py` automatically as its final step, so this manual run is only needed if you created or edited session/decision files after the hook fired, or if you want the refreshed view immediately.

### Step 6 — Confirm with the user

In plain language, tell them what was saved and where:

> "Saved a quick summary to your Last Session file. [If a decision: 'Logged the [topic] decision to your Decision Log.'] [If a personal entry: 'Added a note to your journal about [topic].'] [If an improvement: 'I filed a GitHub issue for the [topic] friction so the maintainer can fix it.'] Talk soon."

Then say goodbye in their primary language.

## What NOT to do

- **Don't ask the user what to save.** Scan the conversation, decide, and do it. They're tired and wrapping up — don't make them organize their own context.
- **Don't make it long.** This whole cascade should take ~30 seconds of work and produce 1-3 short summary messages to the user. Not a 5-minute ceremony.
- **Don't write to the team vault if it's personal content.** The team vault must stay strictly business — see the personal/team firewall rule elsewhere in this CLAUDE.md if applicable.
- **Don't file empty or trivial GitHub issues.** "I had a small problem and figured it out" is not an issue. Real improvements only — friction the maintainer should fix at the source, not one-off user errors.
- **Don't fail silently.** If the gh CLI isn't installed, or the vault path is missing, or a file write errors — TELL THE USER. Better a clumsy "I tried to save this but couldn't, can you do it manually?" than a silent loss of context.

## Why this rule matters

Without this rule, every session ends with valuable context evaporating: a decision that didn't get logged, a friction point the maintainer never hears about, a personal insight that didn't make it into the journal. After 50 sessions, that's hundreds of pieces of lost context — exactly the thing the AI brain setup is supposed to prevent. The cascade is the safety net.
