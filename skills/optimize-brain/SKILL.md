---
type: skill
name: optimize-brain
description: Deep optimization for an existing Obsidian vault. Compress archives into summaries, standardize contacts, tag journals, clean up the graph, build dashboards, and create domain summaries. The weekend version of /setup-brain.
trigger: /optimize-brain
---

# Optimize Brain — Deep Vault Optimization

> **`{SKILL_DIR}`** = this skill's own folder (locally: the directory this SKILL.md lives in; a served brain substitutes the real absolute path before you read this). Shared starter files live at the repo root two levels up: `{SKILL_DIR}/../..`. If a path does not resolve, name the missing file and stop — never guess another location.

You are running a deep optimization on an existing Obsidian vault. The user has already set up the basics (folder structure, CLAUDE.md, context layer) — either through `/setup-brain` or manually. Now they want the full treatment.

This is a weekend project, not an afternoon one. Set expectations upfront and let them choose which phases to run.

Your tone: collaborative, methodical, honest about what takes time. You're a vault architect, not a setup wizard.

## Opening

Start with:

"Hey! This is the deep optimization pass. Unlike setup, this one works with what you already have — your existing notes, journals, contacts, and files.

Here's what we can do. Each phase is independent — pick the ones that matter to you, or we can do them all:

1. **File Audit** — scan your vault, report what's there, find the mess (10 min)
2. **AI Chat Import & Cleanup** — export and import your ChatGPT/Claude/Gemini history, keep the gold, delete the noise (30-60 min)
3. **CRM Standardization** — turn scattered people mentions into queryable contacts (30-60 min)
4. **Journal Compression** — create monthly summaries from your journal entries (30-60 min)
5. **Domain Summaries** — one compressed note per life area (20-30 min)
6. **Graph Cleanup** — fix broken links, connect orphans, add missing links (20-30 min)
7. **Dashboards** — build live Dataview queries for contacts, tasks, projects (15-20 min)
8. **Wikilink Audit** — scan for people, concepts, and themes that should be linked but aren't (20 min)
9. **About Me / Then vs Now** — build a self-portrait from your data (15 min)
10. **Skill & Routing Health Check** — verify all skills work, paths resolve, CLAUDE.md routing is complete (10 min)
11. **Vault Health Report** — final audit with stats and recommendations (10 min)

Which ones do you want to tackle? Or want to start from the top and do them all?"

**Wait for their answer. Don't run everything automatically.**

---

## Phase 1: File Audit

"Let me scan your vault and tell you what we're working with."

Scan the vault and report:

```
Vault Stats:
- Total files: [X]
- Markdown files: [X]
- Non-markdown files: [X] (images, PDFs, etc.)
- Files with YAML frontmatter: [X]
- Files WITHOUT frontmatter: [X]
- Folders: [list with counts]

Biggest folders: [top 5 by file count]
Oldest file: [name, date]
Newest file: [name, date]

Potential issues:
- [X] files with no creation date
- [X] files in the root (not organized into folders)
- [X] files that might be duplicates (similar names)
- [X] non-markdown files that could be converted
- [X] empty files
```

Ask: "Anything here surprise you? Want me to clean up any of these issues before we move on?"

**Quick fixes to offer:**
- Move root files into appropriate folders
- Add missing frontmatter (creationDate, type)
- Flag duplicates for review
- Delete empty files (with confirmation)

---

## Phase 2: AI Chat Import & Cleanup

"Your AI chat history is some of the most valuable data you have. Every conversation where you brainstormed an idea, processed a decision, worked through a problem — that's your thinking captured in real time. Let's get it into your vault."

### Step 1: Export
Walk them through exporting from each platform they use:
- **ChatGPT:** Settings → Data Controls → Export data → zip file with all conversations
- **Claude (claude.ai):** Settings → Account → Export Data → zip file
- **Google Gemini:** gemini.google.com → Activity → Download, or Google Takeout
- **Other AI tools:** Check settings for export/download

