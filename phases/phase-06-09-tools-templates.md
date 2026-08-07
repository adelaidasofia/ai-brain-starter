## Phase 6: Tool Routing

Ask: "What tools do you already use day to day? I want to know so I route tasks to the right tool instead of doing everything here. Things like:
- Research: Perplexity, Google, ChatGPT?
- Design: Canva, Figma?
- Project management: Linear, Notion, Asana?
- CRM/Sales: HubSpot, Apollo?
- Meetings: Granola, Otter, Fireflies?
- Writing/websites: Framer, Substack, Ghost?
- Anything else?"

Build a Tool Routing section for their CLAUDE.md based on what they use. Include ALL their tools, plus defaults for gaps:

```markdown
## Tool Routing — Use the right tool for the job

| Task | Best Tool | Don't Do Here |
|------|-----------|--------------|
| Quick web research, fact-checking | [Perplexity/their answer] | Don't hallucinate or guess |
| Deep research + deliverables | [Manus AI / their answer] | Don't spend 30min researching here |
| Meeting transcription | [Granola/Otter/their answer] | Don't manually transcribe |
| Design / visuals | [Canva/Figma/their answer] | Don't describe designs in text |
| Project management / sprints | [Linear/Notion/their answer] | Don't track sprints in markdown |
| CRM / sales pipeline | [HubSpot/their answer] | Don't build pipeline trackers in notes |
| Website building | [Framer/their answer] | Don't build HTML here |
| [add rows for any other tools they mentioned] | | |

**Rule:** When a task is better suited to another tool, say: "This is a [Tool] task — do it there, paste the result here if you need me to process it." Don't burn Claude tokens when another tool is faster.
```

## Phase 7: Import Existing Notes (if they have them)

Ask: "Earlier you mentioned you have notes in [whatever they said]. Want to import them now? Here's what we can do with them:"

**For each source, explain the benefit and process:**

- **Apple Notes:** "Export as text, drop into vault. I'll add structure and links."
- **Google Docs:** "Download as .docx, I'll convert to markdown."
- **Notion:** "Export as markdown from Notion settings. Drop the folder in."
- **Paper journals:** "Take photos, I'll transcribe them with OCR."
- **Voice memos:** "Transcribe with your phone's built-in transcription, paste the text."
- **Scattered files:** "Drop them all in one folder, I'll sort them."
- **Old journals:** "These are gold. Even 10 entries give me patterns to work with. Import as many as you can."

**AI chat exports — ask about this specifically:**

"Do you have conversations saved in ChatGPT, Claude, Gemini, or any other AI tool? Those are some of your most valuable notes — they contain your thinking, your decisions, your brainstorming, your questions. Most people don't realize how much context is buried in their AI chat history."

Walk them through exporting:
- **ChatGPT:** Settings → Data Controls → Export data. You'll get a zip file with all conversations as JSON. Drop the zip in the vault, I'll convert them to markdown.
- **Claude (claude.ai):** Go to Settings → Account → Export Data. Same process — zip file, I'll convert.
- **Google Gemini:** Go to gemini.google.com → Activity → Download. Or use Google Takeout.
- **Other AI tools:** Check settings for an export/download option. Most have one.

Once imported, explain: "Not every AI chat is worth keeping. The ones where you brainstormed a business idea, processed a decision, worked through a problem, had a deep personal conversation — those are gold. The ones where you asked how to convert a PDF or fix a CSS bug? We can delete those."

**AI chat cleanup pass:**
After importing, scan the AI chats and categorize:
- **Keep and organize:** Chats with real thinking, decisions, brainstorming, personal processing, strategy discussions. Move to an `AI Chats/` folder with descriptive names.
- **Delete:** Trivial utility chats (tech support, quick lookups, "how do I do X" one-offs). Ask before bulk-deleting: "I found [X] chats that look like quick utility questions — things like 'how to resize an image' or 'what's the weather.' Want me to delete those and keep the meaningful ones?"
- **Extract and merge:** Some chats have one great insight buried in a long conversation. Extract the insight into a proper note, then archive or delete the chat.

Report: "Imported [X] AI chats. Kept [Y] meaningful ones, deleted [Z] utility chats, extracted [W] insights into standalone notes."

If they import files, do a basic standardization pass:
- Add YAML frontmatter (creationDate, type) to each file
- Move files to the right folders
- Report what was imported: "I imported X files into Y folders."

**If they have people mentioned in their notes:**
"I found [X] people mentioned across your notes. Want me to create a CRM folder with a contact card for each person? Each one will have their name, relationship to you, and a live query showing every note that mentions them."

If yes, create CRM entries with this template:

```markdown
---
type: person
aliases: [nicknames]
relationship: [friend/family/colleague/etc]
status: [active/inactive]
priority: [high/medium/low]
last_updated: [today]
---

[2-3 sentences on who this person is RIGHT NOW — their current role, your current relationship, the most important thing about them at this moment. Rewrite this section whenever something significant changes. Never append here — synthesize.]

**Next step:** [one specific action]

---

## Timeline

- [date] — [what happened, what was said, what changed]
```

**Rule:** Everything above `---` is synthesized current truth — rewrite it when things change. Everything below is an append-only evidence log — never edit, only add new entries. This means clicking a contact gives you their current state instantly, not a scroll through history.

## Phase 8: Templates

Create these template files in Meta/Templates/:

**Template - Journal Entry.md:**
```markdown
---
creationDate: {{date}}T{{time}}
---
[Write here]

## Concepts
[[Tag1]] | [[Tag2]]
```