### Step 2: Import and convert
Convert exported JSON/HTML files to markdown. Add basic frontmatter (creationDate, type: ai-chat). Place in an `AI Chats/` folder.

### Step 3: Triage
Scan all imported chats and categorize:

**Keep and organize:**
- Chats with real thinking — business strategy, personal processing, creative brainstorming, decision-making
- Rename with descriptive titles based on content (not "Chat from March 15")

**Delete:**
- Quick utility chats: "how do I resize an image," "what's the capital of," "fix this CSS," "should I buy this milk," "how do I make the elevator work"
- One-line lookups with no lasting value

Ask before bulk-deleting: "I found [X] chats that look like quick utility questions. Want me to delete those and keep the meaningful ones?"

**Extract and archive:**
- Some long chats have one great insight buried in a rambling conversation. Extract the insight into a proper standalone note, then archive the chat.

### Step 4: Report
```
AI Chat Import Complete:
- Total chats imported: [X]
- Kept (meaningful): [Y]
- Deleted (utility): [Z]
- Insights extracted to standalone notes: [W]
```

---

## Phase 3: CRM Standardization

"Let me find every person mentioned in your vault and turn them into queryable contacts."

### Step 1: Scan for people
Scan all files for:
- Existing CRM/people entries
- Names mentioned frequently in wikilinks `[[Name]]`
- Names mentioned in text without wikilinks
- People in meeting notes, journal entries, etc.

Report:
```
People found:
- [X] existing CRM entries
- [X] names mentioned in wikilinks but no CRM entry
- [X] frequently mentioned names without any entry

Top 20 most-mentioned people:
1. [Name] — [X] mentions — [has CRM entry? Y/N]
2. ...
```

### Step 2: Ask for context
"Here are the people who show up most but don't have contact cards. For each one, tell me who they are (one line is fine):"

List the top names without CRM entries. Wait for their answers.

### Step 3: Create CRM entries
For each person, create a note in the CRM folder:

```yaml
---
creationDate: [today]
type: person
aliases: [nicknames]
relationship: [from user's answer]
status: [active/inactive — guess from recency]
priority: [high/medium/low — guess from mention count]
---
```

Add a Dataview query at the bottom of each:
```dataviewjs
const name = dv.current().file.name;
const linked = dv.pages(`[[${name}]]`)
  .where(p => !p.file.path.includes("Meta"))
  .sort(p => p.creationDate || p.file.mtime, "desc");
const rows = linked.map(p => {
  const date = p.creationDate
    ? String(p.creationDate).slice(0,10)
    : p.file.mtime.toFormat("yyyy-MM-dd");
  const folder = p.file.folder.split("/").pop();
  return [p.file.link, date, folder];
});
dv.paragraph(`**${rows.length} mentions**`);
dv.table(["File", "Date", "Source"], rows);
```

### Step 4: Standardize existing CRM entries
For existing entries missing fields, add:
- type: person
- relationship (ask if unknown)
- status
- priority
- last_interaction (estimate from latest mention)
- Dataview query if missing

Report: "Created [X] new contact cards. Updated [Y] existing ones. Your CRM now has [Z] total contacts."

### Step 5: Cross-reference
"Want me to cross-reference your CRM against your journals? I can tell you:
- Who's in the CRM but never shows up in your writing (inflated?)
- Who's in your writing constantly but has no CRM entry (actually important?)
- Who has an inflated role vs. actual presence?"

If yes, run the audit and report findings. Let them decide what to fix.

---

## Phase 4: Journal Compression

"If you have a lot of journal entries, reading them all every time is impossible. I'll create monthly summaries that compress each month into one note."

### Step 1: Scan journals
Count entries by month. Report:
```
Journal entries found: [X]
Date range: [earliest] to [latest]
Entries per year: [breakdown]

Months with entries: [X]
Months WITHOUT summaries: [X]
```

### Step 2: Create monthly summaries
For each month without a summary, read all entries and create:

```markdown
---
creationDate: [first day of month]
type: summary
month: YYYY-MM
entries_count: [X]
---

# YYYY-MM Monthly Summary

## Themes
[3-5 major themes that month — work, relationships, health, emotions]

## Key Events
[Bullet list of what happened]

## Emotional Landscape
[What floors were dominant? Any patterns?]

## Key People
[Who showed up most this month and in what context]

## Open Threads
[What was unresolved at month's end?]

## Quotes
[2-3 most striking lines from the entries that month]
```

Save to `Journals/Monthly Summaries/YYYY-MM Monthly Summary.md`

**Important:** Process in batches. Don't try to do 5 years at once. Ask:
"How far back do you want me to go? Recent 6 months? Full history? I'll do them in batches of 3-4 months at a time."

### Step 3: Report
"Created [X] monthly summaries covering [date range]. Now I can understand any month of your life from one note instead of reading [Y] entries."

---

## Phase 5: Domain Summaries

"Every major area of your life gets one compressed summary note. These let me understand a whole domain from 400 words instead of scanning dozens of files."

### Step 1: Identify domains
Look at the folder structure and file content. Suggest domains:
"Based on your vault, I'd create summaries for:"
- [List 5-8 domains based on their actual folders and content]

Ask: "Does this look right? Any to add or remove?"

### Step 2: Create summaries
For each domain, read through the relevant files and create:

```markdown
---
creationDate: [today]
type: summary
domain: [name]
last_updated: [today]
---

# [Domain] Summary

## Current State
[One paragraph — what's happening right now in this area]

## Key Files
[5-10 most important files with [[wikilinks]]]

## Key People
[Who's involved in this domain]

## Open Questions
[What's unresolved or uncertain]

## History (brief)
[2-3 sentences of context — how did this area get to where it is now]
```

### Step 3: Link
Add links to domain summaries from CLAUDE.md and 00 Start Here if they aren't already there.

---

## Phase 6: Graph Cleanup

"Now let's fix the connections. A disconnected graph means I (and you) can't follow threads."

### Step 1: Scan
Use Obsidian CLI if available, otherwise scan files directly:

```
Graph Health:
- Total wikilinks: [X]
- Broken links (point to notes that don't exist): [X]
- Orphan notes (no incoming links): [X]
- Dead-end notes (no outgoing links): [X]
- Notes with aliases: [X]
```

### Step 2: Fix broken links
For each broken link, categorize:
- **Create as real note** — if it's a concept, person, or topic that should exist
- **Fix typo** — if it's a misspelling of an existing note
- **Remove** — if it's junk

Present the list and ask for confirmation before making changes.

### Step 3: Connect orphans
For notes with no incoming links, suggest where they should be linked FROM:
"These notes exist but nothing links to them. For each one, I'll suggest 2-3 notes that should reference it."

### Step 4: Enrich dead ends
For notes with no outgoing links, suggest 3-5 outbound links to add:
"These notes don't link to anything else. I'll add connections to related concepts."

### Step 5: Add aliases
Scan for people and concepts that go by multiple names. Add aliases to their frontmatter so all variations resolve to the same note.

### Step 6: Report
```
Graph Cleanup Complete:
- Broken links fixed: [X]
- New notes created: [X]
- Orphans connected: [X]
- Dead ends enriched: [X]
- Aliases added: [X]
```

---

## Phase 7: Dashboards

"Let's build some live dashboards using Dataview. These update automatically as your vault changes."

Ask: "What would be most useful to see at a glance? I can build:"

Offer these and build whichever they want:

### CRM Dashboard
```dataview
TABLE relationship, status, priority, last_interaction
FROM "CRM"
WHERE type = "person"
SORT priority ASC, last_interaction DESC
```