**Template - CRM Entry.md:**
```markdown
---
creationDate: {{date}}
type: person
aliases: []
relationship:
status: active
priority: medium
---

## Context

## Connected

## Interactions
```

> **Note:** Never include `# {{title}}` after frontmatter. In Obsidian the filename IS the title; an H1 that repeats it creates a visible duplicate.

**Template - Meeting Note.md:**
```markdown
---
creationDate: {{date}}
type: meeting
attendees: []
---

## Agenda

## Notes

## Action Items
- [ ]

## Decisions Made
```

Tell them: "Templates are set up. When you create a new note in Obsidian, Templater can auto-apply these."

### Optional: pre-built Templater templates

The starter ships six richer templates in `~/.claude/skills/ai-brain-starter/templates/obsidian/` — they use Templater syntax (date auto-fill, suggesters) so they need the Templater plugin installed (already covered in Phase 2-3). Copy any you want into your vault's `Meta/Templates/` folder:

| File | What it does |
|---|---|
| `Journal Entry.md` | Clean journal scaffold with 16-floor suggester (Shame → Peace) |
| `Theme Note.md` | Concept/theme page with Dataviewjs block listing connected files |
| `CRM Entry.md` | Person note with context, connected, interactions sections |
| `Writing Draft.md` | Substack/long-form draft with series suggester |
| `Floor Check-In.md` | Emotional-floor check-in with body-awareness prompts |
| `graphify-extraction-prompt.md` | Reference prompt for building a knowledge graph from this vault — uses `{VAULT_ROOT}` placeholder, works with any corpus |

Copy command:
```bash
# Adjust the destination to your vault's Meta/Templates path
cp ~/.claude/skills/ai-brain-starter/templates/obsidian/*.md "/path/to/your/vault/Meta/Templates/"
```

None are required. Pick the ones that match how you want to work.

## Phase 9: Verify All Skills

Phase 0 installed everything. This phase verifies nothing was skipped or failed.

### Verify all skills are present
Run a quick check on every skill folder. All installs live in bootstrap: if anything below is missing, re-run `bash ~/.claude/skills/ai-brain-starter/bootstrap.sh` (Mac/Linux) or `powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\.claude\skills\ai-brain-starter\bootstrap.ps1"` (Windows).
- `ls ~/.claude/skills/graphify/SKILL.md`
- `ls ~/.claude/skills/meeting-todos/SKILL.md`
- `ls ~/.claude/skills/patterns/SKILL.md`
- `ls ~/.claude/skills/insights/SKILL.md`
- `ls ~/.claude/skills/deconstruct/SKILL.md`
- `ls ~/.claude/skills/daily-journal/SKILL.md`
- `ls ~/.claude/skills/repurpose-talk/SKILL.md`
- `ls ~/.claude/skills/nano-banana/SKILL.md`
- `ls ~/.claude/skills/humanizer`
- `graphify --version`

Add skill routing to their CLAUDE.md (global `~/.claude/CLAUDE.md` if it exists, or vault root). Add ALL of these:

```markdown
# graphify
- **graphify** (`~/.claude/skills/graphify/SKILL.md`) - any input to knowledge graph. Trigger: `/graphify`
When the user types `/graphify`, invoke the Skill tool with `skill: "graphify"` before doing anything else.

# daily journal
- **daily-journal** (`~/.claude/skills/daily-journal/SKILL.md`) — daily journal interview. Trigger: `/journal`
When the user types `/journal`, invoke the Skill tool with `skill: "daily-journal"` before doing anything else.

# humanizer
- **humanizer** (`~/.claude/skills/humanizer/SKILL.md`) — remove AI writing patterns from text. Trigger: `/humanizer`
When the user types `/humanizer`, invoke the Skill tool with `skill: "humanizer"` before doing anything else.

# insights (weekly / monthly)
- **insights** (`~/.claude/skills/insights/SKILL.md`) - journal pattern recognition. Triggers: `/weekly`, `/monthly`, `/insights`
When the user types `/weekly` or `/monthly`, invoke the Skill tool with `skill: "insights"` before doing anything else.

# patterns (Instinct Engine)
- **patterns** (`~/.claude/skills/patterns/SKILL.md`) — extract recurring patterns from sessions. Trigger: `/patterns`
When the user types `/patterns`, invoke the Skill tool with `skill: "patterns"` before doing anything else.

# meeting todos
- **meeting-todos** (`~/.claude/skills/meeting-todos/SKILL.md`) — extract action items from a meeting note. Trigger: `/meeting-todos`
When the user types `/meeting-todos`, invoke the Skill tool with `skill: "meeting-todos"` before doing anything else.

# deconstruct (first principles)
- **deconstruct** (`~/.claude/skills/deconstruct/SKILL.md`) — first-principles analyst. Trigger: `/deconstruct`
When the user types `/deconstruct`, invoke the Skill tool with `skill: "deconstruct"` before doing anything else.

# repurpose-talk
- **repurpose-talk** (`~/.claude/skills/repurpose-talk/SKILL.md`) - turn a speaking engagement into content pieces. Trigger: `/repurpose-talk`
When the user types `/repurpose-talk`, invoke the Skill tool with `skill: "repurpose-talk"` before doing anything else.

# nano-banana (image generation)
- **nano-banana** (`~/.claude/skills/nano-banana/SKILL.md`) — generate and edit images via Gemini. Trigger: `/nano-banana`
```

Tell the user what's installed: "You have [X] power tools running. Here's what each one does:" and give a one-line explanation of each slash command.