### Needs Follow-Up
```dataview
TABLE relationship, last_interaction, next_step
FROM "CRM"
WHERE type = "person" AND status = "active" AND next_step != ""
SORT last_interaction ASC
```

### Recent Journal Entries
```dataview
TABLE creationDate
FROM "📓 Journals"
WHERE type != "summary"
SORT creationDate DESC
LIMIT 20
```

### Open Tasks Across Vault
```dataview
TASK
WHERE !completed
SORT file.mtime DESC
LIMIT 30
```

### Recent Meeting Notes
```dataview
TABLE creationDate, attendees
FROM "Meeting Notes" OR "Meetings"
SORT creationDate DESC
LIMIT 15
```

### Custom dashboards
Ask: "Anything specific you want to track? (project status, book reading, habits, etc.)"

Build custom Dataview queries based on their answer.

---

## Phase 8: Wikilink Audit

"I'm going to scan every file for names, concepts, and themes that should be wikilinked but aren't. This makes your graph denser and more navigable."

### Step 1: Build reference list
Scan all files and extract:
- All existing note titles and aliases
- All CRM entry names
- Common concept words that have notes (e.g., Fear, Love, Growth, Money)

### Step 2: Scan for unlinked mentions
Find places in files where these names/concepts appear in plain text but aren't wikilinked.

Report:
```
Found [X] unlinked mentions across [Y] files.
Top unlinked references:
- "[Name]" appears unlinked [X] times
- "[Concept]" appears unlinked [X] times
```

### Step 3: Add wikilinks
Ask: "Want me to add the wikilinks? I'll show you a preview first."

For each file, show what would change, then apply with confirmation.

**Be careful:** Don't wikilink common words that happen to match note titles. Only link when the reference is clearly about that concept/person.

---

## Phase 9: About Me / Then vs Now

"Let's build a self-portrait from your data. This isn't a resume — it's who you are, based on what you've written."

### Step 1: Gather data
Read:
- CLAUDE.md
- Journal entries (especially recent + a few from years ago)
- Monthly summaries if they exist
- Any personal/about pages

### Step 2: Interview
"Before I write this, a few questions:"
1. "How would you describe yourself to someone who matters to you — not a LinkedIn bio?"
2. "What's changed most about you in the last year?"
3. "What hasn't changed in a decade?"
4. "What are you most proud of that most people don't know about?"

### Step 3: Create About Me
Create `Meta/About Me.md`:

```markdown
---
creationDate: [today]
type: meta
last_updated: [today]
---

# About Me

## Who I Am
[From their answers + vault data — 2-3 paragraphs, their voice]

## Then vs. Now
[What's changed based on journal data — specifics, not generalities]

## What Stays the Same
[Patterns that persist — the thread through everything]

## What I'm Working Toward
[From current priorities and open loops]
```

---

## Phase 10: Skill & Routing Health Check

"Let me make sure all your skills are working and your CLAUDE.md has the right routing entries."

### Step 1: Verify skill paths
Check that every skill file referenced in CLAUDE.md actually exists.

Skills install as sibling folders of this one, so list what is actually on disk rather than guessing at a fixed set of names:

```bash
# Every installed skill's SKILL.md, whatever the install shape
ls "{SKILL_DIR}"/../*/SKILL.md
```

Now read the routing entries in their CLAUDE.md and compare the two lists. Any skill CLAUDE.md routes to that is missing from the listing is a dead routing entry — name it and offer to reinstall it. The reverse case (installed but unrouted) is Step 3, not this one.

### Step 2: Verify save paths in skills
Read each skill file and check that any file paths it references (journal save path, insight save path, etc.) resolve to real folders. Common bug: double "Desktop" in paths, missing emoji prefixes on folders.

```bash
# Example: check if the journal save folder exists
ls "/path/from/skill/Journals/"
```

If a path is broken, fix it and tell the user what was wrong.

### Step 3: Verify CLAUDE.md routing
Check that the user's CLAUDE.md has routing entries for all installed skills:

- `/journal` → daily-journal skill
- `/weekly` and `/monthly` → insights skill
- `/graphify` → graphify skill
- `/humanizer` → humanizer skill
- `/setup-brain` → ai-brain-starter skill

For any missing routing, add it. Route by skill NAME, not by install path — the Skill tool resolves the name, and a hardcoded path goes stale the moment the brain is read on another machine or by another tool:

```markdown
# [skill name]
- **[skill-name]** — [description]. Trigger: `/[command]`
When the user types `/[command]`, invoke the Skill tool with `skill: "[skill-name]"` before doing anything else.
```

### Step 4: Verify session protocol hook
Check if the UserPromptSubmit hook is installed in `.claude/settings.local.json`:

```bash
cat .claude/settings.local.json | grep -q "UserPromptSubmit" && echo "Hook installed" || echo "MISSING"
```

If missing, install it (see Phase 5 of setup-brain for the hook JSON).

### Step 5: Check insights skill references advisory panel
Read the insights skill and verify it has the full advisory panel with voice descriptions. If it's using a generic "select relevant advisors" instruction without actual names, flag it.

### Step 6: Check daily journal references advisory panel
Read the daily journal skill and verify it includes the advisory panel step after saving. If missing, add it (1-2 advisors, in-character, one sentence each).

Report:
```
Skill Health Check:
- Skills found: [X] of [Y] expected
- Broken paths fixed: [X]
- Missing routing entries added: [X]
- Session hook: [installed/missing]
- Advisory panel: [in insights: Y/N] [in journal: Y/N]
```

---

## Phase 11: Vault Health Report

"Final audit. Here's the state of your vault after optimization."

Generate a report:

```markdown
# Vault Health Report — [date]

## Stats
- Total files: [X]
- Files with frontmatter: [X] ([%])
- CRM contacts: [X] (active: [Y], inactive: [Z])
- Journal entries: [X] (spanning [date range])
- Monthly summaries: [X]
- Domain summaries: [X]
- Broken links: [X]
- Orphan notes: [X]
- Dead ends: [X]

## What We Did Today
[List of phases completed and what changed]

## Recommendations
[What to do next — things that need manual review, areas to keep building]

## Maintenance Schedule
- **Weekly:** Update Last Session, add new contacts to CRM
- **Monthly:** Review Open Loops, create monthly journal summary
- **Quarterly:** Update About Me, refresh domain summaries, run graph cleanup, audit CRM
```

Save to `Meta/Vault Health Report.md`.

---

## Important Notes for Claude

- **Don't do everything at once.** Let the user pick phases. If they want all 9, break it into chunks with natural stopping points.
- **Show before you change.** For CRM standardization, graph cleanup, and wikilink audit — always preview changes before applying.
- **Batch large operations.** If they have 500 journal entries, don't process all at once. Do 3-4 months at a time.
- **Be honest about time.** "This phase has 200 files to process. It'll take about 15 minutes of me working. Want to grab coffee while I do it?"
- **Celebrate the stats.** People love seeing their vault quantified. "You have 12 years of journals — that's 2,071 entries. That's extraordinary."
- **Don't touch journal content.** Journals are sacred. Add frontmatter, create summaries, add wikilinks — but never edit the actual journal text.
- **Adapt to their vault.** Not everyone has journals. Not everyone has a CRM. Skip phases that don't apply and suggest what would be most impactful for their specific vault.
- **Save progress.** After each phase, update Last Session.md so they (and you) know what was done if the session ends.
- **NEVER FAIL SILENTLY.** After every file write, verify the file exists. After every install, verify it worked. If ANYTHING fails — wrong path, missing folder, permission error — TELL THE USER IMMEDIATELY, then FIX IT. Create the missing folder, correct the path, retry. Don't just report problems; solve them. People's data is sacred.
